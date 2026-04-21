"""Live runner — evaluate the backtest signal pipeline on live IC Markets M1.

Poll `mt5.copy_rates_from_pos` every `poll_interval_sec`, detect new closed M1
bars, roll them up into the main-TF buffer, and on each main-TF bar close
invoke `ff.signal_lib.build_signal_library` to decide whether to fire. Plans
go to `artifacts/live/plans/<YYYY-MM-DD>.jsonl`; orders get routed via
`ff.live.broker_mt5.MT5Broker`.

The runner is deliberately a plain class backed by a single thread + a
cooperative `stop_event`. No subprocess, no asyncio — the web layer wraps
this in a subprocess in `app.live_jobs` so uvicorn reloads don't kill it.

MT5 is a Windows-only dependency; imports are lazy so dev boxes can import
`ff.live.runner` without the `MetaTrader5` package installed. Only `run()`
requires it.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Thread
from typing import Any

import numpy as np
import pandas as pd

from ff import signal_lib as _sl
from ff.defaults.complexity import complexity_to_ea
from ff.defaults.overrides import apply_overrides


LOG = logging.getLogger(__name__)


# ── Timeframe rollup ──────────────────────────────────────────────────

TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D": 1440}


def _rollup_main_tf(m1_df: pd.DataFrame, main_tf_minutes: int) -> pd.DataFrame:
    """Resample M1 bars to a coarser TF. Drops the last (in-progress) bar —
    only fully-closed bars flow into the signal pipeline.
    """
    if m1_df.empty:
        return m1_df
    rule = f"{main_tf_minutes}min"
    agg = m1_df.resample(rule, label="left", closed="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "spread": "mean",
    }).dropna()
    # Trim the final bar if its right-edge hasn't been reached yet: require
    # the M1 buffer to extend at least `main_tf_minutes - 1` bars past the
    # start of the last main-TF bar.
    if not agg.empty:
        last_ts = agg.index[-1]
        required_end = last_ts + pd.Timedelta(minutes=main_tf_minutes - 1)
        if m1_df.index.max() < required_end:
            agg = agg.iloc[:-1]
    return agg


# ── Paths ──────────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parent.parent.parent
LIVE_DIR = _ROOT / "artifacts" / "live"
PLANS_DIR = LIVE_DIR / "plans"
STATE_FILE = LIVE_DIR / "state.json"
PARAMS_PINNED_FILE = LIVE_DIR / "params_pinned.json"
ERRORS_FILE = LIVE_DIR / "errors.jsonl"
TICKETS_FILE = LIVE_DIR / "tickets.jsonl"


# ── Config ─────────────────────────────────────────────────────────────

@dataclass
class BrokerCfg:
    login: int
    password: str
    server: str
    terminal_path: str | None = None
    deviation_pips: float = 3.0
    magic_number: int = 20260420
    symbol_map: dict[str, str] = field(default_factory=dict)


@dataclass
class LiveConfig:
    """Input contract for a live-runner session.

    ``recipe`` + ``overrides`` share shape with the web UI's ``POST /api/run``
    body so the same complexity_to_ea → apply_overrides path builds the EA.
    ``pairs`` is applied on top of the recipe's pair — the recipe's pair is
    the reference for parameter calibration but the live loop trades across
    all of ``pairs`` with the same override set.
    """

    recipe: dict[str, Any]
    overrides: dict[str, Any]
    pairs: list[str]
    broker: BrokerCfg
    poll_interval_sec: float = 1.0
    lookback_bars: int = 500
    size_lots: float = 0.01

    def pair_recipe(self, pair: str) -> dict[str, Any]:
        """Recipe scoped to a single pair — used per-pair EA build."""
        r = dict(self.recipe)
        r["pair"] = pair
        return r


# ── Per-pair runtime state ─────────────────────────────────────────────

@dataclass
class OpenPosition:
    plan_id: str
    ticket: int
    pair: str
    direction: int
    entry_price: float
    sl_price: float
    tp_price: float
    opened_at: str
    size_lots: float


@dataclass
class PairState:
    pair: str
    ea: dict[str, Any]
    m1_buf: pd.DataFrame
    main_buf: pd.DataFrame
    last_main_ts: pd.Timestamp | None
    open_positions: dict[str, OpenPosition] = field(default_factory=dict)


# ── Persistence ────────────────────────────────────────────────────────

def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, default=str, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _log_error(row: dict[str, Any]) -> None:
    row = {"ts": pd.Timestamp.utcnow().isoformat(), **row}
    _append_jsonl(ERRORS_FILE, row)


# ── Plan emission ──────────────────────────────────────────────────────

def _plan_id(pair: str, signal_bar_ts: pd.Timestamp, direction: int) -> str:
    return f"{pair}_{signal_bar_ts.isoformat()}_{direction:+d}"


def _today_plans_file() -> Path:
    return PLANS_DIR / f"{pd.Timestamp.utcnow().strftime('%Y-%m-%d')}.jsonl"


def _emit_plan(plan: dict[str, Any]) -> None:
    _append_jsonl(_today_plans_file(), plan)


# ── Runner skeleton ────────────────────────────────────────────────────

def _build_pair_state(pair: str, cfg: LiveConfig) -> PairState:
    ea = complexity_to_ea(cfg.pair_recipe(pair), cfg.recipe.get("level", 1))
    ea = apply_overrides(ea, cfg.overrides)
    return PairState(
        pair=pair,
        ea=ea,
        m1_buf=pd.DataFrame(),
        main_buf=pd.DataFrame(),
        last_main_ts=None,
    )


def run(cfg: LiveConfig, stop_event: Event | None = None) -> None:
    """Enter the live loop. Blocking; returns when ``stop_event`` fires.

    Raises ``RuntimeError`` if MT5 connect fails — caller (Scheduled Task)
    is expected to restart the process.
    """
    # Local import: MT5 is Windows-only and not on dev boxes.
    from ff.live.broker_mt5 import MT5Broker

    if stop_event is None:
        stop_event = Event()

    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    PLANS_DIR.mkdir(parents=True, exist_ok=True)

    broker = MT5Broker(cfg.broker)
    broker.connect()
    LOG.info("[live] MT5 connected: login=%s server=%s", cfg.broker.login, cfg.broker.server)

    pair_states: dict[str, PairState] = {p: _build_pair_state(p, cfg) for p in cfg.pairs}
    # Expose to the plan-emission path so _persist_state can capture all pairs.
    global _pair_states_cache
    _pair_states_cache = pair_states
    LOG.info("[live] initialised %d pair states: %s", len(pair_states), cfg.pairs)

    hb = _spawn_heartbeat(pair_states, stop_event)
    ar = _spawn_auto_reconciler(stop_event)
    try:
        _main_loop(cfg, pair_states, broker, stop_event)
    finally:
        stop_event.set()
        hb.join(timeout=2.0)
        ar.join(timeout=2.0)
        broker.disconnect()
        LOG.info("[live] runner stopped cleanly")


def _spawn_heartbeat(pair_states: dict[str, PairState], stop_event: Event) -> Thread:
    def _loop() -> None:
        started = time.monotonic()
        while not stop_event.wait(30.0):
            uptime = time.monotonic() - started
            open_n = sum(len(ps.open_positions) for ps in pair_states.values())
            LOG.info("[live] tick uptime=%.0fs pairs=%d open=%d",
                     uptime, len(pair_states), open_n)

    t = Thread(target=_loop, daemon=True, name="live-heartbeat")
    t.start()
    return t


def _spawn_auto_reconciler(stop_event: Event) -> Thread:
    """Run the reconciler every hour (configurable) against the pinned
    source run. No-op if no ``pinned_run.json`` exists.
    """
    def _loop() -> None:
        interval_min = _read_auto_reconcile_interval_min()
        # Wait a grace period after startup so at least one plan can fire.
        if stop_event.wait(60.0):
            return
        while not stop_event.wait(interval_min * 60.0):
            try:
                _run_auto_reconcile()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("[live] auto-reconcile failed: %r", exc)

    t = Thread(target=_loop, daemon=True, name="live-auto-reconcile")
    t.start()
    return t


def _read_auto_reconcile_interval_min() -> int:
    svc_cfg = LIVE_DIR / "service_config.json"
    if not svc_cfg.exists():
        return 60
    try:
        return int(json.loads(svc_cfg.read_text(encoding="utf-8")).get("auto_reconcile_interval_min", 60))
    except (json.JSONDecodeError, ValueError, TypeError):
        return 60


def _run_auto_reconcile() -> None:
    """Load pinned backtest + today's plans + tickets + recent MT5 deals, run
    the reconciler, write an HTML+JSON report. Also drops a ``latest.html``
    alias the UI iframe points at.
    """
    pinned = LIVE_DIR / "pinned_run.json"
    if not pinned.exists():
        return
    run_id = json.loads(pinned.read_text(encoding="utf-8")).get("run_id")
    if not run_id:
        return

    from ff.live import reconcile as _recon
    import numpy as _np

    run_file = _ROOT_RUNS / f"{run_id}.npz"
    if not run_file.exists():
        LOG.warning("[live] pinned run missing: %s", run_file)
        return

    z = _np.load(run_file, allow_pickle=True)
    if "trades" not in z.files:
        return
    bt = pd.DataFrame(z["trades"])
    bt["entry_ts"] = pd.to_datetime(bt["entry_ts"], utc=True)
    bt["exit_ts"] = pd.to_datetime(bt["exit_ts"], utc=True)
    # Single-pair for v1 — extend once we capture pair on each backtest trade row.
    svc = json.loads((LIVE_DIR / "service_config.json").read_text(encoding="utf-8"))
    bt["pair"] = svc.get("recipe", {}).get("pair") or (svc.get("pairs") or ["EUR_USD"])[0]

    # TODO: ingest MT5 deals for live side. For v1 we reconcile plans only
    # (matched = plan exists, not plan filled-at-price).
    live_df = pd.DataFrame([])

    report = _recon.reconcile(bt, live_df)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    html_path, _ = _recon.write_report(report, LIVE_DIR / "reconcile", stamp)
    (LIVE_DIR / "reconcile" / "latest.html").write_bytes(html_path.read_bytes())
    LOG.info("[live] auto-reconcile wrote %s counts=%s", html_path.name, report.counts)


_ROOT_RUNS = Path(__file__).resolve().parent.parent.parent / "artifacts" / "runs"


def _main_loop(
    cfg: LiveConfig,
    pair_states: dict[str, PairState],
    broker: Any,
    stop_event: Event,
) -> None:
    """Outer poll loop. One pass iterates all pairs then sleeps.

    This is the smallest viable implementation — enough to wire Phase B.2
    (bar ingestion + signal eval) onto. Signal evaluation and order routing
    are stubbed with TODO markers until their dedicated tasks land.
    """
    while not stop_event.is_set():
        tick_start = time.monotonic()
        for pair, state in pair_states.items():
            try:
                _poll_pair(cfg, state, broker)
            except Exception as exc:  # noqa: BLE001 — main loop must not die
                _log_error({"pair": pair, "error": repr(exc)})
                LOG.exception("[live] %s poll failed", pair)
        elapsed = time.monotonic() - tick_start
        sleep_for = max(0.5, cfg.poll_interval_sec - elapsed)
        if stop_event.wait(sleep_for):
            return


def _poll_pair(cfg: LiveConfig, state: PairState, broker: Any) -> None:
    """One poll for one pair. Ingest bars → evaluate → maybe fire a plan."""
    # Lookback window includes enough M1 to cover the longest signal period
    # rolled up to main-TF, plus a buffer. Fetching a few hundred M1 bars is
    # cheap locally and avoids managing an incremental cursor.
    main_tf = cfg.recipe.get("main_tf", "H1")
    main_tf_min = TF_MINUTES[main_tf]
    fetch_m1_n = int(cfg.lookback_bars * main_tf_min + main_tf_min * 2)
    m1_new = broker.copy_rates_m1(state.pair, fetch_m1_n)
    if m1_new.empty:
        return

    # Merge into rolling buffer (deduplicate on index).
    if state.m1_buf.empty:
        state.m1_buf = m1_new
    else:
        state.m1_buf = (
            pd.concat([state.m1_buf, m1_new])
              .loc[lambda d: ~d.index.duplicated(keep="last")]
              .sort_index()
        )
    # Trim older than the lookback window we actually need.
    max_keep = cfg.lookback_bars * main_tf_min + main_tf_min * 4
    if len(state.m1_buf) > max_keep:
        state.m1_buf = state.m1_buf.iloc[-max_keep:]

    # Roll up to main TF.
    state.main_buf = _rollup_main_tf(state.m1_buf, main_tf_min)
    if state.main_buf.empty:
        return

    latest_main_ts = state.main_buf.index[-1]
    if state.last_main_ts is not None and latest_main_ts <= state.last_main_ts:
        # No new main-TF bar has closed since last poll — nothing to decide.
        return
    state.last_main_ts = latest_main_ts

    _evaluate_and_fire(cfg, state, broker, latest_main_ts)


def _evaluate_and_fire(
    cfg: LiveConfig,
    state: PairState,
    broker: Any,
    signal_bar_ts: pd.Timestamp,
) -> None:
    """Run signal_lib on the trailing main-TF window, and if any variant fires
    on the latest bar, emit a plan + submit the order."""
    pip_value = _pip_value_for_pair(state.pair)
    ea = state.ea
    try:
        lib = _sl.build_signal_library(
            ea["signals"], state.main_buf,
            pip_value=pip_value,
            atr_period=ea.get("execution", {}).get("atr_period", 14),
            use_cache=False,
        )
    except Exception as exc:  # noqa: BLE001 — one pair must not kill the runner
        _log_error({"pair": state.pair, "stage": "signal_lib", "error": repr(exc)})
        return

    if lib.n_signals == 0:
        return

    # Did any variant fire on the latest bar?
    latest_bar_idx = len(state.main_buf) - 1
    hits = np.where(lib.bar_index == latest_bar_idx)[0]
    if hits.size == 0:
        return

    # Fire first matching signal only — multi-fire on the same bar is rare,
    # and the live loop is single-trade-per-signal-bar.
    si = int(hits[0])
    direction = int(lib.direction[si])
    entry_ref_price = float(lib.entry_price[si])
    atr_pips = float(lib.atr_pips[si])

    sl_price, tp_price = _compute_sl_tp_live(ea, direction, entry_ref_price, atr_pips, pip_value)

    plan_id = _plan_id(state.pair, signal_bar_ts, direction)
    if _is_duplicate_plan(plan_id):
        LOG.info("[live] %s duplicate plan_id=%s — skipping", state.pair, plan_id)
        return

    plan = {
        "plan_id": plan_id,
        "created_at_ts": pd.Timestamp.now("UTC").isoformat(),
        "pair": state.pair,
        "main_tf": cfg.recipe.get("main_tf", "H1"),
        "sub_tf": cfg.recipe.get("sub_tf", "M1"),
        "signal_bar_ts": signal_bar_ts.isoformat(),
        "fired_at_ts": pd.Timestamp.now("UTC").isoformat(),
        "direction": direction,
        "entry_ref_price": entry_ref_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "size_lots": cfg.size_lots,
    }
    _emit_plan(plan)
    LOG.info("[live] %s fired %s sl=%.5f tp=%.5f", state.pair, plan_id, sl_price, tp_price)

    try:
        ticket = broker.submit_market_order(plan)
    except Exception as exc:  # noqa: BLE001
        _log_error({"pair": state.pair, "stage": "submit", "plan_id": plan_id,
                    "error": repr(exc)})
        return

    _append_jsonl(TICKETS_FILE, {
        "plan_id": plan_id,
        "ticket": ticket.ticket,
        "submitted_at": ticket.submitted_at,
        "filled_at": ticket.filled_at,
        "fill_price": ticket.fill_price,
        "fill_volume": ticket.fill_volume,
        "retcode": ticket.retcode,
        "comment": ticket.comment,
    })
    state.open_positions[plan_id] = OpenPosition(
        plan_id=plan_id,
        ticket=ticket.ticket,
        pair=state.pair,
        direction=direction,
        entry_price=ticket.fill_price,
        sl_price=sl_price,
        tp_price=tp_price,
        opened_at=ticket.submitted_at,
        size_lots=cfg.size_lots,
    )
    _persist_state({p: st.open_positions for p, st in _pair_states_cache.items()})


def _pip_value_for_pair(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def _compute_sl_tp_live(
    ea: dict,
    direction: int,
    entry_price: float,
    atr_pips: float,
    pip_value: float,
) -> tuple[float, float]:
    """Mirror of ``ff.sl_tp.compute_sl_tp`` for the live side.

    Only covers the two most common modes — ATR-distance SL and RR-ratio TP —
    because that's what the calibrated high-trade-count presets will use.
    More modes land when the calibration script needs them.
    """
    sl_cfg = ea.get("stop_loss", {})
    sl_mode = sl_cfg.get("selector", "atr")
    if sl_mode == "atr":
        sl_mult = sl_cfg.get("atr", {}).get("mult", 1.5)
        sl_pips = atr_pips * sl_mult
    elif sl_mode == "fixed":
        sl_pips = sl_cfg.get("fixed", {}).get("pips", 20.0)
    else:
        sl_pips = atr_pips * 1.5

    tp_cfg = ea.get("take_profit", {})
    tp_mode = tp_cfg.get("selector", "rr")
    if tp_mode == "rr":
        rr = tp_cfg.get("rr", {}).get("ratio", 1.5)
        tp_pips = sl_pips * rr
    elif tp_mode == "atr":
        tp_pips = atr_pips * tp_cfg.get("atr", {}).get("mult", 2.0)
    elif tp_mode == "fixed":
        tp_pips = tp_cfg.get("fixed", {}).get("pips", 30.0)
    else:
        tp_pips = sl_pips * 1.5

    if direction > 0:
        return entry_price - sl_pips * pip_value, entry_price + tp_pips * pip_value
    return entry_price + sl_pips * pip_value, entry_price - tp_pips * pip_value


def _is_duplicate_plan(plan_id: str) -> bool:
    if not TICKETS_FILE.exists():
        return False
    for line in TICKETS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            if json.loads(line).get("plan_id") == plan_id:
                return True
        except json.JSONDecodeError:
            continue
    return False


# Exposed so `_main_loop` can persist through `_persist_state`.
_pair_states_cache: dict[str, PairState] = {}


def _persist_state(open_positions_by_pair: dict[str, dict[str, OpenPosition]]) -> None:
    payload = {
        pair: {plan_id: op.__dict__ for plan_id, op in openmap.items()}
        for pair, openmap in open_positions_by_pair.items()
    }
    _atomic_write_json(STATE_FILE, payload)


# ── Helpers for tests ─────────────────────────────────────────────────

def _load_pinned_params() -> dict[str, Any] | None:
    if PARAMS_PINNED_FILE.exists():
        return json.loads(PARAMS_PINNED_FILE.read_text(encoding="utf-8"))
    return None


def _save_pinned_params(params: dict[str, Any]) -> None:
    _atomic_write_json(PARAMS_PINNED_FILE, params)
