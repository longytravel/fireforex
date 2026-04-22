"""Synthetic live runner pipeline test.

Drives ``ff.live.runner._poll_pair`` with a mock broker that yields controlled
M1 streams. Asserts:

- m1_buf accumulates bars across polls, deduplicating on index overlap.
- main_buf rollup produces one H1 bar per complete 60 M1 bars; in-progress
  H1 is held back until the 60th M1 bar arrives.
- ``last_main_ts`` advances once and only once per new closed H1 bar.

Does NOT require MetaTrader5 or network. Signal evaluation and order
submission are stubbed via ``monkeypatch`` so this test exercises the data
plumbing only; signal-content correctness is already covered by the full
backtest golden test.
"""
from __future__ import annotations

import pandas as pd
import numpy as np
import pytest

from ff.live import runner as live_runner


class _MockBroker:
    def __init__(self) -> None:
        self.planned_response: pd.DataFrame = pd.DataFrame()
        self.submitted: list[dict] = []

    def copy_rates_m1(self, pair: str, n: int) -> pd.DataFrame:
        return self.planned_response.copy() if not self.planned_response.empty else pd.DataFrame()

    def submit_market_order(self, plan):  # pragma: no cover — not exercised here
        self.submitted.append(plan)
        from ff.live.broker_mt5 import Ticket
        return Ticket(plan["plan_id"], 42, "2026-04-20T00:00Z", None, plan["entry_ref_price"], plan["size_lots"], 10009, "mock")


def _synth_m1(start: str, minutes: int, base_price: float = 1.1000) -> pd.DataFrame:
    idx = pd.date_range(start, periods=minutes, freq="1min", tz="UTC")
    return pd.DataFrame({
        "open": np.full(minutes, base_price),
        "high": np.full(minutes, base_price + 0.0002),
        "low": np.full(minutes, base_price - 0.0002),
        "close": np.full(minutes, base_price + 0.00005),
        "spread": np.full(minutes, 1.0),
        "tick_volume": np.ones(minutes),
    }, index=idx)


@pytest.fixture
def cfg():
    from ff.live.runner import LiveConfig, BrokerCfg
    return LiveConfig(
        instance_id="test_instance",
        recipe={"pair": "EUR_USD", "main_tf": "H1", "sub_tf": "M1", "level": 1},
        overrides={},
        pairs=["EUR_USD"],
        broker=BrokerCfg(login=0, password="x", server="x"),
        poll_interval_sec=10.0,
        lookback_bars=10,
    )


@pytest.fixture
def pair_state(cfg):
    # Build pair state without running complexity_to_ea for speed — this test
    # stubs signal eval anyway.
    return live_runner.PairState(
        pair="EUR_USD",
        ea={"signals": {}, "execution": {"atr_period": 14}},
        m1_buf=pd.DataFrame(),
        main_buf=pd.DataFrame(),
        last_main_ts=None,
    )


def test_poll_merges_m1_buf_and_rolls_up_to_h1(monkeypatch, cfg, pair_state):
    broker = _MockBroker()
    # Feed exactly 120 M1 bars — two complete H1 bars should surface.
    broker.planned_response = _synth_m1("2026-04-20 10:00", 120)

    # Stub signal eval so we don't need real indicators.
    monkeypatch.setattr(
        live_runner, "_evaluate_and_fire",
        lambda *a, **k: None,
    )

    live_runner._poll_pair(cfg, pair_state, broker, {"EUR_USD": pair_state})

    assert len(pair_state.m1_buf) == 120
    assert len(pair_state.main_buf) == 2  # two closed H1 bars
    assert pair_state.last_main_ts == pd.Timestamp("2026-04-20 11:00", tz="UTC")


def test_poll_deduplicates_overlapping_m1(monkeypatch, cfg, pair_state):
    broker = _MockBroker()
    monkeypatch.setattr(
        live_runner, "_evaluate_and_fire",
        lambda *a, **k: None,
    )

    broker.planned_response = _synth_m1("2026-04-20 10:00", 60)
    live_runner._poll_pair(cfg, pair_state, broker, {"EUR_USD": pair_state})
    assert len(pair_state.m1_buf) == 60

    # Overlap last 30 M1 + 30 new
    broker.planned_response = _synth_m1("2026-04-20 10:30", 60)
    live_runner._poll_pair(cfg, pair_state, broker, {"EUR_USD": pair_state})

    # 60 original + 30 new (last 30 of 2nd batch extend past original) = 90
    assert len(pair_state.m1_buf) == 90
    assert pair_state.m1_buf.index.is_monotonic_increasing
    assert pair_state.m1_buf.index.is_unique


def test_last_main_ts_only_advances_on_new_closed_bar(monkeypatch, cfg, pair_state):
    broker = _MockBroker()
    fired = []
    monkeypatch.setattr(
        live_runner, "_evaluate_and_fire",
        lambda cfg, state, broker, ts, pair_states: fired.append(ts),
    )

    # First poll: 119 M1 (only 1 H1 bar closed)
    broker.planned_response = _synth_m1("2026-04-20 10:00", 119)
    live_runner._poll_pair(cfg, pair_state, broker, {"EUR_USD": pair_state})
    assert len(fired) == 1
    assert fired[0] == pd.Timestamp("2026-04-20 10:00", tz="UTC")

    # Second poll: 120 M1 — no new H1 closed yet (11:00 H1 needs 11:00..11:59 M1 plus bar at 12:00)
    broker.planned_response = _synth_m1("2026-04-20 10:00", 120)
    live_runner._poll_pair(cfg, pair_state, broker, {"EUR_USD": pair_state})
    # One more H1 closed.
    assert len(fired) == 2

    # Third poll: no new M1 — no advance
    broker.planned_response = _synth_m1("2026-04-20 10:00", 120)
    live_runner._poll_pair(cfg, pair_state, broker, {"EUR_USD": pair_state})
    assert len(fired) == 2  # still two fires


class _StubLibrary:
    """Minimal replacement for ``signal_lib.build_signal_library`` output.

    Exposes the attributes ``_evaluate_and_fire`` reads: bar_index, variant,
    direction, entry_price, atr_pips, swing_sl, variant_map, n_signals.
    """
    def __init__(self, latest_bar_idx: int) -> None:
        self.bar_index = np.array([latest_bar_idx], dtype=np.int64)
        self.variant = np.array([7], dtype=np.int64)
        self.direction = np.array([1], dtype=np.int64)
        self.entry_price = np.array([1.10005], dtype=np.float64)
        self.atr_pips = np.array([10.0], dtype=np.float64)
        self.swing_sl = np.array([1.0995], dtype=np.float64)
        self.variant_map = [{}] * 8
        self.variant_map[7] = {"family": "ema_cross", "params": {"fast": 8, "slow": 21}}
        self.n_signals = 1
        self.n_variants = 8


def test_plan_carries_parity_fields(monkeypatch, cfg, pair_state):
    """Emitted plan includes signal_variant, signal_family, spread_at_fire_pips."""
    # Seed m1_buf + main_buf so _evaluate_and_fire has history. The 'spread'
    # column is what drives spread_at_fire_pips.
    m1 = _synth_m1("2026-04-20 10:00", 60)
    # Overwrite spread with a known value. MT5 returns spread as an
    # integer in broker POINTS (not price units) - on a modern 5-digit
    # broker 1 pip = 10 points, so 15 points = 1.5 pips. The runner's
    # plan-emission path divides by 10 to produce spread_at_fire_pips.
    m1["spread"] = 15.0
    pair_state.m1_buf = m1.copy()
    pair_state.main_buf = m1.iloc[-1:].copy()  # 1 synthetic main bar

    # Stub signal_lib to fire on the latest bar.
    from ff import signal_lib as sl
    monkeypatch.setattr(
        sl, "build_signal_library",
        lambda *a, **k: _StubLibrary(latest_bar_idx=len(pair_state.main_buf) - 1),
    )
    monkeypatch.setattr(live_runner, "_sl", sl)

    # Prevent the emitted plan hitting disk + bypass duplicate-plan check.
    emitted: list[dict] = []
    monkeypatch.setattr(live_runner, "_emit_plan", lambda _c, plan: emitted.append(plan))
    monkeypatch.setattr(live_runner, "_is_duplicate_plan", lambda _c, _pid: False)
    monkeypatch.setattr(live_runner, "_append_jsonl", lambda *a, **k: None)

    broker = _MockBroker()
    signal_bar_ts = pair_state.main_buf.index[-1]
    live_runner._evaluate_and_fire(cfg, pair_state, broker, signal_bar_ts, {"EUR_USD": pair_state})

    assert len(emitted) == 1, f"expected 1 plan, got {len(emitted)}"
    plan = emitted[0]
    assert plan["signal_variant"] == 7
    assert plan["signal_family"] == "ema_cross"
    assert plan["spread_at_fire_pips"] == pytest.approx(1.5, rel=1e-6)


def test_max_open_per_pair_blocks_stacking(monkeypatch, cfg, pair_state):
    """With cap=1 and one position already open, the next fire is
    rejected, an error row is logged, and the mock broker never sees
    a submit_market_order call."""
    m1 = _synth_m1("2026-04-20 10:00", 60)
    pair_state.m1_buf = m1.copy()
    pair_state.main_buf = m1.iloc[-1:].copy()

    # Pre-seed an open position so the pair is "full".
    pair_state.open_positions["existing-plan"] = live_runner.OpenPosition(
        plan_id="existing-plan",
        ticket=99,
        pair="EUR_USD",
        direction=1,
        entry_price=1.1000,
        sl_price=1.0990,
        tp_price=1.1020,
        opened_at="2026-04-20T09:00:00+00:00",
        size_lots=0.01,
        atr_pips_at_entry=10.0,
        last_known_sl=1.0990,
        partial_done=False,
    )

    from ff import signal_lib as sl
    monkeypatch.setattr(
        sl, "build_signal_library",
        lambda *a, **k: _StubLibrary(latest_bar_idx=len(pair_state.main_buf) - 1),
    )
    monkeypatch.setattr(live_runner, "_sl", sl)
    monkeypatch.setattr(live_runner, "_emit_plan", lambda _c, plan: None)
    monkeypatch.setattr(live_runner, "_is_duplicate_plan", lambda _c, _pid: False)

    cap_errors: list[dict] = []
    monkeypatch.setattr(
        live_runner, "_log_error",
        lambda _c, row: cap_errors.append(row) if row.get("stage") == "cap" else None,
    )
    # Ensure we can detect that submit_market_order was not invoked.
    submitted: list = []
    broker = _MockBroker()
    broker.submit_market_order = lambda plan: submitted.append(plan)  # type: ignore

    cfg.max_open_per_pair = 1
    signal_bar_ts = pair_state.main_buf.index[-1]
    live_runner._evaluate_and_fire(cfg, pair_state, broker, signal_bar_ts, {"EUR_USD": pair_state})

    assert submitted == [], "cap should have blocked the submit"
    assert len(cap_errors) == 1
    assert cap_errors[0]["open_count"] == 1
    assert cap_errors[0]["cap"] == 1


# ── Multi-instance ─────────────────────────────────────────────────────

def test_plan_id_includes_instance_id_so_same_pair_bar_does_not_collide():
    """Two instances firing the same pair on the same bar must produce
    distinct plan_ids. Without the instance_id prefix, dedup would
    collapse instance B's fire onto instance A's ticket."""
    ts = pd.Timestamp("2026-04-22T10:00:00", tz="UTC")
    p1 = live_runner._plan_id("inst_a", "EUR_USD", ts, 1)
    p2 = live_runner._plan_id("inst_b", "EUR_USD", ts, 1)
    assert p1 != p2
    assert p1.startswith("inst_a_")
    assert p2.startswith("inst_b_")


def test_run_rejects_duplicate_instance_ids():
    """Startup must fail loudly on duplicate instance_id — silent
    pair_states overwrite is hard to diagnose later."""
    from ff.live.runner import LiveConfig, BrokerCfg, run
    a = LiveConfig(
        instance_id="dup", recipe={"main_tf": "H1"}, overrides={},
        pairs=["EUR_USD"],
        broker=BrokerCfg(login=0, password="x", server="x", magic_number=100),
    )
    b = LiveConfig(
        instance_id="dup", recipe={"main_tf": "H1"}, overrides={},
        pairs=["GBP_USD"],
        broker=BrokerCfg(login=0, password="x", server="x", magic_number=101),
    )
    with pytest.raises(RuntimeError, match="duplicate instance_id"):
        run([a, b])


def test_run_rejects_duplicate_magic_numbers():
    """Duplicate magic across instances breaks MT5-side attribution."""
    from ff.live.runner import LiveConfig, BrokerCfg, run
    a = LiveConfig(
        instance_id="a", recipe={"main_tf": "H1"}, overrides={},
        pairs=["EUR_USD"],
        broker=BrokerCfg(login=0, password="x", server="x", magic_number=200),
    )
    b = LiveConfig(
        instance_id="b", recipe={"main_tf": "H1"}, overrides={},
        pairs=["GBP_USD"],
        broker=BrokerCfg(login=0, password="x", server="x", magic_number=200),
    )
    with pytest.raises(RuntimeError, match="duplicate magic_number"):
        run([a, b])


def test_dedup_scans_plans_file_not_just_tickets(tmp_path, monkeypatch):
    """A crash between _emit_plan and _append_jsonl(tickets) must not
    cause a refire on restart. Dedup checks plans too."""
    from ff.live.runner import LiveConfig, BrokerCfg, _is_duplicate_plan, _emit_plan
    cfg = LiveConfig(
        instance_id="t", recipe={"main_tf": "H1"}, overrides={},
        pairs=["EUR_USD"],
        broker=BrokerCfg(login=0, password="x", server="x"),
    )
    # Redirect LIVE_DIR to a fresh tmp path so the test does not pollute
    # artifacts/live.
    monkeypatch.setattr(live_runner, "LIVE_DIR", tmp_path)
    cfg.plans_dir.mkdir(parents=True, exist_ok=True)

    plan_id = "t_EUR_USD_2026-04-22T10:00:00+00:00_+1"
    assert _is_duplicate_plan(cfg, plan_id) is False

    _emit_plan(cfg, {"plan_id": plan_id, "pair": "EUR_USD"})
    # Tickets file still missing — would have returned False before the
    # plans-scan fix.
    assert _is_duplicate_plan(cfg, plan_id) is True
