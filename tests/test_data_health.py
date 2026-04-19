"""Health checks: NaN / OHLC / timestamp / weekend-gap semantics."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ff import harness
from ff.data import health


def _build(tmp_path: Path, rows: list[dict], name: str = "EUR_USD_H1.parquet") -> Path:
    df = pd.DataFrame(rows)
    path = tmp_path / name
    df.to_parquet(path, index=False)
    # Bust the harness parquet cache so each test re-reads the fresh file.
    harness._PARQUET_CACHE.clear()
    return path


def _clean_row(ts, o=1.0, h=1.1, l=0.9, c=1.0):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c,
            "volume": 0, "spread": 0.0001}


@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "DATA_ROOT", tmp_path)
    return tmp_path


def test_clean_file_is_ok(tmp_root):
    idx = pd.date_range("2024-01-08 00:00", periods=48, freq="h", tz="UTC")  # Mon-Tue
    _build(tmp_root, [_clean_row(t) for t in idx])
    r = health.check("EUR_USD", "H1")
    assert r["summary"] == "ok"
    assert r["bars"] == 48


def test_ohlc_violation_flags_fail(tmp_root):
    idx = pd.date_range("2024-01-08 00:00", periods=10, freq="h", tz="UTC")
    rows = [_clean_row(t) for t in idx]
    rows[3]["high"] = 0.5  # high < low
    _build(tmp_root, rows)
    r = health.check("EUR_USD", "H1")
    assert r["summary"] == "fail"
    assert r["ohlc_violations"]["high_lt_low"] == 1


def test_nan_flags_warn(tmp_root):
    idx = pd.date_range("2024-01-08 00:00", periods=10, freq="h", tz="UTC")
    rows = [_clean_row(t) for t in idx]
    rows[2]["close"] = float("nan")
    _build(tmp_root, rows)
    r = health.check("EUR_USD", "H1")
    assert r["summary"] == "warn"
    assert r["nan_counts"]["close"] == 1


def test_weekend_gap_is_not_flagged(tmp_root):
    # Fri 22:00 UTC → Sun 22:00 UTC is the expected FX close window.
    # A gap that lives fully inside that window should NOT trigger a warning.
    fri = pd.Timestamp("2024-01-12 21:00", tz="UTC")  # Friday 21:00
    sun = pd.Timestamp("2024-01-14 23:00", tz="UTC")  # Sunday 23:00
    rows = [_clean_row(fri), _clean_row(sun),
            _clean_row(sun + pd.Timedelta(hours=1))]
    _build(tmp_root, rows)
    r = health.check("EUR_USD", "H1")
    real_gaps = [g for g in r["gap_samples"] if "_more" not in g]
    assert real_gaps == []


def test_midweek_gap_is_flagged(tmp_root):
    # Tuesday to Wednesday 12h gap — nothing to do with weekends.
    t0 = pd.Timestamp("2024-01-09 00:00", tz="UTC")  # Tue
    t1 = t0 + pd.Timedelta(hours=12)                 # Tue noon
    t2 = t1 + pd.Timedelta(hours=1)                  # +1h
    rows = [_clean_row(t0), _clean_row(t1), _clean_row(t2)]
    # Only two "gaps" — between [t0,t1] (12h, > 2×1h → flagged).
    _build(tmp_root, rows)
    r = health.check("EUR_USD", "H1")
    real = [g for g in r["gap_samples"] if "_more" not in g]
    assert len(real) >= 1
