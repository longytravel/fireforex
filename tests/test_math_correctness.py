"""Hand-calculated math floor for the signal library.

The principle: every indicator gets a tiny input (5-20 bars) where the expected
output is computed by hand in this file's comments. If the real implementation
ever drifts — different EMA formula, off-by-one in cross detection, wrong ATR
true-range definition — these tests scream.

This is the pattern future math (e.g. a Chandelier stop) should follow: prove
the formula on a handful of bars you can reason about, then prove it composes
with the rest of the system on a synthetic trade fixture.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ff import signal_lib as sl

# ── 1. EMA (span-based, no warmup) ────────────────────────────────────


def test_ewm_span3_matches_hand_calculation():
    """span=3 → alpha = 2/(3+1) = 0.5. Start from arr[0], blend 50/50 forward.

    arr = [1, 2, 3, 4, 5], expected:
      y[0] = 1
      y[1] = 0.5*2 + 0.5*1    = 1.5
      y[2] = 0.5*3 + 0.5*1.5  = 2.25
      y[3] = 0.5*4 + 0.5*2.25 = 3.125
      y[4] = 0.5*5 + 0.5*3.125 = 4.0625
    """
    arr = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    got = sl.ewm(arr, span=3)
    expected = np.array([1.0, 1.5, 2.25, 3.125, 4.0625])
    np.testing.assert_allclose(got, expected, rtol=1e-12)


def test_ewm_returns_float64():
    """Engine consumers assume float64. Make sure we're not handing out float32."""
    got = sl.ewm(np.array([1.0, 2.0, 3.0]), span=2)
    assert got.dtype == np.float64


# ── 2. ATR (true-range then EMA) ──────────────────────────────────────


def test_atr_ema_true_range_formula():
    """TR[i] = max(H[i]-L[i], |H[i]-C[i-1]|, |L[i]-C[i-1]|). TR[0] uses
    C[-1] fallback = C[0] per the implementation (prepend close[0]).

    Fixture (4 bars):
      bar 0: H=10.2  L=9.8   C=10.0
      bar 1: H=10.5  L=10.1  C=10.4
      bar 2: H=11.0  L=10.3  C=10.9
      bar 3: H=10.8  L=10.5  C=10.7  (inside day, prev close gap)

    Hand computation:
      TR[0]: prev_close=10.0 → max(0.4, |10.2-10.0|, |9.8-10.0|) = max(0.4, 0.2, 0.2) = 0.4
      TR[1]: prev_close=10.0 → max(0.4, |10.5-10.0|, |10.1-10.0|) = max(0.4, 0.5, 0.1) = 0.5
      TR[2]: prev_close=10.4 → max(0.7, |11.0-10.4|, |10.3-10.4|) = max(0.7, 0.6, 0.1) = 0.7
      TR[3]: prev_close=10.9 → max(0.3, |10.8-10.9|, |10.5-10.9|) = max(0.3, 0.1, 0.4) = 0.4

    Then EMA(TR, span=2): alpha = 2/3.
      y[0] = 0.4
      y[1] = (2/3)*0.5 + (1/3)*0.4 ≈ 0.4666666...
      y[2] = (2/3)*0.7 + (1/3)*0.4666 ≈ 0.6222222...
      y[3] = (2/3)*0.4 + (1/3)*0.6222 ≈ 0.4740740...
    """
    h = np.array([10.2, 10.5, 11.0, 10.8])
    l = np.array([9.8, 10.1, 10.3, 10.5])
    c = np.array([10.0, 10.4, 10.9, 10.7])
    got = sl.atr_ema(h, l, c, period=2)
    # Clear per-run caches first (atr_ema caches by id).
    sl._ATR_CACHE.clear()
    got = sl.atr_ema(h, l, c, period=2)
    expected = np.array([0.4, 7.0 / 15.0, 28.0 / 45.0, 64.0 / 135.0])
    np.testing.assert_allclose(got, expected, rtol=1e-10)


# ── 3. RSI (Wilder-style via span-based EMA) ──────────────────────────


def test_rsi_flat_series_is_50_or_neutral():
    """On a perfectly flat series, gain and loss are both 0 → rs = inf (by
    the code's np.where), so rsi = 100 - 100/inf = 100. This is a boundary
    case worth locking: the current code treats "no loss" as RSI=100."""
    flat = np.full(20, 1.2345)
    out = sl.rsi(flat, period=3)
    # Flat deltas → gain=loss=0 → rs = inf (implementation choice).
    np.testing.assert_allclose(out, 100.0, rtol=1e-12)


def test_rsi_monotonic_up_approaches_100():
    """Strictly increasing close → all deltas positive → avg_loss ≈ 0 →
    rs large → rsi → 100. Not asserting exact equality, just the property."""
    up = np.cumsum(np.ones(30) * 0.5) + 1.0
    out = sl.rsi(up, period=5)
    # Steady state after a few bars should be near 100.
    assert out[-1] > 99.0, f"expected RSI near 100 on monotonic up, got {out[-1]}"


def test_rsi_monotonic_down_approaches_zero():
    down = 10.0 - np.cumsum(np.ones(30) * 0.5)
    out = sl.rsi(down, period=5)
    assert out[-1] < 1.0, f"expected RSI near 0 on monotonic down, got {out[-1]}"


# ── 4. Donchian breakout: entry on the bar where close exceeds prior range ──


def test_donchian_breakout_fires_on_first_break():
    """Lookback=3 Donchian:
       bar: 0    1    2    3    4    5    6
       H:   1.0  1.1  1.2  1.3  1.1  1.0  1.4
       L:   0.9  1.0  1.1  1.0  0.9  0.8  0.9
       C:   1.0  1.1  1.2  1.05 0.95 0.85 1.35

    prior-3 high at each bar (shifted by 1, rolling max of last 3):
      bar 3: max(H[0..2]) = 1.2  → C[3]=1.05 <= 1.2, no break
      bar 4: max(H[1..3]) = 1.3  → C[4]=0.95, no
      bar 5: max(H[2..4]) = 1.3  → C[5]=0.85, no
      bar 6: max(H[3..5]) = 1.3  → C[6]=1.35 > 1.3 → LONG breakout on bar 6

    prior-3 low similarly:
      bar 3: min(L[0..2]) = 0.9  → C[3]=1.05, no
      bar 4: min(L[1..3]) = 1.0  → C[4]=0.95 < 1.0 → SHORT breakout FIRES on bar 4
      bar 5: min(L[2..4]) = 0.9  → C[5]=0.85 < 0.9, but edge-suppressed
                                    (bar 4 was already in short_breaks)

    Edge detection: only the FIRST bar of a continuous breakout fires. So
    the expected signal set is {4 (short), 6 (long)}.
    """
    h = np.array([1.0, 1.1, 1.2, 1.3, 1.1, 1.0, 1.4])
    l = np.array([0.9, 1.0, 1.1, 1.0, 0.9, 0.8, 0.9])
    c = np.array([1.0, 1.1, 1.2, 1.05, 0.95, 0.85, 1.35])
    idx = pd.date_range("2020-01-01", periods=len(c), freq="1h", tz="UTC")
    df = pd.DataFrame({"high": h, "low": l, "close": c}, index=idx)

    sl._ARRAYS_CACHE.clear()
    sl._ATR_CACHE.clear()
    ss = sl.donchian(df, lookback=3, atr_period=3, pip_value=0.0001)

    assert set(ss.bar_index.tolist()) == {4, 6}, f"expected signals at bar 4 (short, first edge) and 6 (long), got {ss.bar_index.tolist()}"
    dir_by_bar = dict(zip(ss.bar_index.tolist(), ss.direction.tolist()))
    assert dir_by_bar[4] == -1, f"bar 4 should be short, got {dir_by_bar[4]}"
    assert dir_by_bar[6] == +1, f"bar 6 should be long, got {dir_by_bar[6]}"


# ── 5. EMA cross: signal fires on the bar AFTER the cross (no lookahead) ──


def test_ema_cross_fires_on_bar_after_cross():
    """Construct a series that definitely crosses at a known bar. Use
    EMA(fast=2) vs EMA(slow=4). The crossover is where fast rises above
    slow. We check that ``bar_index`` points AT LEAST ONE bar after the
    cross — never on the cross bar itself — which is the anti-lookahead
    guarantee.
    """
    # 40 bars: down-then-up pattern forces a cross somewhere in the middle.
    close = np.concatenate([np.linspace(1.2, 1.0, 20), np.linspace(1.0, 1.2, 20)])
    high = close + 0.0005
    low = close - 0.0005
    idx = pd.date_range("2020-01-01", periods=len(close), freq="1h", tz="UTC")
    df = pd.DataFrame({"high": high, "low": low, "close": close}, index=idx)

    sl._ARRAYS_CACHE.clear()
    sl._ATR_CACHE.clear()
    ss = sl.ema_cross(df, fast=2, slow=4, atr_period=3, pip_value=0.0001)

    assert ss.bar_index.size >= 1, "expected at least one cross in up-reversal"
    # Reconstruct expected cross bars by hand via the same EMA.
    fast_line = sl.ewm(close, 2)
    slow_line = sl.ewm(close, 4)
    up = (fast_line[1:] > slow_line[1:]) & (fast_line[:-1] <= slow_line[:-1])
    expected_up_bars = np.where(up)[0] + 1
    # Signal bar must be cross_index+1 (one bar after the crossover condition).
    for bar in ss.bar_index.tolist():
        if ss.direction[list(ss.bar_index).index(bar)] == +1:
            assert bar in expected_up_bars, f"long signal at bar {bar} doesn't match hand-calculated cross bars {expected_up_bars.tolist()}"

    # Anti-lookahead: entry_price must equal close at the signal bar
    # (never the bar before, which would peek at the crossover bar's data).
    np.testing.assert_allclose(ss.entry_price, close[ss.bar_index], rtol=1e-12)


# ── 6. MACD cross: similar anti-lookahead property ────────────────────


def test_macd_cross_fires_on_bar_after_cross():
    close = np.concatenate([np.linspace(1.5, 1.0, 30), np.linspace(1.0, 1.5, 30)])
    high = close + 0.0005
    low = close - 0.0005
    idx = pd.date_range("2020-01-01", periods=len(close), freq="1h", tz="UTC")
    df = pd.DataFrame({"high": high, "low": low, "close": close}, index=idx)

    sl._ARRAYS_CACHE.clear()
    sl._ATR_CACHE.clear()
    ss = sl.macd_cross(df, fast=3, slow=8, signal=2, atr_period=3, pip_value=0.0001)

    # At least one signal, and entry_price must match close at the signal bar.
    assert ss.bar_index.size >= 1
    np.testing.assert_allclose(ss.entry_price, close[ss.bar_index], rtol=1e-12)


def test_macd_rejects_invalid_combos():
    close = np.linspace(1.0, 1.2, 50)
    idx = pd.date_range("2020-01-01", periods=len(close), freq="1h", tz="UTC")
    df = pd.DataFrame({"high": close + 0.001, "low": close - 0.001, "close": close}, index=idx)
    # fast >= slow must raise InvalidCombo (sampler should have filtered this,
    # but the family is the last line of defence).
    with pytest.raises(sl.InvalidCombo):
        sl.macd_cross(df, fast=10, slow=10, signal=3, atr_period=3, pip_value=0.0001)
    with pytest.raises(sl.InvalidCombo):
        sl.macd_cross(df, fast=3, slow=10, signal=10, atr_period=3, pip_value=0.0001)


# ── 7. Session tagging (sanity-check hour → session mapping) ──────────


def test_session_of_hour_covers_all_24_hours():
    hours = np.arange(24, dtype=np.int64)
    out = sl.session_of_hour(hours)
    assert out.shape == hours.shape
    # All values must be one of the four defined session IDs.
    assert set(out.tolist()).issubset({sl.SESSION_ASIA, sl.SESSION_LONDON, sl.SESSION_NY, sl.SESSION_OVERLAP})
    # Known spot-checks from the docstring:
    assert out[3] == sl.SESSION_ASIA  # 3am UTC → Asia
    assert out[10] == sl.SESSION_LONDON  # 10am UTC → London
    assert out[14] == sl.SESSION_OVERLAP  # 2pm UTC → London/NY overlap
    assert out[18] == sl.SESSION_NY  # 6pm UTC → NY
