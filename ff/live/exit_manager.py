"""Python port of the per-sub-bar management loop from
``core/src/trade_full.rs`` (lines 165-502).

The live runner opens a market order with a static SL/TP, but the Rust
engine runs trailing stops, breakeven moves, chandelier stops and partial
closes on every M1 sub-bar. Without this module, any trial whose best
params use those features would silently diverge live-side — the live
position would sit with its original SL while the backtest closed on a
tightened trail. That is the "trades don't match" symptom.

Scope: trailing, breakeven, chandelier, partial close. Stale / session /
max_bars are time-based and gated by ``ff.live.parity_guard`` — deploys
that use them are refused until this module is extended to cover them.

Source of truth is the Rust file. Every line-numbered comment below
points back into ``trade_full.rs`` so drift is obvious. The parity
tests in ``tests/test_exit_manager.py`` drive the same scenarios
through both ``ff_core.batch_evaluate`` and this module and assert
``exit_reason + exit_price`` match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# Constants mirror core/src/constants.rs. Kept local so the live process
# does not import ff_core (Rust wheel) at runtime.
DIR_BUY = 1
DIR_SELL = -1

TRAIL_OFF = 0
TRAIL_FIXED_PIP = 1
TRAIL_ATR_CHANDELIER = 2

EXIT_NONE = 0
EXIT_SL = 1
EXIT_TP = 2
EXIT_TRAILING = 3
EXIT_BREAKEVEN = 4
EXIT_MAX_BARS = 5
EXIT_STALE = 6
EXIT_CHANDELIER = 7


@dataclass
class MgmtParams:
    """Frozen trial parameters needed to replay the management loop.

    ``initial_sl`` is the SL the position opened with (what MT5 holds as
    its stop). ``actual_entry`` is the spread+slippage-adjusted reference
    price — for live, the broker's fill_price is already adjusted so
    callers can pass it directly.
    """

    direction: int
    actual_entry: float
    initial_sl: float
    tp_price: float
    atr_pips: float
    pip_value: float
    slippage_pips: float = 0.0
    trailing_mode: int = 0
    trail_activate_pips: float = 0.0
    trail_distance_pips: float = 0.0
    trail_atr_mult: float = 0.0
    breakeven_enabled: int = 0
    breakeven_trigger_pips: float = 0.0
    breakeven_offset_pips: float = 0.0
    partial_enabled: int = 0
    partial_pct: float = 0.0
    partial_trigger_pips: float = 0.0
    chandelier_enabled: int = 0
    chandelier_activate_pips: float = 0.0
    chandelier_atr_mult: float = 0.0


@dataclass
class MgmtState:
    """Mirrors the local vars inside ``simulate_trade_full``. A fresh
    instance is built at the start of each replay — the loop is pure, no
    cross-poll state leakage."""

    current_sl: float
    partial_done: bool = False
    be_locked: bool = False
    trailing_active: bool = False
    chandelier_active: bool = False
    chandelier_peak_high: float = 0.0
    chandelier_trough_low: float = 0.0


@dataclass
class Action:
    """What the runner should do this poll. At most one per call."""

    kind: str  # "hold" | "modify_sl" | "partial_close" | "close"
    new_sl: float | None = None
    partial_pct: float | None = None
    exit_reason: int | None = None
    exit_price: float | None = None  # surfaced for tests; also used as close price


# ── Inner: one sub-bar advance ─────────────────────────────────────────


def _step_sub_bar(
    params: MgmtParams,
    state: MgmtState,
    sb_high: float,
    sb_low: float,
    sb_close: float,
    sb_spread: float,
) -> Action | None:
    """Advance the state machine by one M1 sub-bar.

    Mirrors the inner ``for sb in sub_start..sub_end`` loop in
    ``trade_full.rs`` (lines 169-501). Mutates ``state`` in place like
    the Rust local vars. Returns a terminal ``Action`` (close) if the
    position exits on this sub-bar, otherwise ``None``.

    Pending SL updates from this sub-bar are committed into ``state``
    BEFORE returning (unless a terminal exit fires first). In the Rust
    loop the commit happens at the top of the next iteration — the
    effect is identical because the SL check runs against the
    pre-commit ``current_sl``.
    """
    is_buy = params.direction == DIR_BUY
    pv = params.pip_value

    # Current floating PnL (trade_full.rs:187-197) — pips from entry using
    # high on the profitable side, low on the losing side.
    if is_buy:
        float_pnl_pips = (sb_high - params.actual_entry) / pv
    else:
        float_pnl_pips = (params.actual_entry - sb_low) / pv

    # Deferred SL pattern (trade_full.rs:105-109). Changes stage into
    # `pending_*` then commit at end of the sub-bar so the SL check runs
    # against the pre-update SL, matching Rust semantics exactly.
    pending_sl = -1.0
    pending_be_locked = False
    pending_trailing_active = False
    pending_chandelier_active = False
    has_pending_update = False

    # ── Breakeven lock (trade_full.rs:210-229) ─────────────────────────
    if params.breakeven_enabled > 0 and not state.be_locked and not pending_be_locked:
        if float_pnl_pips >= params.breakeven_trigger_pips:
            if is_buy:
                be_price = params.actual_entry + params.breakeven_offset_pips * pv
                accept = be_price > state.current_sl and be_price < sb_close
            else:
                be_price = params.actual_entry - params.breakeven_offset_pips * pv
                accept = be_price < state.current_sl and be_price > sb_close
            if accept:
                pending_sl = be_price
                pending_be_locked = True
                pending_trailing_active = state.trailing_active
                has_pending_update = True

    # ── Trailing stop (trade_full.rs:232-310) ──────────────────────────
    if params.trailing_mode != TRAIL_OFF:
        # Activation
        if not state.trailing_active and not pending_trailing_active:
            if float_pnl_pips >= params.trail_activate_pips:
                pending_trailing_active = True
                if params.trailing_mode == TRAIL_FIXED_PIP:
                    trail_dist = params.trail_distance_pips * pv
                else:  # TRAIL_ATR_CHANDELIER
                    trail_dist = params.trail_atr_mult * params.atr_pips * pv
                if is_buy:
                    new_sl = sb_high - trail_dist
                    effective_sl = pending_sl if has_pending_update and pending_sl > 0.0 else state.current_sl
                    if new_sl > effective_sl and new_sl < sb_close:
                        pending_sl = new_sl
                else:
                    new_sl = sb_low + trail_dist
                    effective_sl = pending_sl if has_pending_update and pending_sl > 0.0 else state.current_sl
                    if new_sl < effective_sl and new_sl > sb_close:
                        pending_sl = new_sl
                if not has_pending_update:
                    pending_be_locked = state.be_locked
                has_pending_update = True

        # Ongoing trail
        if state.trailing_active:
            if params.trailing_mode == TRAIL_FIXED_PIP:
                trail_dist = params.trail_distance_pips * pv
            else:
                trail_dist = params.trail_atr_mult * params.atr_pips * pv
            if is_buy:
                new_sl = sb_high - trail_dist
                effective_sl = pending_sl if has_pending_update and pending_sl > 0.0 else state.current_sl
                if new_sl > effective_sl and new_sl < sb_close:
                    pending_sl = new_sl
                    if not has_pending_update:
                        pending_be_locked = state.be_locked
                    pending_trailing_active = True
                    has_pending_update = True
            else:
                new_sl = sb_low + trail_dist
                effective_sl = pending_sl if has_pending_update and pending_sl > 0.0 else state.current_sl
                if new_sl < effective_sl and new_sl > sb_close:
                    pending_sl = new_sl
                    if not has_pending_update:
                        pending_be_locked = state.be_locked
                    pending_trailing_active = True
                    has_pending_update = True

    # ── Chandelier stop (trade_full.rs:321-392) ────────────────────────
    if params.chandelier_enabled != 0 and params.chandelier_atr_mult > 0.0 and params.chandelier_activate_pips >= 0.0:
        # Track peak/trough every sub-bar regardless of arming.
        if is_buy:
            if sb_high > state.chandelier_peak_high:
                state.chandelier_peak_high = sb_high
        else:
            if sb_low < state.chandelier_trough_low:
                state.chandelier_trough_low = sb_low

        armed_now = state.chandelier_active or pending_chandelier_active or float_pnl_pips >= params.chandelier_activate_pips
        if armed_now:
            chand_dist = params.chandelier_atr_mult * params.atr_pips * pv
            if is_buy:
                new_sl = state.chandelier_peak_high - chand_dist
                effective_sl = pending_sl if has_pending_update and pending_sl > 0.0 else state.current_sl
                if new_sl > effective_sl and new_sl < sb_low:
                    pending_sl = new_sl
                    if not has_pending_update:
                        pending_be_locked = state.be_locked
                        pending_trailing_active = state.trailing_active
                    pending_chandelier_active = True
                    has_pending_update = True
                elif not has_pending_update:
                    pending_chandelier_active = (
                        pending_chandelier_active or state.chandelier_active or float_pnl_pips >= params.chandelier_activate_pips
                    )
                    if pending_chandelier_active:
                        pending_be_locked = state.be_locked
                        pending_trailing_active = state.trailing_active
                        has_pending_update = True
                else:
                    pending_chandelier_active = True
            else:
                new_sl = state.chandelier_trough_low + chand_dist
                effective_sl = pending_sl if has_pending_update and pending_sl > 0.0 else state.current_sl
                if new_sl < effective_sl and new_sl > sb_high:
                    pending_sl = new_sl
                    if not has_pending_update:
                        pending_be_locked = state.be_locked
                        pending_trailing_active = state.trailing_active
                    pending_chandelier_active = True
                    has_pending_update = True
                elif not has_pending_update:
                    pending_chandelier_active = (
                        pending_chandelier_active or state.chandelier_active or float_pnl_pips >= params.chandelier_activate_pips
                    )
                    if pending_chandelier_active:
                        pending_be_locked = state.be_locked
                        pending_trailing_active = state.trailing_active
                        has_pending_update = True
                else:
                    pending_chandelier_active = True

    # ── Partial close (trade_full.rs:406-442) ──────────────────────────
    # Immediate, not deferred. Runs BEFORE the SL check in Rust (same here).
    if params.partial_enabled > 0 and not state.partial_done:
        if float_pnl_pips >= params.partial_trigger_pips:
            if is_buy:
                tp_pips_from_entry = (params.tp_price - params.actual_entry) / pv
                tp_reachable = sb_high >= params.tp_price
            else:
                tp_pips_from_entry = (params.actual_entry - params.tp_price) / pv
                tp_reachable = sb_low <= params.tp_price
            tp_has_priority = tp_reachable and tp_pips_from_entry < params.partial_trigger_pips
            if not tp_has_priority:
                state.partial_done = True

    # ── SL check on current_sl, not pending (trade_full.rs:444-501) ────
    if is_buy:
        if sb_low <= state.current_sl:
            reason = _sl_exit_reason(state)
            return Action(kind="close", exit_reason=reason, exit_price=state.current_sl)
        if sb_high >= params.tp_price:
            return Action(kind="close", exit_reason=EXIT_TP, exit_price=params.tp_price)
    else:
        if sb_high >= state.current_sl:
            reason = _sl_exit_reason(state)
            return Action(kind="close", exit_reason=reason, exit_price=state.current_sl)
        if sb_low <= params.tp_price:
            return Action(kind="close", exit_reason=EXIT_TP, exit_price=params.tp_price)

    # Commit pending at end of sub-bar. In Rust this happens at the top
    # of the next iteration; equivalent because the SL check above runs
    # against pre-commit state.
    if has_pending_update:
        if pending_sl > 0.0:
            state.current_sl = pending_sl
        state.be_locked = pending_be_locked
        state.trailing_active = pending_trailing_active
        state.chandelier_active = pending_chandelier_active

    return None


def _sl_exit_reason(state: MgmtState) -> int:
    """SL-hit reason order mirrors trade_full.rs:448-456."""
    if state.chandelier_active:
        return EXIT_CHANDELIER
    if state.trailing_active:
        return EXIT_TRAILING
    if state.be_locked:
        return EXIT_BREAKEVEN
    return EXIT_SL


# ── Outer: replay since entry, return single action ────────────────────


def compute_action(
    params: MgmtParams,
    last_known_sl: float,
    partial_done: bool,
    m1_bars: Iterable[tuple[float, float, float, float]],
) -> tuple[Action, MgmtState]:
    """Replay every M1 bar since entry through the state machine and
    return the single action the runner should dispatch this poll.

    ``m1_bars`` yields ``(high, low, close, spread)`` in chronological
    order, starting at the first M1 bar AFTER the entry bar.

    ``last_known_sl`` is the SL currently on the broker; a modify_sl
    action is only emitted if the replay lands on a different value.

    ``partial_done`` is the persisted flag on the OpenPosition; a
    partial_close is only emitted if the replay newly triggers one.

    Priority when the replay finishes without a terminal exit:
    ``partial_close`` > ``modify_sl`` > ``hold``.
    """
    state = MgmtState(
        current_sl=params.initial_sl,
        partial_done=partial_done,
        chandelier_peak_high=params.actual_entry,
        chandelier_trough_low=params.actual_entry,
    )

    for h, l, c, s in m1_bars:
        terminal = _step_sub_bar(params, state, h, l, c, s)
        if terminal is not None:
            return terminal, state

    # No terminal exit. Resolve to at most one non-terminal action.
    if state.partial_done and not partial_done:
        return (
            Action(kind="partial_close", partial_pct=params.partial_pct / 100.0),
            state,
        )
    if abs(state.current_sl - last_known_sl) > 1e-9:
        return Action(kind="modify_sl", new_sl=state.current_sl), state
    return Action(kind="hold"), state


# ── Trial → MgmtParams glue ────────────────────────────────────────────


def params_from_trial(
    trial: dict,
    direction: int,
    actual_entry: float,
    initial_sl: float,
    tp_price: float,
    atr_pips: float,
    pip_value: float,
    slippage_pips: float = 0.0,
) -> MgmtParams:
    """Build ``MgmtParams`` from a frozen ``best_trial.engine`` dict.

    Trial shape (see ``ff.defaults.complexity`` for the schema that
    generates these)::

        engine:
          trailing:
            test: bool                         # on/off
            when_on:
              activate: float                  # pips
              mode:
                selector: "fixed" | "atr"
                fixed: {distance: float}       # pips
                atr:   {mult:     float}
          breakeven:
            test: bool
            when_on:
              trigger: float                   # pips
              offset:  float                   # pips
          partial:
            test: bool
            when_on:
              trigger: float                   # pips
              pct:     float                   # percent of position
          chandelier:
            test: bool
            when_on:
              activate: float                  # pips
              atr_mult: float

    The group's on/off lives at ``group.test`` (NOT ``group.when_on.test``
    — that was the 2026-04-21 bug that let stale/session/max_bars trials
    deploy live, and silently zeroed every management knob in the replay).
    """
    eng = (trial or {}).get("engine", {}) or {}

    trailing = eng.get("trailing") or {}
    breakeven = eng.get("breakeven") or {}
    partial = eng.get("partial") or {}
    chandelier = eng.get("chandelier") or {}

    trail_on = bool(trailing.get("test", False))
    be_on = bool(breakeven.get("test", False))
    partial_on = bool(partial.get("test", False))
    chand_on = bool(chandelier.get("test", False))

    trail_mode = 0
    trail_activate = 0.0
    trail_distance = 0.0
    trail_atr_mult = 0.0
    if trail_on:
        trail_wo = trailing.get("when_on") or {}
        trail_activate = float(trail_wo.get("activate", 0.0))
        mode = trail_wo.get("mode") or {}
        sel = mode.get("selector", "fixed")
        if sel == "fixed":
            trail_mode = TRAIL_FIXED_PIP
            trail_distance = float((mode.get("fixed") or {}).get("distance", 0.0))
        else:  # atr
            trail_mode = TRAIL_ATR_CHANDELIER
            trail_atr_mult = float((mode.get("atr") or {}).get("mult", 0.0))

    be_wo = (breakeven.get("when_on") or {}) if be_on else {}
    partial_wo = (partial.get("when_on") or {}) if partial_on else {}
    chand_wo = (chandelier.get("when_on") or {}) if chand_on else {}

    return MgmtParams(
        direction=direction,
        actual_entry=actual_entry,
        initial_sl=initial_sl,
        tp_price=tp_price,
        atr_pips=atr_pips,
        pip_value=pip_value,
        slippage_pips=slippage_pips,
        trailing_mode=trail_mode,
        trail_activate_pips=trail_activate,
        trail_distance_pips=trail_distance,
        trail_atr_mult=trail_atr_mult,
        breakeven_enabled=1 if be_on else 0,
        breakeven_trigger_pips=float(be_wo.get("trigger", 0.0)),
        breakeven_offset_pips=float(be_wo.get("offset", 0.0)),
        partial_enabled=1 if partial_on else 0,
        partial_pct=float(partial_wo.get("pct", 0.0)),
        partial_trigger_pips=float(partial_wo.get("trigger", 0.0)),
        chandelier_enabled=1 if chand_on else 0,
        chandelier_activate_pips=float(chand_wo.get("activate", 0.0)),
        chandelier_atr_mult=float(chand_wo.get("atr_mult", 0.0)),
    )
