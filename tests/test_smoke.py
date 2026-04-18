"""Smoke tests — prove the core loop works on synthetic data without any Parquet file."""
from __future__ import annotations

import sys
from dataclasses import fields
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fire_forex_v0 import DEFAULT_PARAMS, Params, run_backtest
from fire_forex_v0.strategy import compute_signals


@pytest.fixture(scope="module")
def synthetic_m1() -> pd.DataFrame:
    rng = np.random.default_rng(0)
    n = 20_000
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    # Random walk on log returns → positive-only close, believable OHLC
    ret = rng.normal(0.0, 0.00015, size=n)
    close = 1.10 * np.exp(np.cumsum(ret))
    spread = np.abs(rng.normal(0.0, 0.00005, size=n))
    high = close + spread
    low = close - spread
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def test_param_count_is_50() -> None:
    assert len(fields(Params)) == 50


def test_defaults_backtest_runs(synthetic_m1: pd.DataFrame) -> None:
    res = run_backtest(synthetic_m1, DEFAULT_PARAMS)
    assert res.trade_count >= 0
    assert np.isfinite(res.score) or res.score == -1e9


def test_signals_align_with_data(synthetic_m1: pd.DataFrame) -> None:
    sig = compute_signals(synthetic_m1, DEFAULT_PARAMS)
    for key in ("entries", "exits", "short_entries", "short_exits", "atr"):
        assert len(sig[key]) == len(synthetic_m1), f"{key} length mismatch"


def test_backtest_respects_disabled_days(synthetic_m1: pd.DataFrame) -> None:
    # Kill all day filters → no trades possible
    p = Params(**{**DEFAULT_PARAMS.as_dict(),
                  "day_mon": False, "day_tue": False, "day_wed": False,
                  "day_thu": False, "day_fri": False})
    res = run_backtest(synthetic_m1, p)
    assert res.trade_count == 0


def test_degenerate_periods_are_safe(synthetic_m1: pd.DataFrame) -> None:
    # Optuna can propose ema_fast > ema_slow; strategy must not crash
    p = Params(**{**DEFAULT_PARAMS.as_dict(), "ema_fast": 200, "ema_slow": 50})
    res = run_backtest(synthetic_m1, p)
    assert np.isfinite(res.score) or res.score == -1e9
