"""Micro-test for the signal filter knobs (Phase 4 of validate-forex-knob).

Nine hand-calculated scenarios from
`docs/validation/2026-04-19-signal-filters/03-behaviour-table.md`.

The signal filter gates in core/src/lib.rs:270-297 admit or reject
signals before any SL/TP/management logic runs. A broken filter shifts
the sampling distribution of "which trades happened" — it silently
distorts every sweep.

This test pins the current engine behaviour for three filter families:
    - PL_SIGNAL_VARIANT      (variant selector)
    - PL_BUY_FILTER_MAX /
      PL_SELL_FILTER_MIN     (direction-scoped value filter)
    - PL_SIGNAL_P0..P9       (generic integer filters)

Each row plants a small known signal array and asserts the number of
admitted signals equals the hand-calculated count.

The test encodes four *known* defects as pinned expected values:
    D2 — ENGINE_DEFAULTS missing P0..P9 (row 9)
    D4 — `as i64` truncation on Pk trial side (row 8)
    D5 — float equality brittleness on buy/sell filter (row 4)
    D6 — buy/sell filter does not honour signal-side -1 (row 5)

A future fix to any of these will flip the relevant assertion and
surface the change. That is intentional.

Fixture conventions (shared unless a row overrides):
    ENTRY_PRICE = 1.10000
    PIP         = 0.0001
    N_H         = 30 H1 bars × SUB_PER_BAR = 60 M1 bars
    Signals on bars 5..14 (10 bars, one signal each)
    No price movement → SL/TP/management never fire
    All admitted signals exit at end-of-data at ~0 pips
"""

from __future__ import annotations

import ff_core as bc
import numpy as np
import pytest

DIR_BUY = 1
DIR_SELL = -1

_SL_FIXED_PIPS_MODE = 0
_TP_RR_MODE = 0
_M_TRADES = 0

ENTRY_PRICE = 1.10000
PIP = 0.0001
N_H = 30
SUB_PER_BAR = 60
N_M = N_H * SUB_PER_BAR
SIG_BAR_START = 5
N_SIGNALS = 10
MAX_TRADES = 16


# ── Signal-planting helpers ──────────────────────────────────────────


def _alt_directions(n: int = N_SIGNALS) -> np.ndarray:
    """Alternating long/short directions."""
    out = np.empty(n, dtype=np.int64)
    out[0::2] = DIR_BUY
    out[1::2] = DIR_SELL
    return out


def _planted_signals(
    directions: np.ndarray,
    variants: np.ndarray | None = None,
    filter_values: np.ndarray | None = None,
    sig_filters: np.ndarray | None = None,
) -> dict:
    """Build a signal array with one signal per bar starting at SIG_BAR_START.

    All optional arrays default to the per-family "off" sentinels
    (variant=0, filter_value=0.0, sig_filters=-1).
    """
    n = directions.size
    assert n == N_SIGNALS, f"expected {N_SIGNALS} directions, got {n}"
    bar_index = np.arange(SIG_BAR_START, SIG_BAR_START + n, dtype=np.int64)
    entry_price = np.full(n, ENTRY_PRICE, dtype=np.float64)
    hour = np.zeros(n, dtype=np.int64)
    day = np.zeros(n, dtype=np.int64)
    atr_pips = np.full(n, 10.0, dtype=np.float64)
    swing_sl = np.zeros(n, dtype=np.float64)

    if variants is None:
        variants = np.zeros(n, dtype=np.int64)
    if filter_values is None:
        filter_values = np.zeros(n, dtype=np.float64)
    if sig_filters is None:
        sig_filters = np.full((bc.NUM_SIGNAL_PARAMS, n), -1, dtype=np.int64)

    return dict(
        bar_index=bar_index,
        direction=directions.astype(np.int64, copy=False),
        entry_price=entry_price,
        hour=hour,
        day=day,
        atr_pips=atr_pips,
        swing_sl=swing_sl,
        filter_value=filter_values.astype(np.float64, copy=False),
        variant=variants.astype(np.int64, copy=False),
        sig_filters=sig_filters.astype(np.int64, copy=False),
    )


# ── Trial-row helpers ────────────────────────────────────────────────


def _base_param_row() -> np.ndarray:
    """Build a param row with all filters OFF and no management."""
    row = np.zeros(bc.NUM_PL, dtype=np.float64)
    row[bc.PL_SIGNAL_VARIANT] = -1.0
    row[bc.PL_SL_MODE] = _SL_FIXED_PIPS_MODE
    row[bc.PL_SL_FIXED_PIPS] = 100.0
    row[bc.PL_TP_MODE] = _TP_RR_MODE
    row[bc.PL_TP_RR_RATIO] = 1.0
    row[bc.PL_HOURS_START] = 0
    row[bc.PL_HOURS_END] = 23
    row[bc.PL_DAYS_BITMASK] = 127
    row[bc.PL_BUY_FILTER_MAX] = -1.0
    row[bc.PL_SELL_FILTER_MIN] = -1.0
    # PL_SIGNAL_P0..P9 default to 0.0 (matching encode() zero-fill).
    # Rows that want them OFF must explicitly set -1.0.
    for f in range(bc.NUM_SIGNAL_PARAMS):
        row[bc.PL_SIGNAL_P0 + f] = -1.0
    return row


# ── Scenario table ───────────────────────────────────────────────────

SCENARIOS = []


# Row 1 — variant positive control
_r1_dirs = _alt_directions()
_r1_variants = np.array([0, 0, 0, 0, 0, 1, 1, 1, 1, 1], dtype=np.int64)
_r1_row = _base_param_row()
_r1_row[bc.PL_SIGNAL_VARIANT] = 0.0
SCENARIOS.append(
    {
        "name": "row_1_variant_positive_control",
        "signals": _planted_signals(_r1_dirs, variants=_r1_variants),
        "param_row": _r1_row,
        "expected_trades": 5,
    }
)


# Row 2 — variant signal-side -1 opt-out (bilateral)
_r2_dirs = _alt_directions()
_r2_variants = np.array([0, 0, 0, 1, 1, 1, -1, -1, -1, -1], dtype=np.int64)
_r2_row = _base_param_row()
_r2_row[bc.PL_SIGNAL_VARIANT] = 1.0
SCENARIOS.append(
    {
        "name": "row_2_variant_signal_side_opt_out",
        "signals": _planted_signals(_r2_dirs, variants=_r2_variants),
        "param_row": _r2_row,
        "expected_trades": 3 + 4,  # variant-1 matches + variant-(-1) opt-outs
    }
)


# Row 3 — buy filter positive control (integer-valued filter_value)
# 4 long filter_value=2, 2 long filter_value=3, 4 short filter_value=5
_r3_dirs = np.array([DIR_BUY] * 6 + [DIR_SELL] * 4, dtype=np.int64)
_r3_fv = np.array([2.0, 2.0, 2.0, 2.0, 3.0, 3.0, 5.0, 5.0, 5.0, 5.0], dtype=np.float64)
_r3_row = _base_param_row()
_r3_row[bc.PL_BUY_FILTER_MAX] = 2.0
SCENARIOS.append(
    {
        "name": "row_3_buy_filter_positive_control",
        "signals": _planted_signals(_r3_dirs, filter_values=_r3_fv),
        "param_row": _r3_row,
        "expected_trades": 4 + 4,  # 4 matching long + 4 short (unfiltered)
    }
)


# Row 4 — float equality brittleness (D5)
# Long filter_values via arithmetic: 0.1+0.2 → 0.30000000000000004
# Trial sets buy_filter_max = 0.3 exact
_r4_dirs = np.array([DIR_BUY] * 5 + [DIR_SELL] * 5, dtype=np.int64)
_drift = np.float64(0.1) + np.float64(0.2)
_r4_fv = np.array([_drift] * 5 + [0.0] * 5, dtype=np.float64)
_r4_row = _base_param_row()
_r4_row[bc.PL_BUY_FILTER_MAX] = 0.3
SCENARIOS.append(
    {
        "name": "row_4_float_equality_brittleness_D5",
        "signals": _planted_signals(_r4_dirs, filter_values=_r4_fv),
        "param_row": _r4_row,
        # Post-D5 fix: tolerance compare (|a-b|<1e-9) admits 0.1+0.2 vs 0.3 drift.
        # Pre-fix expected 5 (longs silently dropped). Post-fix: all 10 admit.
        "expected_trades": 5 + 5,
    }
)


# Row 5 — buy/sell signal-side -1 is NOT an opt-out (D6)
_r5_dirs = np.array([DIR_BUY] * 6 + [DIR_SELL] * 4, dtype=np.int64)
_r5_fv = np.array([2.0, 2.0, 2.0, -1.0, -1.0, -1.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float64)
_r5_row = _base_param_row()
_r5_row[bc.PL_BUY_FILTER_MAX] = 2.0
SCENARIOS.append(
    {
        "name": "row_5_buy_filter_signal_side_minus_one_D6",
        "signals": _planted_signals(_r5_dirs, filter_values=_r5_fv),
        "param_row": _r5_row,
        # Post-D6 fix: signal-side -1 is now a bilateral opt-out (matches Pk).
        # Pre-fix expected 7 (3 match + 0 opt + 4 short). Post-fix: 3 + 3 + 4 = 10.
        "expected_trades": 3 + 3 + 4,
    }
)


# Row 6 — independent directional asymmetry
_r6_dirs = np.array([DIR_BUY] * 5 + [DIR_SELL] * 5, dtype=np.int64)
_r6_fv = np.array([2.0] * 5 + [3.0] * 5, dtype=np.float64)
_r6_row = _base_param_row()
_r6_row[bc.PL_BUY_FILTER_MAX] = 2.0
_r6_row[bc.PL_SELL_FILTER_MIN] = 3.0
SCENARIOS.append(
    {
        "name": "row_6_buy_sell_directional_asymmetry",
        "signals": _planted_signals(_r6_dirs, filter_values=_r6_fv),
        "param_row": _r6_row,
        "expected_trades": 5 + 5,
    }
)


# Row 7 — Pk bilateral opt-out positive control
_r7_dirs = _alt_directions()
_r7_filters = np.full((bc.NUM_SIGNAL_PARAMS, N_SIGNALS), -1, dtype=np.int64)
_r7_filters[0, 0:3] = 5
_r7_filters[0, 3:6] = 3
# remaining 6..9 stay at -1 (opt-out)
_r7_row = _base_param_row()
_r7_row[bc.PL_SIGNAL_P0] = 5.0
SCENARIOS.append(
    {
        "name": "row_7_pk_bilateral_opt_out_positive_control",
        "signals": _planted_signals(_r7_dirs, sig_filters=_r7_filters),
        "param_row": _r7_row,
        "expected_trades": 3 + 4,  # matching 5s + four -1 opt-outs
    }
)


# Row 8 — Pk trial-side truncation (D4)
_r8_dirs = _alt_directions()
_r8_filters = np.full((bc.NUM_SIGNAL_PARAMS, N_SIGNALS), -1, dtype=np.int64)
_r8_filters[0, 0:5] = 2
_r8_filters[0, 5:10] = 3
_r8_row = _base_param_row()
_r8_row[bc.PL_SIGNAL_P0] = 2.9  # post-D4: .round() → 3, not truncation to 2
SCENARIOS.append(
    {
        "name": "row_8_pk_trial_truncation_D4",
        "signals": _planted_signals(_r8_dirs, sig_filters=_r8_filters),
        "param_row": _r8_row,
        # Post-D4 fix: trial 2.9 rounds to 3. Signals with sig_filters[0]=3 admit
        # (indices 5..10 = 5 signals). Pre-fix it truncated to 2, so signals with
        # sig_filters[0]=2 (indices 0..5) admitted instead. Same count, different
        # signals; the count assertion happens to coincide.
        "expected_trades": 5,
    }
)


# Row 9 — ENGINE_DEFAULTS hole: P0 default 0.0 treated as active (D2)
_r9_dirs = _alt_directions()
_r9_filters = np.full((bc.NUM_SIGNAL_PARAMS, N_SIGNALS), -1, dtype=np.int64)
_r9_filters[0, 0:3] = 0
_r9_filters[0, 3:6] = 1
# 6..9 stay at -1 (opt-out)
_r9_row = _base_param_row()
# D2 fix lives in ff/encoding.py ENGINE_DEFAULTS — it changes encode() so an
# unregistered P0..P9 slot now defaults to -1.0 instead of 0.0. This row
# still exercises the Rust engine's "trial value 0 is active" behaviour
# (which is correct at the engine level), but in practice encode() now
# writes -1.0 unless an EA explicitly maps the slot. See
# test_encoding_defaults_for_pk_slots below for the encoder-layer assertion.
_r9_row[bc.PL_SIGNAL_P0] = 0.0
SCENARIOS.append(
    {
        "name": "row_9_engine_defaults_hole_P0_zero_D2",
        "signals": _planted_signals(_r9_dirs, sig_filters=_r9_filters),
        "param_row": _r9_row,
        "expected_trades": 3 + 4,  # three 0=0 matches + four -1 opt-outs
    }
)


# ── Fixture builder ──────────────────────────────────────────────────


def _build_price_data() -> dict:
    """All-flat OHLC across N_H bars. No price movement → no SL/TP fires."""
    m_h = np.full(N_M, ENTRY_PRICE, dtype=np.float64)
    m_l = np.full(N_M, ENTRY_PRICE, dtype=np.float64)
    m_c = np.full(N_M, ENTRY_PRICE, dtype=np.float64)
    m_s = np.zeros(N_M, dtype=np.float64)

    h_h = np.full(N_H, ENTRY_PRICE, dtype=np.float64)
    h_l = np.full(N_H, ENTRY_PRICE, dtype=np.float64)
    h_c = np.full(N_H, ENTRY_PRICE, dtype=np.float64)
    h_s = np.zeros(N_H, dtype=np.float64)

    map_start = (np.arange(N_H) * SUB_PER_BAR).astype(np.int64)
    map_end = ((np.arange(N_H) + 1) * SUB_PER_BAR).astype(np.int64)

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
    )


def _run_scenario(scenario: dict) -> int:
    prices = _build_price_data()
    sig = scenario["signals"]
    param_matrix = scenario["param_row"].reshape(1, -1)
    param_layout = np.arange(bc.NUM_PL, dtype=np.int64)
    metrics = np.zeros((1, bc.NUM_METRICS), dtype=np.float64)
    pnl = np.empty((1, MAX_TRADES), dtype=np.float64)
    trade_records = np.empty((1, (MAX_TRADES) * bc.NUM_TRADE_FIELDS), dtype=np.float64)

    bc.batch_evaluate(
        prices["h_h"],
        prices["h_l"],
        prices["h_c"],
        prices["h_s"],
        PIP,
        0.0,
        sig["bar_index"],
        sig["direction"],
        sig["entry_price"],
        sig["hour"],
        sig["day"],
        sig["atr_pips"],
        sig["swing_sl"],
        sig["filter_value"],
        sig["variant"],
        sig["sig_filters"],
        param_matrix,
        param_layout,
        metrics,
        MAX_TRADES,
        365.0 * 24.0,
        0.0,
        999.0,
        prices["m_h"],
        prices["m_l"],
        prices["m_c"],
        prices["m_s"],
        prices["map_start"],
        prices["map_end"],
        pnl,
        trade_records,
    )

    return int(metrics[0, _M_TRADES])


def test_encoding_defaults_for_pk_slots():
    """D2 fix: encode() with a mapping that does NOT register P0..P9 must
    write -1.0 into every Pk slot, not 0.0."""
    from ff import encoding as enc

    trial = {"signal_variant": 0}
    mapping = [(bc.PL_SIGNAL_VARIANT, enc.slot_int(("signal_variant",)))]
    pm = enc.encode([trial], mapping)
    for f in range(bc.NUM_SIGNAL_PARAMS):
        assert (
            pm[0, bc.PL_SIGNAL_P0 + f] == -1.0
        ), f"PL_SIGNAL_P{f} defaulted to {pm[0, bc.PL_SIGNAL_P0 + f]}, expected -1.0. D2 fix regressed."


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_signal_filter_scenario(scenario):
    n_trades = _run_scenario(scenario)
    expected = scenario["expected_trades"]
    assert n_trades == expected, (
        f"{scenario['name']}: engine admitted {n_trades} signals, "
        f"expected {expected}. Filter gate is behaving differently "
        f"from the hand-calculated expectation in "
        f"docs/validation/2026-04-19-signal-filters/03-behaviour-table.md."
    )
