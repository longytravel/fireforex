"""Micro-test for the trailing stop family (Phase 4 of validate-forex-knob).

Five hand-calculated scenarios from
`docs/validation/2026-04-19-trailing/03-behaviour-table.md`.

Expected state AFTER v2 trailing-fix (added 2026-04-19 afternoon):
    - Rows 1, 5 (safe controls): trail activates and tracks as expected.
    - Rows 2, 3, 4 (bug scenarios): the side-of-price guard rejects the
      invalid trailing SL; the trade continues to end-of-data at entry,
      producing 0 pips. Pre-fix these rows produced +5 / +5 / +5.1 pips
      from the engine bug.

This test is the guardrail: if any "safe" row stops accepting the trail,
or any "rejected" row starts producing non-zero pips, the fix has
regressed.
"""
from __future__ import annotations

import numpy as np
import pytest

import ff_core as bc


DIR_BUY = 1
DIR_SELL = -1

_SL_FIXED_PIPS_MODE = 0
_TP_FIXED_PIPS_MODE = 2
_TRAIL_OFF = 0
_TRAIL_FIXED_PIP = 1
_TRAIL_ATR_CHANDELIER = 2

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
        "name": "row_1_long_safe_fixed",
        "direction": DIR_BUY,
        "mode": _TRAIL_FIXED_PIP,
        "activate": 5.0,
        "distance": 10.0,
        "atr_mult": 0.0,
        "atr_pips": 20.0,
        "subs": {
            TRIGGER_SUB: (_p(+6), _p(+0), _p(+3)),
            NEXT_SUB:    (_p(+1), _p(-6), _p(-5)),
        },
        "expected_pnl_pips": -4.0,
    },
    {
        "name": "row_2_long_tiny_distance_BUG",
        "direction": DIR_BUY,
        "mode": _TRAIL_FIXED_PIP,
        "activate": 5.0,
        "distance": 1.0,
        "atr_mult": 0.0,
        "atr_pips": 20.0,
        # Post-fix: new_sl (entry + 5) > sb_close (entry + 2) for long
        # → side-of-price guard REJECTS. Trade continues flat,
        # end-of-data close at entry → 0 pips.
        # Pre-fix produced +5 pips from the engine bug.
        "subs": {
            TRIGGER_SUB: (_p(+6), _p(+0), _p(+2)),
            NEXT_SUB:    (_p(+3), _p(+1), _p(+1)),
        },
        "expected_pnl_pips": 0.0,
    },
    {
        "name": "row_3_short_tiny_distance_BUG_mirror",
        "direction": DIR_SELL,
        "mode": _TRAIL_FIXED_PIP,
        "activate": 5.0,
        "distance": 1.0,
        "atr_mult": 0.0,
        "atr_pips": 20.0,
        # Post-fix: new_sl (entry - 5) < sb_close (entry - 2) for short
        # → side-of-price guard REJECTS. 0 pips.
        # Pre-fix produced +5 pips (mirror bug).
        "subs": {
            TRIGGER_SUB: (_p(+0), _p(-6), _p(-2)),
            NEXT_SUB:    (_p(-1), _p(-3), _p(-1)),
        },
        "expected_pnl_pips": 0.0,
    },
    {
        "name": "row_4_long_atr_mode_tiny_BUG",
        "direction": DIR_BUY,
        "mode": _TRAIL_ATR_CHANDELIER,
        "activate": 5.0,
        "distance": 0.0,
        "atr_mult": 0.3,
        "atr_pips": 3.0,
        # Post-fix: effective distance 0.9 pips → new_sl ~= entry + 5.1
        # pips > sb_close (+2 pips) → side-of-price guard REJECTS. 0 pips.
        # Pre-fix produced +5.1 pips (ATR-mode bug).
        "subs": {
            TRIGGER_SUB: (_p(+6), _p(+0), _p(+2)),
            NEXT_SUB:    (_p(+3), _p(+1), _p(+1)),
        },
        "expected_pnl_pips": 0.0,
    },
    {
        "name": "row_5_short_safe_fixed",
        "direction": DIR_SELL,
        "mode": _TRAIL_FIXED_PIP,
        "activate": 5.0,
        "distance": 10.0,
        "atr_mult": 0.0,
        "atr_pips": 20.0,
        "subs": {
            TRIGGER_SUB: (_p(+0), _p(-6), _p(-3)),
            NEXT_SUB:    (_p(+6), _p(-1), _p(+5)),
        },
        "expected_pnl_pips": -4.0,
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
    row[bc.PL_TRAILING_MODE] = scenario["mode"]
    row[bc.PL_TRAIL_ACTIVATE] = scenario["activate"]
    row[bc.PL_TRAIL_DISTANCE] = scenario["distance"]
    row[bc.PL_TRAIL_ATR_MULT] = scenario["atr_mult"]
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
def test_trailing_scenario(scenario):
    n_trades, trade_pnl = _run_scenario(scenario)
    assert n_trades == 1, (
        f"{scenario['name']}: expected 1 trade, got {n_trades}"
    )
    expected = scenario["expected_pnl_pips"]
    tol = 0.25
    assert abs(trade_pnl - expected) <= tol, (
        f"{scenario['name']}: engine PnL = {trade_pnl:+.3f}, expected "
        f"{expected:+.3f} (+/-{tol}).\n"
        f"Mode={scenario['mode']}, activate={scenario['activate']}, "
        f"distance={scenario['distance']}, atr_mult={scenario['atr_mult']}, "
        f"atr_pips={scenario['atr_pips']}."
    )
