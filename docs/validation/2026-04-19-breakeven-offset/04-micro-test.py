"""Micro-test for breakeven.offset (Phase 4 of validate-forex-knob).

Six hand-calculated scenarios from `docs/validation/2026-04-19-breakeven-offset/
03-behaviour-table.md`. Each scenario pins a specific synthetic OHLC path,
runs ff_core.batch_evaluate for exactly one trial, and asserts the resulting
PnL against a hand-calculated expected value.

Expected state as of 2026-04-19:
    - rows 1, 3 (long-normal and long-negative-offset) PASS — BE works.
    - rows 2, 3, 5 FAIL — the engine writes an SL above current price
      for a long (below for a short) and exits for +offset pips. This is
      the main bug this validation run was designed to detect.

After the Rust fix lands (adding a side-of-price guard at
core/src/trade_full.rs:180 and :185) rows 2, 3, 5 should start passing
with PnL values that reflect the new behaviour (either the trade continues
to TP, or exits on the original SL, etc). This test is the guardrail.
"""

from __future__ import annotations

import ff_core as bc
import numpy as np
import pytest

# ── Constants (match core/src/constants.rs) ──────────────────────────

DIR_BUY = 1
DIR_SELL = -1

_SL_FIXED_PIPS_MODE = 0
_TP_FIXED_PIPS_MODE = 2

_M_TRADES = 0

# ── Fixture geometry ─────────────────────────────────────────────────
# 5 H1 bars × 60 M1 sub-bars per bar = 300 M1 bars.
# Signal at H1 bar 1, so trade management runs bar 2 onwards (sub 120+).
# We place the BE-trigger sub-bar at sub 130, and the "next sub-bar"
# that applies the pending SL and exits at sub 131.

ENTRY_PRICE = 1.10000
PIP = 0.0001
N_H = 5
SUB_PER_BAR = 60
N_M = N_H * SUB_PER_BAR
SIG_BAR = 1
TRIGGER_SUB = 2 * SUB_PER_BAR + 10  # 130: inside bar 2's sub-range
NEXT_SUB = TRIGGER_SUB + 1  # 131: applies pending, exits here


# ── Scenarios (match 03-behaviour-table.md) ──────────────────────────


def _p(pips: float) -> float:
    """pips offset from ENTRY_PRICE → absolute price."""
    return ENTRY_PRICE + pips * PIP


SCENARIOS = [
    {
        "name": "row_1_plain_vanilla_long",
        "direction": DIR_BUY,
        "trigger": 5.0,
        "offset": 2.0,
        # Trigger sub: sb_high reaches +6 pips (>= trigger of 5)
        # Next sub: sb_low dips to +1 pip (<= new SL of +2 pip) → exit at +2
        "subs": {
            TRIGGER_SUB: (_p(+6), _p(+0), _p(+3)),  # h, l, c
            NEXT_SUB: (_p(+2), _p(+1), _p(+2)),
        },
        "expected_pnl_pips": +2.0,
    },
    {
        "name": "row_2_main_bug_long_offset_gt_trigger",
        "direction": DIR_BUY,
        "trigger": 5.0,
        "offset": 10.0,
        # Trigger sub: sb_high +6 pips (>= trigger)
        # Next sub: sb_low +1 pip — SL at +10 pips is ABOVE sb_low, so fires
        # instantly for +10 pips. THIS IS THE BUG.
        "subs": {
            TRIGGER_SUB: (_p(+6), _p(+0), _p(+3)),
            NEXT_SUB: (_p(+2), _p(+1), _p(+1)),
        },
        "expected_pnl_pips": +10.0,
    },
    {
        "name": "row_3_main_bug_short_offset_gt_trigger",
        "direction": DIR_SELL,
        "trigger": 5.0,
        "offset": 10.0,
        # For short: float_pnl uses sb_low (price fell). Trigger sub has
        # sb_low = -6 pips (profit 6). BE moves SL to entry − 10 pips.
        # Next sub has sb_high at -1 pip — above the new SL of -10 pips,
        # so SL fires for +10 pips short profit. Mirror bug.
        "subs": {
            TRIGGER_SUB: (_p(+0), _p(-6), _p(-3)),
            NEXT_SUB: (_p(-1), _p(-2), _p(-1)),
        },
        "expected_pnl_pips": +10.0,
    },
    {
        "name": "row_4_long_spike_tight_offset",
        "direction": DIR_BUY,
        "trigger": 5.0,
        "offset": 2.0,
        # Trigger sub: spike to +6 pips then close at +2. BE fires.
        # Next sub: price retreats hard below entry — sb_low −4 pips.
        # SL at +2 pips still fires on next sub (sb_low ≤ +2). Exit +2.
        # This is defensible (tick-level trigger is MT5-standard).
        "subs": {
            TRIGGER_SUB: (_p(+6), _p(+0), _p(+2)),
            NEXT_SUB: (_p(+1), _p(-4), _p(-3)),
        },
        "expected_pnl_pips": +2.0,
    },
    {
        "name": "row_5_long_spike_plus_main_bug",
        "direction": DIR_BUY,
        "trigger": 5.0,
        "offset": 10.0,
        # Spike to +6 pips triggers BE. SL at +10. Price retreats hard;
        # would have been a loss without BE. Instead, SL at +10 fires
        # on next sub (sb_low ≤ +10). Exit +10 pips from a losing path.
        # THE WORST CASE COMBINATION — bug amplified by intrabar trigger.
        "subs": {
            TRIGGER_SUB: (_p(+6), _p(+0), _p(+2)),
            NEXT_SUB: (_p(+1), _p(-6), _p(-5)),
        },
        "expected_pnl_pips": +10.0,
    },
    {
        "name": "row_6_long_negative_offset_safe_path",
        "direction": DIR_BUY,
        "trigger": 5.0,
        "offset": -2.0,
        # Trigger sub reaches +5. BE moves SL to entry − 2 pips = 1.09998
        # (below entry, but tighter than original 1.09970). Next sub retraces:
        # sb_low −4 pips. SL at −2 pips fires (sb_low ≤ −2 pip). Exit −2.
        # SAFE path — new SL is on the correct side of price, so fires as a
        # normal loss-limiting stop, not an impossible-profit stop.
        "subs": {
            TRIGGER_SUB: (_p(+6), _p(+0), _p(+3)),
            NEXT_SUB: (_p(+1), _p(-4), _p(-3)),
        },
        "expected_pnl_pips": -2.0,
    },
]


# ── Fixture builder ──────────────────────────────────────────────────


def _build_fixture(scenario: dict) -> dict:
    """Build OHLC + signal arrays for a single-trade scenario."""
    # Flat everything at ENTRY_PRICE by default.
    m_h = np.full(N_M, ENTRY_PRICE, dtype=np.float64)
    m_l = np.full(N_M, ENTRY_PRICE, dtype=np.float64)
    m_c = np.full(N_M, ENTRY_PRICE, dtype=np.float64)
    m_s = np.zeros(N_M, dtype=np.float64)

    # Apply per-scenario sub-bar overrides.
    for sub_idx, (h, l, c) in scenario["subs"].items():
        m_h[sub_idx] = h
        m_l[sub_idx] = l
        m_c[sub_idx] = c

    # Derive H1 OHLC from M1 aggregates (clean).
    h_h = np.empty(N_H, dtype=np.float64)
    h_l = np.empty(N_H, dtype=np.float64)
    h_c = np.empty(N_H, dtype=np.float64)
    h_s = np.zeros(N_H, dtype=np.float64)
    for i in range(N_H):
        s, e = i * SUB_PER_BAR, (i + 1) * SUB_PER_BAR
        h_h[i] = m_h[s:e].max()
        h_l[i] = m_l[s:e].min()
        h_c[i] = m_c[e - 1]

    map_start = (np.arange(N_H) * SUB_PER_BAR).astype(np.int64)
    map_end = ((np.arange(N_H) + 1) * SUB_PER_BAR).astype(np.int64)

    # Single signal at H1 bar SIG_BAR.
    bar_index = np.array([SIG_BAR], dtype=np.int64)
    direction = np.array([scenario["direction"]], dtype=np.int64)
    entry_price = np.array([ENTRY_PRICE], dtype=np.float64)
    hour = np.array([0], dtype=np.int64)
    day = np.array([0], dtype=np.int64)
    atr_pips = np.array([20.0], dtype=np.float64)
    swing_sl = np.zeros(1, dtype=np.float64)
    filter_value = np.zeros(1, dtype=np.float64)
    variant = np.zeros(1, dtype=np.int64)
    sig_filters = np.full((bc.NUM_SIGNAL_PARAMS, 1), -1, dtype=np.int64)

    return dict(
        h_h=h_h,
        h_l=h_l,
        h_c=h_c,
        h_s=h_s,
        m_h=m_h,
        m_l=m_l,
        m_c=m_c,
        m_s=m_s,
        map_start=map_start,
        map_end=map_end,
        bar_index=bar_index,
        direction=direction,
        entry_price=entry_price,
        hour=hour,
        day=day,
        atr_pips=atr_pips,
        swing_sl=swing_sl,
        filter_value=filter_value,
        variant=variant,
        sig_filters=sig_filters,
    )


def _build_param_row(scenario: dict) -> np.ndarray:
    """Build a single parameter row: fixed SL 30 pips, fixed TP 60 pips,
    breakeven active with the scenario's trigger/offset, everything else OFF."""
    row = np.zeros(bc.NUM_PL, dtype=np.float64)
    row[bc.PL_SIGNAL_VARIANT] = 0
    row[bc.PL_SL_MODE] = _SL_FIXED_PIPS_MODE
    row[bc.PL_SL_FIXED_PIPS] = 30.0
    row[bc.PL_TP_MODE] = _TP_FIXED_PIPS_MODE
    row[bc.PL_TP_FIXED_PIPS] = 60.0
    row[bc.PL_HOURS_START] = 0
    row[bc.PL_HOURS_END] = 23
    row[bc.PL_DAYS_BITMASK] = 127
    row[bc.PL_BUY_FILTER_MAX] = -1
    row[bc.PL_SELL_FILTER_MIN] = -1
    row[bc.PL_BREAKEVEN_ENABLED] = 1
    row[bc.PL_BREAKEVEN_TRIGGER] = scenario["trigger"]
    row[bc.PL_BREAKEVEN_OFFSET] = scenario["offset"]
    return row


def _run_scenario(scenario: dict) -> tuple[int, float]:
    """Run one scenario and return (trade_count, pnl_of_first_trade_in_pips)."""
    data = _build_fixture(scenario)
    row = _build_param_row(scenario)
    param_matrix = row.reshape(1, -1)
    max_trades = 1
    metrics = np.zeros((1, bc.NUM_METRICS), dtype=np.float64)
    pnl = np.empty((1, max_trades), dtype=np.float64)
    param_layout = np.arange(bc.NUM_PL, dtype=np.int64)

    bc.batch_evaluate(
        data["h_h"],
        data["h_l"],
        data["h_c"],
        data["h_s"],
        PIP,
        0.0,  # pip_value, slippage
        data["bar_index"],
        data["direction"],
        data["entry_price"],
        data["hour"],
        data["day"],
        data["atr_pips"],
        data["swing_sl"],
        data["filter_value"],
        data["variant"],
        data["sig_filters"],
        param_matrix,
        param_layout,
        metrics,
        max_trades,
        365.0 * 24.0,  # bars_per_year (H1)
        0.0,
        999.0,  # commission, max_spread
        data["m_h"],
        data["m_l"],
        data["m_c"],
        data["m_s"],
        data["map_start"],
        data["map_end"],
        pnl,
    )

    n_trades = int(metrics[0, _M_TRADES])
    trade_pnl = float(pnl[0, 0]) if n_trades > 0 else float("nan")
    return n_trades, trade_pnl


# ── The tests ────────────────────────────────────────────────────────


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_breakeven_offset_scenario(scenario):
    n_trades, trade_pnl = _run_scenario(scenario)

    assert (
        n_trades == 1
    ), f"{scenario['name']}: expected exactly 1 trade, got {n_trades}. Scenario setup rejected the signal — check the fixture."

    expected = scenario["expected_pnl_pips"]
    tol = 0.15  # 0.15 pip tolerance swallows rounding / sub-bar arithmetic
    assert abs(trade_pnl - expected) <= tol, (
        f"{scenario['name']}: engine produced PnL={trade_pnl:+.3f} pips, "
        f"expected {expected:+.3f} pips (tol ±{tol}).\n"
        f"Params: trigger={scenario['trigger']}, offset={scenario['offset']}, "
        f"direction={'BUY' if scenario['direction'] == DIR_BUY else 'SELL'}.\n"
        f"If this row is one of the known bug rows (2, 3, 5) and it now "
        f"disagrees with the hand-calc, the engine may have been fixed "
        f"and this test needs its expectation updated."
    )
