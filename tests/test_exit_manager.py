"""Parity tests for ``ff.live.exit_manager``.

The live exit manager is a Python port of ``core/src/trade_full.rs``
(lines 165-502). These tests drive the SAME synthetic trade scenario
through both paths and assert ``exit_reason`` + ``exit_price`` match
exactly. A divergence means the port has drifted from the Rust engine
and live trades will no longer reproduce backtest outcomes.

One test per feature family:
  - trailing (fixed pip)
  - breakeven
  - chandelier
  - partial close (followed by natural SL/TP exit)

Each test keeps spread and slippage at zero so the Rust short-side
spread adjustment (``exit_price += sell_spread``) is a no-op — otherwise
the naive price comparison would need to subtract the spread back out.
"""
from __future__ import annotations

import numpy as np
import pytest

import ff_core as bc

from ff.live.exit_manager import (
    Action,
    MgmtParams,
    TRAIL_FIXED_PIP,
    compute_action,
)


# ── Engine-side param layout constants (mirror core/src/constants.rs) ─
_SL_FIXED = 0
_TP_FIXED = 2


def _build_price_series(
    n_h: int = 80,
    direction: int = 1,
    start_px: float = 1.1000,
    bar_move_pips: float = 3.0,
) -> dict:
    """Build a gentle monotone trend so a BUY rides profit (or SELL the
    reverse). Deterministic — no randomness — so exit prices pin to
    identifiable bar indices."""
    pv = 0.0001
    sub_per_h = 60
    n_m = n_h * sub_per_h

    move = bar_move_pips * pv * direction
    h_c = (start_px + np.arange(n_h) * move).astype(np.float64)
    h_h = (h_c + 0.00015).astype(np.float64)
    h_l = (h_c - 0.00015).astype(np.float64)
    h_s = np.zeros(n_h, dtype=np.float64)

    # Linear interpolation gives sub-bar opens/closes that smoothly walk
    # between H1 closes; jitter is symmetric ±0.5 pip on high/low.
    base = np.interp(np.arange(n_m), np.arange(n_h) * sub_per_h, h_c)
    m_c = base.astype(np.float64)
    m_h = (m_c + 0.00005).astype(np.float64)
    m_l = (m_c - 0.00005).astype(np.float64)
    m_s = np.zeros(n_m, dtype=np.float64)

    map_start = (np.arange(n_h) * sub_per_h).astype(np.int64)
    map_end = ((np.arange(n_h) + 1) * sub_per_h).astype(np.int64)

    return dict(h_h=h_h, h_l=h_l, h_c=h_c, h_s=h_s,
                m_h=m_h, m_l=m_l, m_c=m_c, m_s=m_s,
                map_start=map_start, map_end=map_end,
                n_h=n_h, n_m=n_m, pv=pv, sub_per_h=sub_per_h)


def _baseline_row() -> np.ndarray:
    """Single param row — SL/TP wide, all management OFF."""
    row = np.zeros(bc.NUM_PL, dtype=np.float64)
    row[bc.PL_SIGNAL_VARIANT] = 0
    row[bc.PL_SL_MODE] = _SL_FIXED
    row[bc.PL_SL_FIXED_PIPS] = 50.0
    row[bc.PL_TP_MODE] = _TP_FIXED
    row[bc.PL_TP_FIXED_PIPS] = 500.0
    row[bc.PL_HOURS_START] = 0
    row[bc.PL_HOURS_END] = 23
    row[bc.PL_DAYS_BITMASK] = 127
    row[bc.PL_BUY_FILTER_MAX] = -1
    row[bc.PL_SELL_FILTER_MIN] = -1
    return row


def _run_engine(data: dict, param_row: np.ndarray,
                entry_bar: int, direction: int, atr_pips: float):
    """Drive ``ff_core.batch_evaluate`` on one synthetic signal and
    return ``(exit_reason, exit_price)`` from the emitted trade
    record. Returns ``(None, None)`` when the engine emits no trade
    (sanity-guarded at test sites)."""
    entry_price = float(data["h_c"][entry_bar])
    n_sig = 1
    sig_bar_index = np.array([entry_bar], dtype=np.int64)
    sig_direction = np.array([direction], dtype=np.int64)
    sig_entry_price = np.array([entry_price], dtype=np.float64)
    sig_hour = np.array([12], dtype=np.int64)
    sig_day = np.array([0], dtype=np.int64)
    sig_atr_pips = np.array([atr_pips], dtype=np.float64)
    sig_swing_sl = np.array([0.0], dtype=np.float64)
    sig_filter_value = np.array([0.0], dtype=np.float64)
    sig_variant = np.array([0], dtype=np.int64)
    sig_filters = np.full((bc.NUM_SIGNAL_PARAMS, n_sig), -1, dtype=np.int64)

    param_matrix = np.stack([param_row])
    param_layout = np.arange(bc.NUM_PL, dtype=np.int64)
    metrics = np.zeros((1, bc.NUM_METRICS), dtype=np.float64)
    pnl = np.zeros((1, 1), dtype=np.float64)
    trades = np.zeros((1, 1 * bc.NUM_TRADE_FIELDS), dtype=np.float64)

    bc.batch_evaluate(
        data["h_h"], data["h_l"], data["h_c"], data["h_s"],
        data["pv"], 0.0,
        sig_bar_index, sig_direction, sig_entry_price,
        sig_hour, sig_day, sig_atr_pips,
        sig_swing_sl, sig_filter_value, sig_variant,
        sig_filters,
        param_matrix, param_layout,
        metrics,
        1, 365.0 * 24.0,
        0.0, 999.0,
        data["m_h"], data["m_l"], data["m_c"], data["m_s"],
        data["map_start"], data["map_end"],
        pnl, trades,
    )

    if int(metrics[0, 0]) == 0:
        return None, None, None
    # trade row layout: [pnl, exit_reason, direction, entry_bar,
    #                    entry_sub, entry_price, exit_bar, exit_sub,
    #                    exit_price]
    tr = trades[0, :bc.NUM_TRADE_FIELDS]
    return int(tr[1]), float(tr[8]), int(tr[4])


def _run_python(data: dict, params: MgmtParams,
                entry_bar: int) -> Action:
    """Replay M1 bars from the first sub-bar after entry_bar through
    ``compute_action``. Mirrors what the live runner does on each
    poll but with the full bar history available at once."""
    start = (entry_bar + 1) * data["sub_per_h"]
    end = data["n_m"]
    m1_bars = list(zip(
        data["m_h"][start:end], data["m_l"][start:end],
        data["m_c"][start:end], data["m_s"][start:end],
    ))
    action, _state = compute_action(
        params,
        last_known_sl=params.initial_sl,
        partial_done=False,
        m1_bars=m1_bars,
    )
    return action


# ── Tests ──────────────────────────────────────────────────────────────

def test_trailing_fixed_pip_buy_parity():
    """BUY + fixed-pip trailing. The uptrend arms the trail; when price
    later dips the trail fires. Rust and Python must agree on the
    exit price (SL value at hit) and reason (EXIT_TRAILING)."""
    data = _build_price_series(n_h=80, direction=1, bar_move_pips=4.0)
    # After the uptrend, bend prices downward so the trail bites.
    # Bars 60..79: drop 10 pips/bar.
    flip_start = 60 * data["sub_per_h"]
    peak = float(data["m_h"][flip_start - 1])
    for i in range(flip_start, data["n_m"]):
        ofs = (i - flip_start) * 0.5 * data["pv"]  # 0.5 pip per M1
        data["m_c"][i] = peak - ofs
        data["m_h"][i] = data["m_c"][i] + 0.00005
        data["m_l"][i] = data["m_c"][i] - 0.00005

    atr_pips = 10.0
    row = _baseline_row()
    row[bc.PL_TRAILING_MODE] = TRAIL_FIXED_PIP
    row[bc.PL_TRAIL_ACTIVATE] = 5.0
    row[bc.PL_TRAIL_DISTANCE] = 8.0

    entry_bar = 5
    entry_price = float(data["h_c"][entry_bar])
    rust_reason, rust_exit, _ = _run_engine(data, row, entry_bar, 1, atr_pips)
    assert rust_reason is not None, "engine emitted no trade"

    params = MgmtParams(
        direction=1, actual_entry=entry_price,
        initial_sl=entry_price - 50.0 * data["pv"],
        tp_price=entry_price + 500.0 * data["pv"],
        atr_pips=atr_pips, pip_value=data["pv"],
        slippage_pips=0.0,
        trailing_mode=TRAIL_FIXED_PIP,
        trail_activate_pips=5.0, trail_distance_pips=8.0,
    )
    action = _run_python(data, params, entry_bar)

    assert action.kind == "close", action
    assert action.exit_reason == rust_reason, (action.exit_reason, rust_reason)
    assert abs(action.exit_price - rust_exit) < 1e-9, (action.exit_price, rust_exit)


def test_breakeven_buy_parity():
    """BUY + breakeven lock. Price pushes past trigger → BE arms →
    price later dips below entry → exit fires at BE price with
    EXIT_BREAKEVEN."""
    data = _build_price_series(n_h=80, direction=1, bar_move_pips=4.0)
    # Drop past entry after bar 40.
    flip_start = 40 * data["sub_per_h"]
    peak = float(data["m_h"][flip_start - 1])
    for i in range(flip_start, data["n_m"]):
        ofs = (i - flip_start) * 0.5 * data["pv"]
        data["m_c"][i] = peak - ofs
        data["m_h"][i] = data["m_c"][i] + 0.00005
        data["m_l"][i] = data["m_c"][i] - 0.00005

    atr_pips = 10.0
    row = _baseline_row()
    row[bc.PL_BREAKEVEN_ENABLED] = 1
    row[bc.PL_BREAKEVEN_TRIGGER] = 5.0
    row[bc.PL_BREAKEVEN_OFFSET] = 1.0

    entry_bar = 5
    entry_price = float(data["h_c"][entry_bar])
    rust_reason, rust_exit, _ = _run_engine(data, row, entry_bar, 1, atr_pips)
    assert rust_reason is not None, "engine emitted no trade"

    params = MgmtParams(
        direction=1, actual_entry=entry_price,
        initial_sl=entry_price - 50.0 * data["pv"],
        tp_price=entry_price + 500.0 * data["pv"],
        atr_pips=atr_pips, pip_value=data["pv"],
        breakeven_enabled=1,
        breakeven_trigger_pips=5.0, breakeven_offset_pips=1.0,
    )
    action = _run_python(data, params, entry_bar)

    assert action.kind == "close", action
    assert action.exit_reason == rust_reason, (action.exit_reason, rust_reason)
    assert abs(action.exit_price - rust_exit) < 1e-9, (action.exit_price, rust_exit)


def test_chandelier_buy_parity():
    """BUY + chandelier stop. Rallies to a peak, then retraces beyond
    chandelier_atr_mult*atr from peak → EXIT_CHANDELIER at the peak-
    anchored SL."""
    data = _build_price_series(n_h=80, direction=1, bar_move_pips=4.0)
    # After bar 50, retrace 0.6 pips per M1 to pull back through the chand SL.
    flip_start = 50 * data["sub_per_h"]
    peak = float(data["m_h"][flip_start - 1])
    for i in range(flip_start, data["n_m"]):
        ofs = (i - flip_start) * 0.6 * data["pv"]
        data["m_c"][i] = peak - ofs
        data["m_h"][i] = data["m_c"][i] + 0.00005
        data["m_l"][i] = data["m_c"][i] - 0.00005

    atr_pips = 10.0
    row = _baseline_row()
    row[bc.PL_CHANDELIER_ENABLED] = 1
    row[bc.PL_CHANDELIER_ACTIVATE] = 1.0
    row[bc.PL_CHANDELIER_ATR_MULT] = 1.5

    entry_bar = 5
    entry_price = float(data["h_c"][entry_bar])
    rust_reason, rust_exit, _ = _run_engine(data, row, entry_bar, 1, atr_pips)
    assert rust_reason is not None, "engine emitted no trade"

    params = MgmtParams(
        direction=1, actual_entry=entry_price,
        initial_sl=entry_price - 50.0 * data["pv"],
        tp_price=entry_price + 500.0 * data["pv"],
        atr_pips=atr_pips, pip_value=data["pv"],
        chandelier_enabled=1,
        chandelier_activate_pips=1.0,
        chandelier_atr_mult=1.5,
    )
    action = _run_python(data, params, entry_bar)

    assert action.kind == "close", action
    assert action.exit_reason == rust_reason, (action.exit_reason, rust_reason)
    assert abs(action.exit_price - rust_exit) < 1e-9, (action.exit_price, rust_exit)


def test_partial_then_sl_buy_parity():
    """BUY + partial close. Partial triggers mid-rally, then a sharp
    reversal hits the original SL. Rust's trade record surfaces the
    terminal SL exit — the partial is rolled into ``realized_pnl``
    and is invisible in ``exit_reason``. The Python replay must end
    on the same terminal exit with the same price."""
    data = _build_price_series(n_h=80, direction=1, bar_move_pips=4.0)
    # Rally to bar 20, then crash through SL.
    crash_start = 20 * data["sub_per_h"]
    peak = float(data["m_h"][crash_start - 1])
    for i in range(crash_start, data["n_m"]):
        ofs = (i - crash_start) * 2.0 * data["pv"]  # 2 pips/M1 → fast
        data["m_c"][i] = peak - ofs
        data["m_h"][i] = data["m_c"][i] + 0.00005
        data["m_l"][i] = data["m_c"][i] - 0.00005

    atr_pips = 10.0
    row = _baseline_row()
    row[bc.PL_PARTIAL_ENABLED] = 1
    row[bc.PL_PARTIAL_PCT] = 50.0
    row[bc.PL_PARTIAL_TRIGGER] = 5.0

    entry_bar = 5
    entry_price = float(data["h_c"][entry_bar])
    rust_reason, rust_exit, _ = _run_engine(data, row, entry_bar, 1, atr_pips)
    assert rust_reason is not None, "engine emitted no trade"

    params = MgmtParams(
        direction=1, actual_entry=entry_price,
        initial_sl=entry_price - 50.0 * data["pv"],
        tp_price=entry_price + 500.0 * data["pv"],
        atr_pips=atr_pips, pip_value=data["pv"],
        partial_enabled=1, partial_pct=50.0, partial_trigger_pips=5.0,
    )
    action = _run_python(data, params, entry_bar)

    assert action.kind == "close", action
    assert action.exit_reason == rust_reason, (action.exit_reason, rust_reason)
    assert abs(action.exit_price - rust_exit) < 1e-9, (action.exit_price, rust_exit)
