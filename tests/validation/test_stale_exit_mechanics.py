"""Micro-test for the stale-exit knob (Phase 4 of validate-forex-knob).

Seven hand-calculated scenarios from
`docs/validation/2026-04-19-stale-exit/03-behaviour-table.md`.

The stale-exit logic in core/src/trade_full.rs:113-136 is believed
correct at the semantic level. This test pins that belief against
explicit hand-calculated PnL targets so any future regression (a
slot-wiring miss, a lookback off-by-one, a sign flip on short PnL, a
change to the entry-spread/slippage contract) trips a loud failure.

Fixture conventions (all rows share these unless overridden):
    ENTRY_PRICE  = 1.10000
    N_H          = 20  H1 bars × 60 sub-bars per bar = 1200 M1 bars
    SIG_BAR      = 1   (entry at H1 bar 1; management loop from bar 2)
    atr_pips     = 10  (via sig_atr_pips_s)
    spread       = 0   (zeroed to avoid short-side exit-spread asymmetry)
    commission   = 0
    SL / TP      = 100 pips fixed (wide — never fires in these fixtures)

Per-bar H1 (H, L, C) is controlled by flooding every sub-bar in the
bar with the same triple. H1 aggregation then produces the exact
specified triple at bar level.
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
N_H = 20
SUB_PER_BAR = 60
N_M = N_H * SUB_PER_BAR
SIG_BAR = 1


def _p(pips: float) -> float:
    """Pips offset from ENTRY_PRICE → absolute price."""
    return ENTRY_PRICE + pips * PIP


def _flat(pips: float) -> tuple[float, float, float]:
    """Convenience: a flat H1 bar at entry + `pips`."""
    p = _p(pips)
    return (p, p, p)


SCENARIOS = [
    {
        "name": "row_1_long_stale_fires_plain",
        "direction": DIR_BUY,
        "stale_enabled": 1,
        "stale_bars": 2,
        "atr_thresh": 0.5,
        "atr_pips": 10.0,
        "slippage": 0.0,
        # Bars 2, 3 flat with range=0; bar 3 is first eligible (bars_held=2).
        # Ceiling = 0.5 * 10 = 5 pips. max_range = 0 < 5 → fires at bar 3.
        "hlc_per_bar": {
            2: _flat(+1),
            3: _flat(+2),
            **{i: _flat(+3) for i in range(4, N_H)},
        },
        "expected_pnl_pips": +2.0,
    },
    {
        "name": "row_2_short_stale_fires_mirror",
        "direction": DIR_SELL,
        "stale_enabled": 1,
        "stale_bars": 2,
        "atr_thresh": 0.5,
        "atr_pips": 10.0,
        "slippage": 0.0,
        # Identical fixture to row 1, direction flipped. Fires at bar 3.
        # pnl_short = (entry - close[3] - 0) / pip = (0 - 2) = -2 pips.
        "hlc_per_bar": {
            2: _flat(+1),
            3: _flat(+2),
            **{i: _flat(+3) for i in range(4, N_H)},
        },
        "expected_pnl_pips": -2.0,
    },
    {
        "name": "row_3_stale_does_not_fire_range_above_ceiling",
        "direction": DIR_BUY,
        "stale_enabled": 1,
        "stale_bars": 2,
        "atr_thresh": 0.5,
        "atr_pips": 10.0,
        "slippage": 0.0,
        # Every bar has H-L range = 10 pips. Ceiling = 5. max_range ≥ 10 → no fire.
        # End-of-data exit at close[19] = entry → 0 pips.
        "hlc_per_bar": {
            i: (_p(+5), _p(-5), _p(0)) for i in range(2, N_H)
        },
        "expected_pnl_pips": 0.0,
    },
    {
        "name": "row_4_stale_off_control",
        "direction": DIR_BUY,
        "stale_enabled": 0,
        "stale_bars": 2,
        "atr_thresh": 0.5,
        "atr_pips": 10.0,
        "slippage": 0.0,
        # Same fixture as row 1. stale_enabled=0 → stale never fires.
        # End-of-data exit at close[19] = entry + 3 pips → +3 pips.
        # The 1-pip gap vs row 1 (+2) proves row 1 fired at bar 3.
        "hlc_per_bar": {
            2: _flat(+1),
            3: _flat(+2),
            **{i: _flat(+3) for i in range(4, N_H)},
        },
        "expected_pnl_pips": +3.0,
    },
    {
        "name": "row_5_atr_thresh_100_degenerates_to_time_exit",
        "direction": DIR_BUY,
        "stale_enabled": 1,
        "stale_bars": 3,
        "atr_thresh": 100.0,
        "atr_pips": 10.0,
        "slippage": 0.0,
        # Ceiling = 1000 pips; no real bar exceeds. Fires at bar 4 (bars_held=3).
        # Bars have wide H-L (10 pips) to confirm the ceiling dominates over
        # observable volatility.
        "hlc_per_bar": {
            2: (_p(+6), _p(-4), _p(+1)),
            3: (_p(+7), _p(-3), _p(+2)),
            4: (_p(+8), _p(-2), _p(+3)),
            **{i: (_p(+8), _p(-2), _p(+3)) for i in range(5, N_H)},
        },
        "expected_pnl_pips": +3.0,
    },
    {
        "name": "row_6_lookback_excludes_entry_bar",
        "direction": DIR_BUY,
        "stale_enabled": 1,
        "stale_bars": 3,
        "atr_thresh": 0.5,
        "atr_pips": 10.0,
        "slippage": 0.0,
        # Entry bar (1) has range = 100 pips. Bars 2-4 are flat (range=0).
        # Ceiling = 5 pips.
        # If the lookback incorrectly included entry_bar, max_range = 100 > 5
        # → no fire → end-of-data close at +3 pips.
        # Correct behaviour: lookback_start = max(2, 4-3+1) = 2 → bars 2..4 only.
        # max_range = 0 < 5 → fires at bar 4 → +3 pips.
        # (Same expected PnL as row 5 — the diagnostic is that a fixture which
        # WOULD break stale if entry bar was included still produces the
        # expected +3.)
        "hlc_per_bar": {
            1: (_p(+50), _p(-50), _p(0)),
            2: _flat(+1),
            3: _flat(+2),
            **{i: _flat(+3) for i in range(4, N_H)},
        },
        "expected_pnl_pips": +3.0,
    },
    {
        "name": "row_7_slippage_applied_long",
        "direction": DIR_BUY,
        "stale_enabled": 1,
        "stale_bars": 2,
        "atr_thresh": 0.5,
        "atr_pips": 10.0,
        "slippage": 2.0,
        # Identical fixture to row 1 plus 2-pip slippage.
        # Long entry adjusts actual_entry upward by slippage:
        #   actual_entry = 1.10000 + 0.0002 = 1.10002
        # Stale fires at bar 3; exit pnl = (close - slip - actual_entry) / pip
        #   = (1.10002 - 0.0002 - 1.10002) / 0.0001 = -2 pips.
        # Round-trip slippage cost = 4 pips vs row 1 (+2) gap.
        "hlc_per_bar": {
            2: _flat(+1),
            3: _flat(+2),
            **{i: _flat(+3) for i in range(4, N_H)},
        },
        "expected_pnl_pips": -2.0,
    },
]


# ── Fixture builder ──────────────────────────────────────────────────

def _build_fixture(scenario: dict) -> dict:
    """Build M1 / H1 arrays from a per-H1-bar (H, L, C) spec.

    Every sub-bar within a given H1 bar is set to the same (h, l, c),
    so H1 aggregation yields the exact specified triple. Bars not in
    the spec default to (ENTRY_PRICE, ENTRY_PRICE, ENTRY_PRICE) = flat.
    """
    m_h = np.full(N_M, ENTRY_PRICE, dtype=np.float64)
    m_l = np.full(N_M, ENTRY_PRICE, dtype=np.float64)
    m_c = np.full(N_M, ENTRY_PRICE, dtype=np.float64)
    m_s = np.zeros(N_M, dtype=np.float64)

    for bar_idx, (h, l, c) in scenario["hlc_per_bar"].items():
        s, e = bar_idx * SUB_PER_BAR, (bar_idx + 1) * SUB_PER_BAR
        m_h[s:e] = h
        m_l[s:e] = l
        m_c[s:e] = c

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
    row[bc.PL_SL_FIXED_PIPS] = 100.0
    row[bc.PL_TP_MODE] = _TP_FIXED_PIPS_MODE
    row[bc.PL_TP_FIXED_PIPS] = 100.0
    row[bc.PL_HOURS_START] = 0
    row[bc.PL_HOURS_END] = 23
    row[bc.PL_DAYS_BITMASK] = 127
    row[bc.PL_BUY_FILTER_MAX] = -1
    row[bc.PL_SELL_FILTER_MIN] = -1
    # Stale params under test.
    row[bc.PL_STALE_ENABLED] = scenario["stale_enabled"]
    row[bc.PL_STALE_BARS] = scenario["stale_bars"]
    row[bc.PL_STALE_ATR_THRESH] = scenario["atr_thresh"]
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
        PIP, scenario["slippage"],
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
def test_stale_exit_scenario(scenario):
    n_trades, trade_pnl = _run_scenario(scenario)
    assert n_trades == 1, (
        f"{scenario['name']}: expected exactly 1 trade, got {n_trades}. "
        f"Fixture may have rejected the signal."
    )
    expected = scenario["expected_pnl_pips"]
    tol = 0.25
    assert abs(trade_pnl - expected) <= tol, (
        f"{scenario['name']}: engine PnL = {trade_pnl:+.3f}, "
        f"expected {expected:+.3f} (±{tol}).\n"
        f"stale_enabled={scenario['stale_enabled']}, "
        f"stale_bars={scenario['stale_bars']}, "
        f"atr_thresh={scenario['atr_thresh']}, "
        f"atr_pips={scenario['atr_pips']}, "
        f"slippage={scenario['slippage']}, "
        f"direction={'BUY' if scenario['direction'] == DIR_BUY else 'SELL'}."
    )
