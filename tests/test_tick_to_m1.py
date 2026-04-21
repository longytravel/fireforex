"""Tick → M1 aggregation correctness."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from ff import harness
from ff.data import inventory, resample


@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(inventory, "ROOTS", (tmp_path,))
    monkeypatch.setattr(inventory, "_CACHE_PATH", tmp_path / "inv.json")
    harness._PARQUET_CACHE.clear()
    return tmp_path


def _tick_frame(start: datetime, rows: list[tuple[int, float, float]]) -> pd.DataFrame:
    """`rows` = list of (offset_ms, bid, ask) triples."""
    return pd.DataFrame({
        "timestamp":  [start + pd.Timedelta(ms, unit="ms") for ms, _, _ in rows],
        "bid":        [b for _, b, _ in rows],
        "ask":        [a for _, _, a in rows],
        "bid_volume": [0.5] * len(rows),
        "ask_volume": [0.5] * len(rows),
    })


def test_120s_of_ticks_produce_two_m1_rows(tmp_root):
    # Minute A (10:00:00 + 0..30s): bids 1.2000 / asks 1.2002 → mid 1.2001
    # Minute B (10:01:00 + 0..30s): bids 1.2010 / asks 1.2014 → mid 1.2012
    start = datetime(2024, 1, 2, 10, 0, 0, tzinfo=timezone.utc)
    rows = [
        (0,      1.2000, 1.2002),
        (10_000, 1.2000, 1.2002),
        (20_000, 1.2000, 1.2002),
        (60_000, 1.2010, 1.2014),     # new minute boundary
        (70_000, 1.2010, 1.2014),
        (80_000, 1.2010, 1.2014),
    ]
    df = _tick_frame(start, rows)
    df.to_parquet(tmp_root / "EUR_USD_TICK.parquet", index=False)

    out = resample.tick_to_m1("EUR_USD")
    m1 = pd.read_parquet(out)
    assert len(m1) == 2

    r0, r1 = m1.iloc[0], m1.iloc[1]
    assert r0["open"] == pytest.approx(1.2001)
    assert r0["close"] == pytest.approx(1.2001)
    assert r0["spread"] == pytest.approx(0.0002)
    assert r1["open"] == pytest.approx(1.2012)
    assert r1["close"] == pytest.approx(1.2012)
    assert r1["spread"] == pytest.approx(0.0004)


def test_missing_tick_file_raises(tmp_root):
    with pytest.raises(FileNotFoundError):
        resample.tick_to_m1("EUR_USD")


def test_mid_high_low_use_both_bid_and_ask(tmp_root):
    # Mid walks up from 1.2001 to 1.2005 inside a single minute; the M1 high
    # must come from the final tick (highest mid), low from the first.
    start = datetime(2024, 1, 2, 10, 0, 0, tzinfo=timezone.utc)
    rows = [
        (0,      1.2000, 1.2002),   # mid 1.2001 (low)
        (5_000,  1.2001, 1.2003),   # mid 1.2002
        (10_000, 1.2003, 1.2005),   # mid 1.2004
        (20_000, 1.2004, 1.2006),   # mid 1.2005 (high)
        (30_000, 1.2002, 1.2004),   # mid 1.2003 (close)
    ]
    df = _tick_frame(start, rows)
    df.to_parquet(tmp_root / "EUR_USD_TICK.parquet", index=False)

    resample.tick_to_m1("EUR_USD")
    m1 = pd.read_parquet(tmp_root / "EUR_USD_M1.parquet")
    r = m1.iloc[0]
    assert r["open"] == pytest.approx(1.2001)
    assert r["low"] == pytest.approx(1.2001)
    assert r["high"] == pytest.approx(1.2005)
    assert r["close"] == pytest.approx(1.2003)
