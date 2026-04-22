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
# Per-instance paths hang off LiveConfig.artifact_root (see below).
# LIVE_DIR is kept for shared files only: runner.log, crashes.jsonl,
# instances.json, archive/.


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

    One LiveConfig = one strategy INSTANCE. The runner can drive many
    LiveConfigs concurrently; they share one MT5 terminal but each owns
    a unique ``instance_id``, ``broker.magic_number``, and artifact
    subdir (``artifacts/live/<instance_id>/``).

    ``recipe`` + ``overrides`` share shape with the web UI's ``POST /api/run``
    body so the same complexity_to_ea → apply_overrides path builds the EA.
    ``pairs`` is applied on top of the recipe's pair — the recipe's pair is
    the reference for parameter calibration but the live loop trades across
    all of ``pairs`` with the same override set.

    ``max_open_per_pair`` caps simultaneous open positions per symbol.
    Without it the runner fires on every main-TF bar close and positions
    stack — a parity hazard and risk hazard both.
    """

    instance_id: str
    recipe: dict[str, Any]
    overrides: dict[str, Any]
    pairs: list[str]
    broker: BrokerCfg
    poll_interval_sec: float = 1.0
    lookback_bars: int = 500
    size_lots: float = 0.01
    best_trial: dict[str, Any] | None = None
    max_open_per_pair: int = 1

    def pair_recipe(self, pair: str) -> dict[str, Any]:
        """Recipe scoped to a single pair — used per-pair EA build."""
        r = dict(self.recipe)
        r["pair"] = pair
        return r

    # Per-instance artifact paths. All runtime state that could collide
    # between instances lives under ``artifact_root``.
    @property
    def artifact_root(self) -> Path:
        return LIVE_DIR / self.instance_id

    @property
    def plans_dir(self) -> Path:
        return self.artifact_root / "plans"

    @property
    def state_file(self) -> Path:
        return self.artifact_root / "state.json"

    @property
    def tickets_file(self) -> Path:
        return self.artifact_root / "tickets.jsonl"

    @property
    def errors_file(self) -> Path:
        return self.artifact_root / "errors.jsonl"

    @property
    def pinned_run_file(self) -> Path:
        return self.artifact_root / "pinned_run.json"

    @property
    def reconcile_dir(self) -> Path:
        return self.artifact_root / "reconcile"

    @property
    def params_pinned_file(self) -> Path:
        return self.artifact_root / "params_pinned.json"


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
    # Trade-management state used by ff.live.exit_manager. The replay is
    # idempotent — it walks every M1 since entry — so chandelier peak /
    # BE-locked / trailing-active flags are reconstructed each poll and
    # do not need persisting. Only the two observer fields do:
    #   last_known_sl : the SL the broker currently holds, so a modify is
    #     only emitted when the replay lands on a different value.
    #   partial_done  : the partial fires once; persisted so a restart
    #     does not re-trigger it.
    atr_pips_at_entry: float = 0.0
    last_known_sl: float = 0.0
    partial_done: bool = False


@dataclass
class PairState:
    pair: str
    ea: dict[str, Any]
    m1_buf: pd.DataFrame
    main_buf: pd.DataFrame
    last_main_ts: pd.Timestamp | None
    open_positions: dict[str, OpenPosition] = field(default_factory=dict)
    best_trial: dict[str, Any] | None = None


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


def _log_error(cfg: LiveConfig, row: dict[str, Any]) -> None:
    row = {"ts": pd.Timestamp.utcnow().isoformat(),
           "instance_id": cfg.instance_id, **row}
    _append_jsonl(cfg.errors_file, row)


# ── Plan emission ──────────────────────────────────────────────────────

def _plan_id(instance_id: str, pair: str, signal_bar_ts: pd.Timestamp,
             direction: int) -> str:
    """Include instance_id so two instances on the same pair/bar don't
    collapse under the dedup check."""
    return f"{instance_id}_{pair}_{signal_bar_ts.isoformat()}_{direction:+d}"


def _today_plans_file(cfg: LiveConfig) -> Path:
    return cfg.plans_dir / f"{pd.Timestamp.utcnow().strftime('%Y-%m-%d')}.jsonl"


def _emit_plan(cfg: LiveConfig, plan: dict[str, Any]) -> None:
    _append_jsonl(_today_plans_file(cfg), plan)


# ── Runner skeleton ────────────────────────────────────────────────────

def _build_pair_state(pair: str, cfg: LiveConfig) -> PairState:
    recipe = cfg.pair_recipe(pair)
    ea = complexity_to_ea(
        level=int(recipe.get("level", 1)),
        pair=recipe["pair"],
        main_tf=recipe["main_tf"],
        sub_tf=recipe.get("sub_tf"),
        name=recipe.get("name"),
    )
    ea = apply_overrides(ea, cfg.overrides)
    return PairState(
        pair=pair,
        ea=ea,
        m1_buf=pd.DataFrame(),
        main_buf=pd.DataFrame(),
        last_main_ts=None,
        best_trial=cfg.best_trial,
    )


def run(instances: "list[LiveConfig] | LiveConfig",
        stop_event: Event | None = None) -> None:
    """Enter the live loop across N instances. Blocking; returns when
    ``stop_event`` fires.

    ``instances`` accepts either a single LiveConfig (legacy callers) or
    a list. Internally always normalised to a list. One MT5 terminal is
    shared — broker creds come from ``instances[0].broker`` and each
    instance's magic_number rides on every submit call via its own
    ``broker.magic_number``.

    Raises ``RuntimeError`` if MT5 connect fails — caller (Scheduled Task)
    is expected to restart the process.
    """
    # Local import: MT5 is Windows-only and not on dev boxes.
    from ff.live.broker_mt5 import MT5Broker

    if isinstance(instances, LiveConfig):
        instances = [instances]
    if not instances:
        LOG.error("[live] run() called with empty instances list")
        return

    # Fail fast on duplicate identity — silent overwrite of pair_states
    # or attribution collisions in MT5 are harder to diagnose later than
    # a startup crash.
    seen_ids: set[str] = set()
    seen_magics: set[int] = set()
    for cfg in instances:
        if cfg.instance_id in seen_ids:
            raise RuntimeError(
                f"[live] duplicate instance_id: {cfg.instance_id!r}")
        seen_ids.add(cfg.instance_id)
        m = int(cfg.broker.magic_number)
        if m in seen_magics:
            raise RuntimeError(
                f"[live] duplicate magic_number {m} across instances")
        seen_magics.add(m)

    if stop_event is None:
        stop_event = Event()

    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    for cfg in instances:
        cfg.plans_dir.mkdir(parents=True, exist_ok=True)

    # One broker for the whole process. First instance's creds drive the
    # connection; each fire picks its own magic via per-call BrokerCfg swap.
    shared_broker_cfg = instances[0].broker
    broker = MT5Broker(shared_broker_cfg)
    broker.connect()
    LOG.info("[live] MT5 connected: login=%s server=%s",
             shared_broker_cfg.login, shared_broker_cfg.server)

    all_pair_states: dict[str, dict[str, PairState]] = {}
    for cfg in instances:
        ps = {p: _build_pair_state(p, cfg) for p in cfg.pairs}
        all_pair_states[cfg.instance_id] = ps
        LOG.info("[live] instance=%s initialised %d pair states: %s",
                 cfg.instance_id, len(ps), cfg.pairs)

    hb = _spawn_heartbeat(all_pair_states, stop_event)
    ar = _spawn_auto_reconciler(instances, stop_event)
    # Push plans/tickets/state.json to the remote ``live-state`` branch so
    # the laptop's Restart shortcut can fetch them and populate the parity
    # workbench. Best-effort — failures are logged and swallowed.
    _spawn_state_sync(stop_event)
    try:
        _main_loop(instances, all_pair_states, broker, stop_event)
    finally:
        stop_event.set()
        hb.join(timeout=2.0)
        ar.join(timeout=2.0)
        broker.disconnect()
        LOG.info("[live] runner stopped cleanly")


def _spawn_heartbeat(
    all_pair_states: dict[str, dict[str, PairState]], stop_event: Event,
) -> Thread:
    def _loop() -> None:
        started = time.monotonic()
        while not stop_event.wait(30.0):
            uptime = time.monotonic() - started
            n_instances = len(all_pair_states)
            total_pairs = sum(len(ps) for ps in all_pair_states.values())
            open_n = sum(
                len(state.open_positions)
                for ps in all_pair_states.values()
                for state in ps.values()
            )
            LOG.info("[live] tick uptime=%.0fs instances=%d pairs=%d open=%d",
                     uptime, n_instances, total_pairs, open_n)

    t = Thread(target=_loop, daemon=True, name="live-heartbeat")
    t.start()
    return t


def _spawn_auto_reconciler(
    instances: "list[LiveConfig]", stop_event: Event,
) -> Thread:
    """Run the reconciler every hour (configurable) against each
    instance's pinned source run. No-op for any instance without a
    ``pinned_run.json``.
    """
    def _loop() -> None:
        interval_min = _read_auto_reconcile_interval_min()
        # Wait a grace period after startup so at least one plan can fire.
        if stop_event.wait(60.0):
            return
        while not stop_event.wait(interval_min * 60.0):
            for cfg in instances:
                try:
                    _run_auto_reconcile(cfg)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("[live] auto-reconcile %s failed: %r",
                                cfg.instance_id, exc)

    t = Thread(target=_loop, daemon=True, name="live-auto-reconcile")
    t.start()
    return t


def _read_auto_reconcile_interval_min() -> int:
    """Global knob — if any instance config or legacy service_config.json
    sets ``auto_reconcile_interval_min``, use the smallest. Default 60.
    """
    candidates: list[int] = []
    # Legacy top-level config (pre-multi-instance) still honoured if present.
    legacy = LIVE_DIR / "service_config.json"
    if legacy.exists():
        try:
            v = int(json.loads(legacy.read_text(encoding="utf-8"))
                    .get("auto_reconcile_interval_min", 60))
            candidates.append(v)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    for sub in LIVE_DIR.glob("*/config.json"):
        try:
            v = int(json.loads(sub.read_text(encoding="utf-8"))
                    .get("auto_reconcile_interval_min", 60))
            candidates.append(v)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return min(candidates) if candidates else 60


def _spawn_state_sync(stop_event: Event) -> Thread:
    """Start the live→remote state sync thread.

    Reads ``state_sync_interval_sec`` from any instance config or the
    legacy top-level service_config (default 60s). Set it to 0 in every
    config to disable the sync entirely — useful on laptops where the
    runner is being tested without a remote worktree configured.
    """
    from ff.live import state_sync as _ss

    candidates: list[int] = []
    legacy = LIVE_DIR / "service_config.json"
    if legacy.exists():
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8")).get(
                "state_sync_interval_sec", 60)
            candidates.append(int(raw) if raw else 0)
        except (json.JSONDecodeError, ValueError, TypeError):
            candidates.append(60)
    for sub in LIVE_DIR.glob("*/config.json"):
        try:
            raw = json.loads(sub.read_text(encoding="utf-8")).get(
                "state_sync_interval_sec", 60)
            candidates.append(int(raw) if raw else 0)
        except (json.JSONDecodeError, ValueError, TypeError):
            candidates.append(60)
    # If any instance wants it enabled, honour the smallest non-zero.
    non_zero = [c for c in candidates if c > 0]
    interval = min(non_zero) if non_zero else 0

    if interval <= 0:
        LOG.info("[live] state_sync disabled (interval=%s)", interval)
        # Return an already-dead thread so the run() cleanup doesn't care.
        dead = Thread(target=lambda: None, daemon=True, name="live-state-sync-disabled")
        dead.start()
        return dead

    def _loop() -> None:
        LOG.info("[live] state_sync thread running (every %ds)", interval)
        consecutive_failures = 0
        while not stop_event.wait(interval):
            try:
                if _ss.snapshot_and_push():
                    LOG.info("[live] state_sync pushed snapshot")
                consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                LOG.warning("[live] state_sync iteration failed: %r", exc)
                # Write to a shared state_sync error log at the top level
                # since it's a process-global concern, not per-instance.
                _append_jsonl(LIVE_DIR / "state_sync_errors.jsonl", {
                    "ts": pd.Timestamp.utcnow().isoformat(),
                    "stage": "state_sync",
                    "consecutive_failures": consecutive_failures,
                    "error": repr(exc),
                })

    t = Thread(target=_loop, daemon=True, name="live-state-sync")
    t.start()
    return t


def _run_auto_reconcile(cfg: LiveConfig) -> None:
    """Load pinned backtest + today's plans + tickets + recent MT5 deals for
    one instance, run the reconciler, write HTML+JSON to the instance's
    ``reconcile/`` subdir. Also drops a ``latest.html`` alias the UI iframe
    points at.
    """
    pinned = cfg.pinned_run_file
    if not pinned.exists():
        return
    run_id = json.loads(pinned.read_text(encoding="utf-8")).get("run_id")
    if not run_id:
        return

    from ff.live import reconcile as _recon
    import numpy as _np

    run_file = _ROOT_RUNS / f"{run_id}.npz"
    if not run_file.exists():
        LOG.warning("[live] instance=%s pinned run missing: %s",
                    cfg.instance_id, run_file)
        return

    z = _np.load(run_file, allow_pickle=True)
    if "trades" not in z.files:
        return
    bt = pd.DataFrame(z["trades"])
    bt["entry_ts"] = pd.to_datetime(bt["entry_ts"], utc=True)
    bt["exit_ts"] = pd.to_datetime(bt["exit_ts"], utc=True)
    if "pair" not in bt.columns:
        bt["pair"] = cfg.recipe.get("pair") or (cfg.pairs or ["EUR_USD"])[0]

    # TODO: ingest MT5 deals for live side. For v1 we reconcile plans only.
    live_df = pd.DataFrame([])

    report = _recon.reconcile(bt, live_df)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    cfg.reconcile_dir.mkdir(parents=True, exist_ok=True)
    html_path, _ = _recon.write_report(report, cfg.reconcile_dir, stamp)
    (cfg.reconcile_dir / "latest.html").write_bytes(html_path.read_bytes())
    LOG.info("[live] instance=%s auto-reconcile wrote %s counts=%s",
             cfg.instance_id, html_path.name, report.counts)


_ROOT_RUNS = Path(__file__).resolve().parent.parent.parent / "artifacts" / "runs"


def _main_loop(
    instances: "list[LiveConfig]",
    all_pair_states: dict[str, dict[str, PairState]],
    broker: Any,
    stop_event: Event,
) -> None:
    """Outer poll loop. One pass iterates every instance's every pair
    then sleeps. Shared MT5 broker; each fire tags magic from its own
    instance's BrokerCfg.
    """
    # Use the smallest poll interval across instances so the fastest
    # one still respects its own cadence.
    poll_interval = min(cfg.poll_interval_sec for cfg in instances)
    while not stop_event.is_set():
        tick_start = time.monotonic()
        for cfg in instances:
            pair_states = all_pair_states[cfg.instance_id]
            for pair, state in pair_states.items():
                try:
                    _poll_pair(cfg, state, broker, pair_states)
                except Exception as exc:  # noqa: BLE001 — main loop must not die
                    _log_error(cfg, {"pair": pair, "error": repr(exc)})
                    LOG.exception("[live] instance=%s %s poll failed",
                                  cfg.instance_id, pair)
        elapsed = time.monotonic() - tick_start
        sleep_for = max(0.5, poll_interval - elapsed)
        if stop_event.wait(sleep_for):
            return


def _poll_pair(cfg: LiveConfig, state: PairState, broker: Any,
               pair_states: dict[str, PairState]) -> None:
    """One poll for one pair. Ingest bars → evaluate → maybe fire a plan.

    ``pair_states`` is the instance's full pair-state dict — passed in so
    ``_persist_state`` can snapshot every open-position map for this
    instance's state.json. No module-level cache needed.
    """
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
    main_bar_closed = state.last_main_ts is None or latest_main_ts > state.last_main_ts
    if main_bar_closed:
        state.last_main_ts = latest_main_ts
        _evaluate_and_fire(cfg, state, broker, latest_main_ts, pair_states)

    # Trade management runs every poll, not only on main-TF bar closes —
    # trailing / breakeven / chandelier advance on every M1 close.
    _manage_open_positions(cfg, state, broker, pair_states)


def _evaluate_and_fire(
    cfg: LiveConfig,
    state: PairState,
    broker: Any,
    signal_bar_ts: pd.Timestamp,
    pair_states: dict[str, PairState],
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
        _log_error(cfg, {"pair": state.pair, "stage": "signal_lib", "error": repr(exc)})
        return

    if lib.n_signals == 0:
        return

    # Did any variant fire on the latest bar?
    latest_bar_idx = len(state.main_buf) - 1
    hits = np.where(lib.bar_index == latest_bar_idx)[0]
    if hits.size == 0:
        return

    # Parity: when a frozen best_trial is active, restrict hits to the variant
    # the backtest picked. Without this, live would fire whatever variant
    # sorted first in the library — a different signal than the backtest.
    if state.best_trial is not None:
        frozen_variant = state.best_trial.get("signal_variant")
        if frozen_variant is not None:
            hits = hits[lib.variant[hits] == int(frozen_variant)]
            if hits.size == 0:
                return

    # Fire first matching signal only — multi-fire on the same bar is rare,
    # and the live loop is single-trade-per-signal-bar.
    si = int(hits[0])
    direction = int(lib.direction[si])
    entry_ref_price = float(lib.entry_price[si])
    atr_pips = float(lib.atr_pips[si])
    variant_id = int(lib.variant[si])
    variant_info = (
        lib.variant_map[variant_id]
        if variant_id < len(lib.variant_map) else {}
    )
    signal_family = str(variant_info.get("family", ""))

    # Spread at fire time: MT5's rates.spread is in POINTS (integer,
    # broker-defined smallest price increment), NOT price units. On a
    # modern 5-digit / 3-digit-JPY broker like IC Markets, 1 pip = 10
    # points universally, so dividing by 10 yields pips. The earlier
    # code divided by ``pip_value`` (0.0001) and surfaced 50000-pip
    # nonsense in the plan - see 2026-04-21 audit.
    spread_at_fire_pips = 0.0
    try:
        if state.m1_buf is not None and len(state.m1_buf) > 0 \
                and "spread" in state.m1_buf.columns:
            spread_at_fire_pips = float(
                state.m1_buf["spread"].iloc[-1]
            ) / 10.0
    except Exception:  # pragma: no cover - defensive: never block a fire on telemetry
        spread_at_fire_pips = 0.0

    sl_price, tp_price = _compute_sl_tp_live(
        ea, direction, entry_ref_price, atr_pips, pip_value,
        best_trial=state.best_trial,
    )

    plan_id = _plan_id(cfg.instance_id, state.pair, signal_bar_ts, direction)
    if _is_duplicate_plan(cfg, plan_id):
        LOG.info("[live] instance=%s %s duplicate plan_id=%s — skipping",
                 cfg.instance_id, state.pair, plan_id)
        return

    # Per-pair open-position cap. Stops the runner from stacking positions
    # on every bar close — the VPS saw 29 concurrent EUR/USD positions in
    # one morning before this guard landed.
    if cfg.max_open_per_pair and len(state.open_positions) >= cfg.max_open_per_pair:
        _log_error(cfg, {
            "pair": state.pair, "stage": "cap",
            "plan_id": plan_id,
            "open_count": len(state.open_positions),
            "cap": cfg.max_open_per_pair,
        })
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
        # Parity fields — let the reconciler compare fire-time broker state
        # against what the engine saw in backtest.
        "signal_variant": variant_id,
        "signal_family": signal_family,
        "spread_at_fire_pips": spread_at_fire_pips,
    }
    _emit_plan(cfg, plan)
    LOG.info("[live] instance=%s %s fired %s sl=%.5f tp=%.5f",
             cfg.instance_id, state.pair, plan_id, sl_price, tp_price)

    # Swap broker cfg so this instance's magic_number is attached to the
    # MT5 request. See ``_with_broker_cfg`` for the shared helper used by
    # the management calls too.
    try:
        ticket = _with_broker_cfg(broker, cfg,
            lambda: broker.submit_market_order(plan))
    except Exception as exc:  # noqa: BLE001
        _log_error(cfg, {"pair": state.pair, "stage": "submit",
                         "plan_id": plan_id, "error": repr(exc)})
        return

    _append_jsonl(cfg.tickets_file, {
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
        atr_pips_at_entry=atr_pips,
        last_known_sl=sl_price,
        partial_done=False,
    )
    _persist_state(cfg, {p: st.open_positions for p, st in pair_states.items()})


def _pip_value_for_pair(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def _compute_sl_tp_live(
    ea: dict,
    direction: int,
    entry_price: float,
    atr_pips: float,
    pip_value: float,
    best_trial: dict | None = None,
) -> tuple[float, float]:
    """SL/TP for a fired live plan. When ``best_trial`` is provided, use its
    frozen ``engine.stop_loss`` / ``engine.take_profit`` dicts — the SAME
    param vector the backtest ran. Falls back to EA schema defaults only
    when no frozen trial exists (legacy code path).
    """
    if best_trial is not None:
        eng = best_trial.get("engine", {})
        sl_cfg = eng.get("stop_loss", {}) or {}
        tp_cfg = eng.get("take_profit", {}) or {}
        sl_mode = sl_cfg.get("selector", "atr")
        if sl_mode == "fixed":
            sl_pips = float(sl_cfg.get("fixed", {}).get("pips", 20.0))
        elif sl_mode == "atr":
            sl_pips = atr_pips * float(sl_cfg.get("atr", {}).get("mult", 1.5))
        else:
            sl_pips = atr_pips * 1.5

        tp_mode = tp_cfg.get("selector", "rr")
        if tp_mode == "rr":
            tp_pips = sl_pips * float(tp_cfg.get("rr", {}).get("ratio", 1.5))
        elif tp_mode == "atr":
            tp_pips = atr_pips * float(tp_cfg.get("atr", {}).get("mult", 2.0))
        elif tp_mode == "fixed":
            tp_pips = float(tp_cfg.get("fixed", {}).get("pips", 30.0))
        else:
            tp_pips = sl_pips * 1.5

        if direction == 1:
            return entry_price - sl_pips * pip_value, entry_price + tp_pips * pip_value
        return entry_price + sl_pips * pip_value, entry_price - tp_pips * pip_value

    return _compute_sl_tp_live_legacy(
        ea, direction, entry_price, atr_pips, pip_value,
    )


def _compute_sl_tp_live_legacy(
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


def _with_broker_cfg(broker: Any, cfg: LiveConfig, fn):
    """Call ``fn()`` with ``broker.cfg`` temporarily swapped to this
    instance's ``cfg.broker`` so order requests carry its magic. Single-
    threaded main loop — no race. Guarded for test mock brokers that
    don't expose ``cfg``.
    """
    has_cfg = hasattr(broker, "cfg")
    prev = broker.cfg if has_cfg else None
    if has_cfg:
        broker.cfg = cfg.broker
    try:
        return fn()
    finally:
        if has_cfg:
            broker.cfg = prev


def _manage_open_positions(cfg: LiveConfig, state: PairState, broker: Any,
                            pair_states: dict[str, PairState]) -> None:
    """Replay each open position through ``ff.live.exit_manager`` and
    dispatch at most one broker action per position per poll.

    Only runs when a frozen ``best_trial`` is loaded — without it we have
    no management parameters to honour and the position sits on its
    original SL/TP (legacy path). With a trial, every M1 since entry is
    replayed through the same state machine the Rust backtest uses; the
    final action (``close`` / ``partial_close`` / ``modify_sl`` / ``hold``)
    is what the runner dispatches here.
    """
    from ff.live import exit_manager

    if not state.open_positions or state.best_trial is None:
        return
    if state.m1_buf is None or state.m1_buf.empty:
        return

    pv = _pip_value_for_pair(state.pair)
    closed: list[str] = []
    mutated = False

    for plan_id, pos in list(state.open_positions.items()):
        try:
            opened = pd.Timestamp(pos.opened_at)
            if opened.tzinfo is None:
                opened = opened.tz_localize("UTC")
        except (TypeError, ValueError):
            continue
        bars = state.m1_buf[state.m1_buf.index > opened]
        if bars.empty:
            continue

        params = exit_manager.params_from_trial(
            state.best_trial,
            direction=pos.direction,
            actual_entry=pos.entry_price,
            initial_sl=pos.sl_price,
            tp_price=pos.tp_price,
            atr_pips=pos.atr_pips_at_entry,
            pip_value=pv,
            slippage_pips=0.0,
        )

        m1_iter: list[tuple[float, float, float, float]] = []
        for ts, row in bars.iterrows():
            spread = float(row.get("spread", 0.0))
            if spread != spread:  # NaN guard
                spread = 0.0
            m1_iter.append((
                float(row["high"]), float(row["low"]),
                float(row["close"]), spread,
            ))

        action, _ = exit_manager.compute_action(
            params,
            last_known_sl=pos.last_known_sl or pos.sl_price,
            partial_done=pos.partial_done,
            m1_bars=m1_iter,
        )

        if action.kind == "hold":
            continue
        if action.kind == "modify_sl":
            try:
                rc = _with_broker_cfg(broker, cfg,
                    lambda: broker.modify_sl(pos.ticket, action.new_sl))
            except Exception as exc:  # noqa: BLE001
                _log_error(cfg, {"pair": state.pair, "stage": "modify_sl",
                            "ticket": pos.ticket, "error": repr(exc)})
                continue
            if rc == 10009:  # MT5 TRADE_RETCODE_DONE
                pos.last_known_sl = action.new_sl
                mutated = True
            else:
                _log_error(cfg, {"pair": state.pair, "stage": "modify_sl",
                            "ticket": pos.ticket, "retcode": rc,
                            "new_sl": action.new_sl})
        elif action.kind == "partial_close":
            try:
                rc = _with_broker_cfg(broker, cfg,
                    lambda: broker.partial_close(pos.ticket, action.partial_pct))
            except Exception as exc:  # noqa: BLE001
                _log_error(cfg, {"pair": state.pair, "stage": "partial_close",
                            "ticket": pos.ticket, "error": repr(exc)})
                continue
            if rc == 10009:
                pos.partial_done = True
                mutated = True
            else:
                _log_error(cfg, {"pair": state.pair, "stage": "partial_close",
                            "ticket": pos.ticket, "retcode": rc,
                            "pct": action.partial_pct})
        elif action.kind == "close":
            try:
                rc = _with_broker_cfg(broker, cfg,
                    lambda: broker.close_position(pos.ticket, reason="engine"))
            except Exception as exc:  # noqa: BLE001
                _log_error(cfg, {"pair": state.pair, "stage": "close",
                            "ticket": pos.ticket, "error": repr(exc)})
                continue
            if rc == 10009:
                closed.append(plan_id)
                mutated = True
            else:
                _log_error(cfg, {"pair": state.pair, "stage": "close",
                            "ticket": pos.ticket, "retcode": rc,
                            "exit_reason": action.exit_reason})

    for plan_id in closed:
        state.open_positions.pop(plan_id, None)

    if mutated:
        _persist_state(cfg, {p: st.open_positions for p, st in pair_states.items()})


def _is_duplicate_plan(cfg: LiveConfig, plan_id: str) -> bool:
    """Dedup scoped to one instance. Checks BOTH the plans log and the
    tickets log — a crash between plan-emit and ticket-append would
    otherwise let a restart refire the same plan. Per-instance scope,
    so two instances firing the same pair on the same bar (distinct
    plan_ids) do not collapse.
    """
    def _scan(path: Path) -> bool:
        if not path.exists():
            return False
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("plan_id") == plan_id:
                        return True
                except json.JSONDecodeError:
                    continue
        except OSError:
            return False
        return False

    # Scan today's plans file plus yesterday's (in case a crash straddled
    # UTC midnight and the plan was written to yesterday's file).
    today = pd.Timestamp.utcnow()
    for ts in (today, today - pd.Timedelta(days=1)):
        pf = cfg.plans_dir / f"{ts.strftime('%Y-%m-%d')}.jsonl"
        if _scan(pf):
            return True
    return _scan(cfg.tickets_file)


def _persist_state(cfg: LiveConfig,
                   open_positions_by_pair: dict[str, dict[str, OpenPosition]]
                   ) -> None:
    payload = {
        pair: {plan_id: op.__dict__ for plan_id, op in openmap.items()}
        for pair, openmap in open_positions_by_pair.items()
    }
    _atomic_write_json(cfg.state_file, payload)


# ── Helpers for tests ─────────────────────────────────────────────────

def _load_pinned_params(cfg: LiveConfig) -> dict[str, Any] | None:
    path = cfg.params_pinned_file
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _save_pinned_params(cfg: LiveConfig, params: dict[str, Any]) -> None:
    _atomic_write_json(cfg.params_pinned_file, params)
