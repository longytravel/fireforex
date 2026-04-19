"""Micro-test for the chandelier stop knob (Phase 4 of validate-forex-knob).

Five hand-calculated scenarios from
`docs/builds/2026-04-19-chandelier-stop/03-reference-scenarios.md`
(condensed for the synthetic-sub-bar fixture used by the other
validation tests).

Expected state AFTER initial build (2026-04-19 evening, v6
chandelier-stop):

    - Row 1  long_fires_peak: arms at sub 130 on a +40 pip spike,
              adopts SL at peak-atr*mult, next sub stops out
              for +10 pips. Exercises: peak tracking, activation,
              side-of-price guard accepting a valid tightening.
    - Row 2  short_fires_peak: mirror of row 1 on a short.
              Exercises: sign flips (trough_low + atr_mult*atr,
              min-ratchet, sb_high guard).
    - Row 3  long_guard_rejects: raw_sl sits above sb_low, so
              the side-of-price guard refuses to adopt. Trade
              never fires chandelier; runs to end-of-data flat
              for 0 pips. Pins the v2-trailing-style guard (as
              opposed to Codex's "fire immediately" interpretation,
              which would produce +10 pips here).
    - Row 4  long_below_activate_never_arms: peak spike but
              float-PnL never crosses the (inflated) activate
              threshold. Arms never fires, no SL write, trade
              runs to end-of-data for 0 pips. Pins the activation
              gate as a strict inequality.
    - Row 5  long_sentinel_strict_no_op: same OHLC as row 1 but
              enabled=0, activate=-1, mult=-1. Chandelier block
              short-circuits. Trade runs flat to end-of-data for
              0 pips (TP 60p away never reached). Pins the sentinel
              short-circuit.

If row 1 or row 2 drops below +10, peak tracking or the ratchet
broke. If rows 3/4/5 start producing +10, the guard or activation
gate regressed. Either direction is a silent-knob recurrence.
"""
from __future__ import annotations

import numpy as np
import pytest

import ff_core as bc


DIR_BUY = 1
DIR_SELL = -1

_SL_FIXED_PIPS_MODE = 0
_TP_FIXED_PIPS_MODE = 2

_M_TRADES = 0

ENTRY_PRICE = 1.10000
PIP = 0.0001
N_H = 5
SUB_PER_BAR = 60
N_M = N_H * SUB_PER_BAR
SIG_BAR = 1
TRIGGER_SUB = 2 * SUB_PER_BAR + 10   # 130
NEXT_SUB = TRIGGER_SUB + 1           # 131


def _p(pips: float) -> float:
    return ENTRY_PRICE + pips * PIP


SCENARIOS = [
    {
        "name": "row_1_long_fires_peak",
        "direction": DIR_BUY,
        "enabled": 1,
        "activate": 10.0,
        "atr_mult": 3.0,
        "atr_pips": 10.0,
        # chand_dist = 3 * 10 * 0.0001 = 0.0030 price = 30 pips.
        # Sub 130: H=+40, L=+30, C=+35. peak=+40, arm at H (+40≥10),
        # raw_sl=+10, sb_low=+30 → 10<30 → adopt pending_sl=+10.
        # Sub 131: apply pending → current_sl=+10. peak unchanged.
        # armed=true, raw_sl=+10, effective_sl=+10, not > → no re-adopt.
        # sb_low=-5 ≤ current_sl=+10 → stop fills at +10.
        # pnl = +10 pips.
        "subs": {
            TRIGGER_SUB: (_p(+40), _p(+30), _p(+35)),
            NEXT_SUB:    (_p(+8),  _p(-5),  _p(0)),
        },
        "expected_pnl_pips": +10.0,
    },
    {
        "name": "row_2_short_fires_peak",
        "direction": DIR_SELL,
        "enabled": 1,
        "activate": 10.0,
        "atr_mult": 3.0,
        "atr_pips": 10.0,
        # Mirror: trough tracked, raw_sl = trough + chand_dist.
        # Sub 130: H=-30, L=-40, C=-35. trough=-40, float +40 ≥ 10 → arm.
        # raw_sl=-10, sb_high=-30 → -10 > -30 → adopt pending_sl=-10.
        # Sub 131: apply pending → current_sl=-10. sb_high=+5 ≥ -10 → fill at -10.
        # pnl = (entry - exit)/pip = -(-10) = +10 pips.
        "subs": {
            TRIGGER_SUB: (_p(-30), _p(-40), _p(-35)),
            NEXT_SUB:    (_p(+5),  _p(-8),  _p(0)),
        },
        "expected_pnl_pips": +10.0,
    },
    {
        "name": "row_3_long_guard_rejects",
        "direction": DIR_BUY,
        "enabled": 1,
        "activate": 10.0,
        "atr_mult": 3.0,
        "atr_pips": 10.0,
        # Sub 130: H=+40, L=+0, C=+20. peak=+40, arm at H.
        # raw_sl=+10, sb_low=+0 → 10 < 0? NO → guard rejects.
        # pending_chandelier_active=true (via else), pending_sl stays -1.
        # Sub 131+: raw_sl still +10, sb_low still +0 → guard keeps rejecting.
        # End-of-data close at entry → pnl = 0.
        "subs": {
            TRIGGER_SUB: (_p(+40), _p(0),  _p(+20)),
            NEXT_SUB:    (_p(+30), _p(0),  _p(+15)),
        },
        "expected_pnl_pips": 0.0,
    },
    {
        "name": "row_4_long_below_activate_never_arms",
        "direction": DIR_BUY,
        "enabled": 1,
        "activate": 50.0,     # threshold far above peak
        "atr_mult": 3.0,
        "atr_pips": 10.0,
        # Sub 130: H=+40, L=-5, C=+20. peak=+40, float=+40 < 50 → no arm.
        # Peak tracks, but no SL write, no exit.
        # Sub 131 stays below threshold.
        # End-of-data close at entry → pnl = 0.
        "subs": {
            TRIGGER_SUB: (_p(+40), _p(-5), _p(+20)),
            NEXT_SUB:    (_p(+30), _p(-5), _p(+15)),
        },
        "expected_pnl_pips": 0.0,
    },
    {
        "name": "row_5_long_sentinel_strict_no_op",
        "direction": DIR_BUY,
        "enabled": 0,          # sentinel off
        "activate": -1.0,      # sentinel
        "atr_mult": -1.0,      # sentinel
        "atr_pips": 10.0,
        # Same OHLC as row 1. With chandelier disabled, trade runs
        # baseline (SL=30p, TP=60p). Peak at +40 < TP=60 → no TP.
        # Next sub at -5p > SL=-30p → no SL. End-of-data close at
        # entry → pnl = 0.
        "subs": {
            TRIGGER_SUB: (_p(+40), _p(+30), _p(+35)),
            NEXT_SUB:    (_p(+8),  _p(-5),  _p(0)),
        },
        "expected_pnl_pips": 0.0,
    },
]


def _build_fixture(scenario: dict) -> dict:
    m_h = np.full(N_M, ENTRY_PRICE, dtype=np.float64)
    m_l = np.full(N_M, ENTRY_PRICE, dtype=np.float64)
    m_c = np.full(N_M, ENTRY_PRICE, dtype=np.float64)
    m_s = np.zeros(N_M, dtype=np.float64)

    for sub_idx, (h, l, c) in scenario["subs"].items():
        m_h[sub_idx] = h
        m_l[sub_idx] = l
        m_c[sub_idx] = c

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

    bar_index = np.array([SIG_BAR], dtype=np.int64)
    direction = np.array([scenario["direction"]], dtype=np.int64)
    entry_price = np.array([ENTRY_PRICE], dtype=np.float64)
    hour = np.array([0], dtype=np.int64)
    day = np.array([0], dtype=np.int64)
    atr_pips = np.array([scenario["atr_pips"]], dtype=np.float64)
    swing_sl = np.zeros(1, dtype=np.float64)
    filter_value = np.zeros(1, dtype=np.float64)
    variant = np.zeros(1, dtype=np.int64)
    sig_filters = np.full((bc.NUM_SIGNAL_PARAMS, 1), -1, dtype=np.int64)

    return dict(
        h_h=h_h, h_l=h_l, h_c=h_c, h_s=h_s,
        m_h=m_h, m_l=m_l, m_c=m_c, m_s=m_s,
        map_start=map_start, map_end=map_end,
        bar_index=bar_index, direction=direction, entry_price=entry_price,
        hour=hour, day=day, atr_pips=atr_pips,
        swing_sl=swing_sl, filter_value=filter_value, variant=variant,
        sig_filters=sig_filters,
    )


def _build_param_row(scenario: dict) -> np.ndarray:
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
    # All other management knobs off.
    row[bc.PL_CHANDELIER_ENABLED] = scenario["enabled"]
    row[bc.PL_CHANDELIER_ACTIVATE] = scenario["activate"]
    row[bc.PL_CHANDELIER_ATR_MULT] = scenario["atr_mult"]
    return row


def _run_scenario(scenario: dict) -> tuple[int, float]:
    data = _build_fixture(scenario)
    row = _build_param_row(scenario)
    param_matrix = row.reshape(1, -1)
    max_trades = 1
    metrics = np.zeros((1, bc.NUM_METRICS), dtype=np.float64)
    pnl = np.empty((1, max_trades), dtype=np.float64)
    param_layout = np.arange(bc.NUM_PL, dtype=np.int64)

    bc.batch_evaluate(
        data["h_h"], data["h_l"], data["h_c"], data["h_s"],
        PIP, 0.0,
        data["bar_index"], data["direction"], data["entry_price"],
        data["hour"], data["day"], data["atr_pips"],
        data["swing_sl"], data["filter_value"], data["variant"],
        data["sig_filters"],
        param_matrix, param_layout,
        metrics,
        max_trades, 365.0 * 24.0,
        0.0, 999.0,
        data["m_h"], data["m_l"], data["m_c"], data["m_s"],
        data["map_start"], data["map_end"],
        pnl,
    )

    n_trades = int(metrics[0, _M_TRADES])
    trade_pnl = float(pnl[0, 0]) if n_trades > 0 else float("nan")
    return n_trades, trade_pnl


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_chandelier_scenario(scenario):
    n_trades, trade_pnl = _run_scenario(scenario)
    assert n_trades == 1, (
        f"{scenario['name']}: expected 1 trade, got {n_trades}"
    )
    expected = scenario["expected_pnl_pips"]
    tol = 0.25
    assert abs(trade_pnl - expected) <= tol, (
        f"{scenario['name']}: engine PnL = {trade_pnl:+.3f}, expected "
        f"{expected:+.3f} (+/-{tol}).\n"
        f"enabled={scenario['enabled']}, activate={scenario['activate']}, "
        f"atr_mult={scenario['atr_mult']}, atr_pips={scenario['atr_pips']}."
    )
