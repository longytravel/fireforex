"""Per-knob sensitivity tests — the floor against silent-ignore regressions.

For each management knob (trailing, break-even, partial, stale, max-bars)
we build TWO parameter rows that are identical except for that one knob.
We then call ``ff_core.batch_evaluate`` directly on both rows with the same
data and the same signal library. If the resulting trade count or total
PnL is **identical** between the two rows, the knob is silently ignored
and the test fails loudly.

These tests exist because today (2026-04-19) we discovered the harness
had been passing ``EXEC_BASIC`` for months, which meant trailing, BE,
partial, stale, and max-bars were ALL silently ignored in every sweep.
This file is the guardrail that makes sure it cannot happen again — for
these knobs, or for any new knob added later.

When adding a new management knob (e.g. a Chandelier stop) add a test
here that flips it and asserts the outcome differs.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import ff_core as bc

from ff import signal_lib as sl


# ── Synthetic data fixture ────────────────────────────────────────────

def _build_data():
    """Build 800 H1 bars of fake EUR/USD data with a 60-sub-bar M1 series.

    Uses a seeded random walk with deliberate trend segments so EMA crosses
    fire a sensible number of times. Returns everything batch_evaluate needs.
    """
    rng = np.random.default_rng(1234)
    n_h = 800
    n_m = n_h * 60

    # H1 close: small random walk with drift so the series isn't flat.
    drift = np.linspace(-0.02, 0.02, n_h)
    noise = rng.normal(0, 0.0015, n_h)
    h_c = 1.1 + np.cumsum(drift / n_h + noise)
    h_h = h_c + rng.uniform(0.0002, 0.0010, n_h)
    h_l = h_c - rng.uniform(0.0002, 0.0010, n_h)
    h_s = np.full(n_h, 0.0001)  # 1-pip spread

    # M1: interpolate closes linearly between hourly closes, add jitter.
    base = np.interp(np.arange(n_m), np.arange(n_h) * 60, h_c)
    m_c = base + rng.normal(0, 0.0002, n_m)
    m_h = m_c + rng.uniform(0.0001, 0.0003, n_m)
    m_l = m_c - rng.uniform(0.0001, 0.0003, n_m)
    m_s = np.full(n_m, 0.0001)

    # Main→sub mapping.
    map_start = (np.arange(n_h) * 60).astype(np.int64)
    map_end = ((np.arange(n_h) + 1) * 60).astype(np.int64)

    # Build one signal variant (ema_cross(5, 20)) by hand.
    idx = pd.date_range("2020-01-01", periods=n_h, freq="1h", tz="UTC")
    df = pd.DataFrame({"high": h_h, "low": h_l, "close": h_c}, index=idx)
    sl._ARRAYS_CACHE.clear()
    sl._ATR_CACHE.clear()
    ss = sl.ema_cross(df, fast=5, slow=20, atr_period=14, pip_value=0.0001)
    assert ss.bar_index.size >= 10, f"need at least 10 signals, got {ss.bar_index.size}"
    bar_index = ss.bar_index.astype(np.int64)
    direction = ss.direction.astype(np.int64)
    entry_price = ss.entry_price.astype(np.float64)
    hour = ss.hour.astype(np.int64)
    day = ss.day.astype(np.int64)
    atr_pips = ss.atr_pips.astype(np.float64)
    swing_sl = np.zeros(bar_index.size, dtype=np.float64)
    filter_value = np.zeros(bar_index.size, dtype=np.float64)
    variant = np.zeros(bar_index.size, dtype=np.int64)
    n_sig = bar_index.size
    sig_filters = np.full((bc.NUM_SIGNAL_PARAMS, n_sig), -1, dtype=np.int64)

    return dict(
        h_h=h_h, h_l=h_l, h_c=h_c, h_s=h_s,
        m_h=m_h, m_l=m_l, m_c=m_c, m_s=m_s,
        map_start=map_start, map_end=map_end,
        bar_index=bar_index, direction=direction, entry_price=entry_price,
        hour=hour, day=day, atr_pips=atr_pips,
        swing_sl=swing_sl, filter_value=filter_value, variant=variant,
        sig_filters=sig_filters, n_sig=n_sig,
    )


# Mode enum + metric-column constants — not re-exported from ff_core, so
# define locally. Source: core/src/constants.rs.
_SL_FIXED_PIPS = 0
_TP_FIXED_PIPS = 2
_TRAIL_OFF = 0
_TRAIL_FIXED_PIP = 1

_M_TRADES = 0
_M_RETURN_PCT = 6


def _baseline_row() -> np.ndarray:
    """A single parameter row with SL/TP set and every management knob OFF."""
    row = np.zeros(bc.NUM_PL, dtype=np.float64)
    row[bc.PL_SIGNAL_VARIANT] = 0
    row[bc.PL_SL_MODE] = _SL_FIXED_PIPS
    row[bc.PL_SL_FIXED_PIPS] = 15.0
    row[bc.PL_TP_MODE] = _TP_FIXED_PIPS
    row[bc.PL_TP_FIXED_PIPS] = 30.0
    row[bc.PL_HOURS_START] = 0
    row[bc.PL_HOURS_END] = 23
    row[bc.PL_DAYS_BITMASK] = 127  # all days
    row[bc.PL_BUY_FILTER_MAX] = -1
    row[bc.PL_SELL_FILTER_MIN] = -1
    # All management fields stay at zero → equivalent to OFF / disabled.
    return row


def _run(data: dict, param_matrix: np.ndarray) -> np.ndarray:
    """Call batch_evaluate on a (n_trials, NUM_PL) param matrix, return metrics."""
    n_trials = param_matrix.shape[0]
    max_trades = data["n_sig"]  # upper bound — one trade per signal
    metrics = np.zeros((n_trials, bc.NUM_METRICS), dtype=np.float64)
    pnl = np.empty((n_trials, max_trades), dtype=np.float64)
    trade_records = np.empty((n_trials, max_trades * bc.NUM_TRADE_FIELDS),
                             dtype=np.float64)
    param_layout = np.arange(bc.NUM_PL, dtype=np.int64)
    bc.batch_evaluate(
        data["h_h"], data["h_l"], data["h_c"], data["h_s"],
        0.0001, 0.0,            # pip_value, slippage
        data["bar_index"], data["direction"], data["entry_price"],
        data["hour"], data["day"], data["atr_pips"],
        data["swing_sl"], data["filter_value"], data["variant"],
        data["sig_filters"],
        param_matrix, param_layout,
        metrics,
        max_trades, 365.0 * 24.0,  # bars_per_year for H1
        0.0, 999.0,                # commission_pips, max_spread_pips
        data["m_h"], data["m_l"], data["m_c"], data["m_s"],
        data["map_start"], data["map_end"],
        pnl,
        trade_records,
    )
    return metrics


@pytest.fixture(scope="module")
def data():
    return _build_data()


def _assert_knob_moves(data, knob_name: str, mutate_row):
    """Build two rows that differ only by ``mutate_row`` and assert the trade
    count or PnL differs under batch_evaluate."""
    off_row = _baseline_row()
    on_row = _baseline_row()
    mutate_row(on_row)
    pm = np.stack([off_row, on_row])
    metrics = _run(data, pm)
    off_trades, on_trades = int(metrics[0, _M_TRADES]), int(metrics[1, _M_TRADES])
    off_ret, on_ret = float(metrics[0, _M_RETURN_PCT]), float(metrics[1, _M_RETURN_PCT])
    moved = (off_trades != on_trades) or (abs(off_ret - on_ret) > 1e-9)
    assert moved, (
        f"{knob_name} is SILENTLY IGNORED — flipping it produced identical "
        f"metrics. trades off={off_trades} on={on_trades}; "
        f"return off={off_ret} on={on_ret}. "
        f"This means the Rust engine is not honouring the knob."
    )


# ── The tests ─────────────────────────────────────────────────────────

def test_max_bars_knob_moves_outcomes(data):
    def mut(row):
        row[bc.PL_MAX_BARS] = 3  # force very-early time exit
    _assert_knob_moves(data, "max_bars", mut)


def test_trailing_stop_knob_moves_outcomes(data):
    def mut(row):
        row[bc.PL_TRAILING_MODE] = _TRAIL_FIXED_PIP
        row[bc.PL_TRAIL_ACTIVATE] = 5.0
        row[bc.PL_TRAIL_DISTANCE] = 8.0
    _assert_knob_moves(data, "trailing", mut)


def test_breakeven_knob_moves_outcomes(data):
    def mut(row):
        row[bc.PL_BREAKEVEN_ENABLED] = 1
        row[bc.PL_BREAKEVEN_TRIGGER] = 5.0
        row[bc.PL_BREAKEVEN_OFFSET] = 1.0
    _assert_knob_moves(data, "breakeven", mut)


def test_partial_close_knob_moves_outcomes(data):
    def mut(row):
        row[bc.PL_PARTIAL_ENABLED] = 1
        row[bc.PL_PARTIAL_PCT] = 50.0
        row[bc.PL_PARTIAL_TRIGGER] = 5.0
    _assert_knob_moves(data, "partial", mut)


def test_stale_exit_knob_moves_outcomes(data):
    # Rust condition (trade_full.rs:121): max_range < stale_atr_thresh * atr_pips.
    # Use a very permissive threshold so it essentially always fires after
    # stale_bars elapsed. That gives us a large effect size to detect.
    def mut(row):
        row[bc.PL_STALE_ENABLED] = 1
        row[bc.PL_STALE_BARS] = 2
        row[bc.PL_STALE_ATR_THRESH] = 100.0
    _assert_knob_moves(data, "stale", mut)


def test_chandelier_knob_moves_outcomes(data):
    # Peak-anchored ATR trailing added 2026-04-19. Use aggressive
    # params (arm at +1 pip, very tight 0.5-ATR distance) so the
    # chandelier SL beats the baseline 15p SL on any modestly-
    # profitable move — guaranteeing the knob affects at least
    # some trades on the random-walk fixture.
    def mut(row):
        row[bc.PL_CHANDELIER_ENABLED] = 1
        row[bc.PL_CHANDELIER_ACTIVATE] = 1.0
        row[bc.PL_CHANDELIER_ATR_MULT] = 0.5
    _assert_knob_moves(data, "chandelier", mut)


def test_session_filter_knob_moves_outcomes(data):
    """This one should already work under EXEC_BASIC — use it as a sanity check.
    If this fails, something much deeper is broken."""
    def mut(row):
        row[bc.PL_HOURS_START] = 9
        row[bc.PL_HOURS_END] = 10  # only 1 hour of trading
    _assert_knob_moves(data, "session_hours", mut)


def test_signal_variant_filter_knob_moves_outcomes(data):
    """Variant 0 exists in the fixture; variant 1 does not.
    Switching the trial's variant to 1 admits zero signals."""
    def mut(row):
        row[bc.PL_SIGNAL_VARIANT] = 1
    _assert_knob_moves(data, "signal_variant", mut)


def test_buy_filter_knob_moves_outcomes(data):
    """Fixture signals have filter_value = 0.0. Trial buy_filter_max = 7.0
    forces every long signal to fail equality and be rejected."""
    def mut(row):
        row[bc.PL_BUY_FILTER_MAX] = 7.0
    _assert_knob_moves(data, "buy_filter_max", mut)


def test_sell_filter_knob_moves_outcomes(data):
    """Fixture signals have filter_value = 0.0. Trial sell_filter_min = 7.0
    forces every short signal to fail equality and be rejected."""
    def mut(row):
        row[bc.PL_SELL_FILTER_MIN] = 7.0
    _assert_knob_moves(data, "sell_filter_min", mut)
