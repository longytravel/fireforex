"""Phase 1 guardrails: build_signal_library disk cache must be byte-identical
to a live build and must invalidate on grid / mtime changes.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ff import schema as sc
from ff import signal_lib as sl


@pytest.fixture
def h1_df() -> pd.DataFrame:
    rng = np.random.default_rng(0)
    n = 4000
    idx = pd.date_range("2020-01-01", periods=n, freq="1h", tz="UTC")
    close = 1.1 + np.cumsum(rng.normal(0, 0.0005, n))
    high = close + rng.uniform(0, 0.0008, n)
    low = close - rng.uniform(0, 0.0008, n)
    return pd.DataFrame({"high": high, "low": low, "close": close}, index=idx)


@pytest.fixture
def signals_cfg() -> dict:
    return {
        "ema_cross": {
            "fast": sc.IntRange(5, 15, step=5),
            "slow": sc.IntRange(20, 40, step=10),
        },
        "donchian": {"lookback": sc.IntRange(10, 30, step=10)},
    }


def _write_parquet(df: pd.DataFrame, tmp_path) -> "Path":
    p = tmp_path / "EUR_USD_H1.parquet"
    df.to_parquet(p)
    return p


def _arrays_equal(a: sl.SignalLibrary, b: sl.SignalLibrary) -> bool:
    if a.variant_map != b.variant_map:
        return False
    for name in (
        "bar_index",
        "direction",
        "entry_price",
        "atr_pips",
        "hour",
        "day",
        "filter_value",
        "swing_sl",
        "variant",
    ):
        if not np.array_equal(getattr(a, name), getattr(b, name)):
            return False
    return True


def test_cache_roundtrip_is_byte_identical(tmp_path, monkeypatch, h1_df, signals_cfg):
    monkeypatch.setattr(sl, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.delenv("FF_NO_CACHE", raising=False)
    parquet = _write_parquet(h1_df, tmp_path)

    live = sl.build_signal_library(
        signals_cfg,
        h1_df,
        pip_value=0.0001,
        atr_period=14,
        data_path=parquet,
        use_cache=False,
    )
    # First cached call writes; second reads.
    first = sl.build_signal_library(
        signals_cfg,
        h1_df,
        pip_value=0.0001,
        atr_period=14,
        data_path=parquet,
    )
    cached = sl.build_signal_library(
        signals_cfg,
        h1_df,
        pip_value=0.0001,
        atr_period=14,
        data_path=parquet,
    )
    assert _arrays_equal(live, first)
    assert _arrays_equal(live, cached)


def test_cache_invalidates_on_grid_change(tmp_path, monkeypatch, h1_df, signals_cfg):
    monkeypatch.setattr(sl, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.delenv("FF_NO_CACHE", raising=False)
    parquet = _write_parquet(h1_df, tmp_path)

    sl.build_signal_library(signals_cfg, h1_df, pip_value=0.0001, atr_period=14, data_path=parquet)
    other = dict(signals_cfg)
    other["donchian"] = {"lookback": sc.IntRange(10, 50, step=10)}
    live_other = sl.build_signal_library(
        other,
        h1_df,
        pip_value=0.0001,
        atr_period=14,
        data_path=parquet,
        use_cache=False,
    )
    cached_other = sl.build_signal_library(
        other,
        h1_df,
        pip_value=0.0001,
        atr_period=14,
        data_path=parquet,
    )
    assert _arrays_equal(live_other, cached_other)


def test_parallel_build_matches_serial(tmp_path, monkeypatch, h1_df, signals_cfg):
    monkeypatch.setattr(sl, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("FF_NO_CACHE", "1")

    monkeypatch.setenv("FF_PARALLEL", "0")
    serial = sl.build_signal_library(
        signals_cfg,
        h1_df,
        pip_value=0.0001,
        atr_period=14,
        use_cache=False,
    )

    monkeypatch.setenv("FF_PARALLEL", "1")
    parallel = sl.build_signal_library(
        signals_cfg,
        h1_df,
        pip_value=0.0001,
        atr_period=14,
        use_cache=False,
    )
    assert _arrays_equal(serial, parallel), "parallel build must be byte-identical to serial"


def test_cache_invalidates_on_window_slice(tmp_path, monkeypatch, h1_df, signals_cfg):
    """Harness aligns main_df to the shared main/sub window. A different window
    is a different input, even if the source parquet file is untouched.
    """
    monkeypatch.setattr(sl, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.delenv("FF_NO_CACHE", raising=False)
    parquet = _write_parquet(h1_df, tmp_path)

    full = sl.build_signal_library(
        signals_cfg,
        h1_df,
        pip_value=0.0001,
        atr_period=14,
        data_path=parquet,
    )
    # Simulate a window shift — as if sub_df coverage moved.
    sliced = h1_df.iloc[500:].copy()
    live_sliced = sl.build_signal_library(
        signals_cfg,
        sliced,
        pip_value=0.0001,
        atr_period=14,
        data_path=parquet,
        use_cache=False,
    )
    cached_sliced = sl.build_signal_library(
        signals_cfg,
        sliced,
        pip_value=0.0001,
        atr_period=14,
        data_path=parquet,
    )
    assert _arrays_equal(live_sliced, cached_sliced), "must match a fresh build of the sliced window"
    assert not _arrays_equal(full, cached_sliced), "sliced result must differ from full-window cache"


def test_cache_invalidates_on_source_edit(tmp_path, monkeypatch, h1_df, signals_cfg):
    """Simulate an edit to ff/signal_lib.py: its _source_hash changes →
    cache key changes → no stale hit."""
    monkeypatch.setattr(sl, "_CACHE_DIR", tmp_path / "cache")
    monkeypatch.delenv("FF_NO_CACHE", raising=False)
    parquet = _write_parquet(h1_df, tmp_path)

    sl.build_signal_library(signals_cfg, h1_df, pip_value=0.0001, atr_period=14, data_path=parquet)
    # Pretend someone tweaked ema_cross: swap in a fake source hash.
    monkeypatch.setattr(sl, "_source_hash", lambda: "editedxxxxxxxxxx")
    # Different key means a cache miss → should rebuild live and still match.
    rebuilt = sl.build_signal_library(
        signals_cfg,
        h1_df,
        pip_value=0.0001,
        atr_period=14,
        data_path=parquet,
    )
    # There are now two cache files: one per source hash.
    cache_files = list((tmp_path / "cache").glob("*.npz"))
    assert len(cache_files) == 2
    # The rebuilt result must still equal a live build (source content didn't really change).
    live = sl.build_signal_library(
        signals_cfg,
        h1_df,
        pip_value=0.0001,
        atr_period=14,
        data_path=parquet,
        use_cache=False,
    )
    assert _arrays_equal(live, rebuilt)


def test_ff_no_cache_env_bypasses_cache(tmp_path, monkeypatch, h1_df, signals_cfg):
    monkeypatch.setattr(sl, "_CACHE_DIR", tmp_path / "cache")
    parquet = _write_parquet(h1_df, tmp_path)

    monkeypatch.setenv("FF_NO_CACHE", "1")
    sl.build_signal_library(signals_cfg, h1_df, pip_value=0.0001, atr_period=14, data_path=parquet)
    # With FF_NO_CACHE set, nothing should have been written.
    assert not (tmp_path / "cache").exists() or not list((tmp_path / "cache").glob("*.npz"))
