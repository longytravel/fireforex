"""Micro-test for the partial close knob (Phase 4 of validate-forex-knob).

Six hand-calculated scenarios from `docs/validation/2026-04-19-partial-close/
03-behaviour-table.md`. Each scenario pins a synthetic OHLC path, runs
ff_core.batch_evaluate for exactly one trial, and asserts the PnL against
a hand-calculated expected value.

The expected values encode the CORRECT engine behaviour — i.e. what partial
close *should* do, given the mechanics brief in 01-mechanics-brief.md.

Two bugs this test was written to catch:

  BUG A (realise-at-trigger) — The pre-fix engine realises the partial at
      `sb_close`, the end-of-sub-bar price, not at the limit price the user
      asked for. On a trending sub-bar where sb_close closes well above the
      trigger, this over-states partial pnl. Real-world limit orders fill
      at the limit price, never better.

  BUG B (trigger-over-tp ordering) — The pre-fix engine checks partial
      close before it checks take-profit within a sub-bar. If the trigger
      sits beyond TP (only reachable if the sl_tp clamp is bypassed, e.g.
      tests with SL=12, TP=12), the partial fires even though a real TP
      limit order would have filled first.

Pre-fix historical values (kept here for context):
    row_2_partial_on_trigger_lt_tp_long  pre-fix = +36.0,  post-fix = +35.0
    row_3_partial_on_trigger_lt_tp_short pre-fix = +36.0,  post-fix = +35.0
    row_4_bug_long_trigger_gt_tp         pre-fix = +26.0,  post-fix = +12.0
    row_5_bug_short_trigger_gt_tp        pre-fix = +26.0,  post-fix = +12.0
    row_6_partial_rescues_to_win         pre-fix = +6.6,   post-fix = +4.5
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
# 5 H1 bars × 60 M1 sub-bars = 300 M1 bars.
# Signal at H1 bar 1 → management runs from bar 2 (sub 120) onwards.
# Trigger sub-bar = 130, next sub-bar = 131.

ENTRY_PRICE = 1.10000
PIP = 0.0001
N_H = 5
SUB_PER_BAR = 60
N_M = N_H * SUB_PER_BAR
SIG_BAR = 1
TRIGGER_SUB = 2 * SUB_PER_BAR + 10
NEXT_SUB = TRIGGER_SUB + 1


def _p(pips: float) -> float:
    """pips offset from ENTRY_PRICE → absolute price."""
    return ENTRY_PRICE + pips * PIP


SCENARIOS = [
    {
        # Row 1: partial disabled; TP fires cleanly on sub 131 at +60.
        "name": "row_1_partial_off_long_tp",
        "direction": DIR_BUY,
        "sl_pips": 30.0,
        "tp_pips": 60.0,
        "partial_enabled": 0,
        "pct": 0.0,
        "trigger": 0.0,
        "subs": {
            TRIGGER_SUB: (_p(+20), _p(+0), _p(+10)),
            NEXT_SUB: (_p(+65), _p(+30), _p(+60)),
        },
        "expected_pnl_pips": +60.0,
    },
    {
        # Row 2: partial trigger below TP. Trigger=10, sb_close=+12.
        # Post-fix realises at trigger (+10 pips). 10 × 0.5 = 5.
        # TP on remainder: 60 × 0.5 = 30. Total +35.
        "name": "row_2_partial_on_trigger_lt_tp_long",
        "direction": DIR_BUY,
        "sl_pips": 30.0,
        "tp_pips": 60.0,
        "partial_enabled": 1,
        "pct": 50.0,
        "trigger": 10.0,
        "subs": {
            TRIGGER_SUB: (_p(+15), _p(+0), _p(+12)),
            NEXT_SUB: (_p(+65), _p(+10), _p(+60)),
        },
        "expected_pnl_pips": +35.0,  # post-fix. pre-fix = +36.0
    },
    {
        # Row 3: mirror of row 2 on short side.
        "name": "row_3_partial_on_trigger_lt_tp_short",
        "direction": DIR_SELL,
        "sl_pips": 30.0,
        "tp_pips": 60.0,
        "partial_enabled": 1,
        "pct": 50.0,
        "trigger": 10.0,
        "subs": {
            TRIGGER_SUB: (_p(+0), _p(-15), _p(-12)),
            NEXT_SUB: (_p(-10), _p(-65), _p(-60)),
        },
        "expected_pnl_pips": +35.0,  # post-fix. pre-fix = +36.0
    },
    {
        # Row 4: BUG B (long). Partial trigger=44 > TP=12. SL=12 chosen
        # so the sl_tp clamp does not raise TP past trigger. Pre-fix engine
        # fires partial at sb_close=+40 (20 pips) then TP on remainder
        # (6 pips) → +26. Correct: TP closer to entry, fires first on full
        # position → +12.
        "name": "row_4_bug_long_trigger_gt_tp",
        "direction": DIR_BUY,
        "sl_pips": 12.0,
        "tp_pips": 12.0,
        "partial_enabled": 1,
        "pct": 50.0,
        "trigger": 44.0,
        "subs": {
            TRIGGER_SUB: (_p(+50), _p(+0), _p(+40)),
            NEXT_SUB: (_p(+1), _p(+0), _p(+0)),
        },
        "expected_pnl_pips": +12.0,  # post-fix. pre-fix = +26.0
    },
    {
        # Row 5: BUG B mirror (short). Pre-fix +26, post-fix +12.
        "name": "row_5_bug_short_trigger_gt_tp",
        "direction": DIR_SELL,
        "sl_pips": 12.0,
        "tp_pips": 12.0,
        "partial_enabled": 1,
        "pct": 50.0,
        "trigger": 44.0,
        "subs": {
            TRIGGER_SUB: (_p(+0), _p(-50), _p(-40)),
            NEXT_SUB: (_p(+0), _p(-1), _p(+0)),
        },
        "expected_pnl_pips": +12.0,  # post-fix. pre-fix = +26.0
    },
    {
        # Row 6: partial rescues the trade. Post-fix realises at trigger
        # (+15 pips): 15 × 0.7 = 10.5. Remainder SL at −20 × 0.3 = −6.
        # Total +4.5.
        "name": "row_6_partial_rescues_to_win",
        "direction": DIR_BUY,
        "sl_pips": 20.0,
        "tp_pips": 60.0,
        "partial_enabled": 1,
        "pct": 70.0,
        "trigger": 15.0,
        "subs": {
            TRIGGER_SUB: (_p(+20), _p(+0), _p(+18)),
            NEXT_SUB: (_p(+18), _p(-25), _p(-22)),
        },
        "expected_pnl_pips": +4.5,  # post-fix. pre-fix = +6.6
    },
]


# ── Fixture builder ──────────────────────────────────────────────────


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
    row = np.zeros(bc.NUM_PL, dtype=np.float64)
    row[bc.PL_SIGNAL_VARIANT] = 0
    row[bc.PL_SL_MODE] = _SL_FIXED_PIPS_MODE
    row[bc.PL_SL_FIXED_PIPS] = scenario["sl_pips"]
    row[bc.PL_TP_MODE] = _TP_FIXED_PIPS_MODE
    row[bc.PL_TP_FIXED_PIPS] = scenario["tp_pips"]
    row[bc.PL_HOURS_START] = 0
    row[bc.PL_HOURS_END] = 23
    row[bc.PL_DAYS_BITMASK] = 127
    row[bc.PL_BUY_FILTER_MAX] = -1
    row[bc.PL_SELL_FILTER_MIN] = -1
    row[bc.PL_PARTIAL_ENABLED] = scenario["partial_enabled"]
    row[bc.PL_PARTIAL_PCT] = scenario["pct"]
    row[bc.PL_PARTIAL_TRIGGER] = scenario["trigger"]
    return row


def _run_scenario(scenario: dict) -> tuple[int, float]:
    data = _build_fixture(scenario)
    row = _build_param_row(scenario)
    param_matrix = row.reshape(1, -1)
    max_trades = 1
    metrics = np.zeros((1, bc.NUM_METRICS), dtype=np.float64)
    pnl = np.empty((1, max_trades), dtype=np.float64)
    trade_records = np.empty((1, (max_trades) * bc.NUM_TRADE_FIELDS), dtype=np.float64)
    param_layout = np.arange(bc.NUM_PL, dtype=np.int64)

    bc.batch_evaluate(
        data["h_h"],
        data["h_l"],
        data["h_c"],
        data["h_s"],
        PIP,
        0.0,
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
        365.0 * 24.0,
        0.0,
        999.0,
        data["m_h"],
        data["m_l"],
        data["m_c"],
        data["m_s"],
        data["map_start"],
        data["map_end"],
        pnl,
        trade_records,
    )

    n_trades = int(metrics[0, _M_TRADES])
    trade_pnl = float(pnl[0, 0]) if n_trades > 0 else float("nan")
    return n_trades, trade_pnl


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_partial_close_scenario(scenario):
    n_trades, trade_pnl = _run_scenario(scenario)

    assert n_trades == 1, (
        f"{scenario['name']}: expected exactly 1 trade, got {n_trades}. Scenario setup rejected the signal — check the fixture."
    )

    expected = scenario["expected_pnl_pips"]
    tol = 0.15
    assert abs(trade_pnl - expected) <= tol, (
        f"{scenario['name']}: engine produced PnL={trade_pnl:+.3f} pips, "
        f"expected {expected:+.3f} pips (tol ±{tol}).\n"
        f"Params: direction={'BUY' if scenario['direction'] == DIR_BUY else 'SELL'}, "
        f"sl={scenario['sl_pips']}, tp={scenario['tp_pips']}, "
        f"partial_enabled={scenario['partial_enabled']}, "
        f"pct={scenario['pct']}, trigger={scenario['trigger']}.\n"
        f"Rows 4 and 5 are the bug rows — if they fail with actual≈+26 and "
        f"expected=+12, the partial-over-TP ordering bug is still live."
    )
