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

    live_runner._poll_pair(cfg, pair_state, broker)

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
    live_runner._poll_pair(cfg, pair_state, broker)
    assert len(pair_state.m1_buf) == 60

    # Overlap last 30 M1 + 30 new
    broker.planned_response = _synth_m1("2026-04-20 10:30", 60)
    live_runner._poll_pair(cfg, pair_state, broker)

    # 60 original + 30 new (last 30 of 2nd batch extend past original) = 90
    assert len(pair_state.m1_buf) == 90
    assert pair_state.m1_buf.index.is_monotonic_increasing
    assert pair_state.m1_buf.index.is_unique


def test_last_main_ts_only_advances_on_new_closed_bar(monkeypatch, cfg, pair_state):
    broker = _MockBroker()
    fired = []
    monkeypatch.setattr(
        live_runner, "_evaluate_and_fire",
        lambda cfg, state, broker, ts: fired.append(ts),
    )

    # First poll: 119 M1 (only 1 H1 bar closed)
    broker.planned_response = _synth_m1("2026-04-20 10:00", 119)
    live_runner._poll_pair(cfg, pair_state, broker)
    assert len(fired) == 1
    assert fired[0] == pd.Timestamp("2026-04-20 10:00", tz="UTC")

    # Second poll: 120 M1 — no new H1 closed yet (11:00 H1 needs 11:00..11:59 M1 plus bar at 12:00)
    broker.planned_response = _synth_m1("2026-04-20 10:00", 120)
    live_runner._poll_pair(cfg, pair_state, broker)
    # One more H1 closed.
    assert len(fired) == 2

    # Third poll: no new M1 — no advance
    broker.planned_response = _synth_m1("2026-04-20 10:00", 120)
    live_runner._poll_pair(cfg, pair_state, broker)
    assert len(fired) == 2  # still two fires
