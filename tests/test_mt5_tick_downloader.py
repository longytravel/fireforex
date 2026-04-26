"""Tests for ff.data.mt5_tick_downloader.

Covers the parts that don't require a live MT5 terminal: the dedupe
behaviour on merge, and the broker-UTC-offset stale-tick guard.
"""

from __future__ import annotations

import time as _time
from types import SimpleNamespace

import pandas as pd
import pytest

from ff.data import mt5_tick_downloader as td


def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df[td._TICK_SCHEMA]


def test_merge_preserves_distinct_same_ms_ticks() -> None:
    """Two ticks at the same millisecond with different bid/ask must both survive."""
    ts = "2026-04-26T12:00:00.123456+00:00"
    existing = _frame(
        [
            {"timestamp": ts, "bid": 1.1000, "ask": 1.1002, "bid_volume": 1.0, "ask_volume": 1.0},
        ]
    )
    new_df = _frame(
        [
            # Same ms, different ask — a legitimate distinct quote.
            {"timestamp": ts, "bid": 1.1000, "ask": 1.1003, "bid_volume": 1.0, "ask_volume": 1.0},
            # Later tick.
            {
                "timestamp": "2026-04-26T12:00:00.456000+00:00",
                "bid": 1.1001,
                "ask": 1.1003,
                "bid_volume": 2.0,
                "ask_volume": 2.0,
            },
        ]
    )

    merged = td._merge_tick_frames(existing, new_df)

    assert len(merged) == 3
    same_ms = merged[merged["timestamp"] == pd.Timestamp(ts)]
    assert len(same_ms) == 2
    assert sorted(same_ms["ask"].tolist()) == [1.1002, 1.1003]


def test_merge_drops_exact_duplicate_rows() -> None:
    """Identical rows from overlapping fetches collapse to a single row."""
    row = {
        "timestamp": "2026-04-26T12:00:00.123456+00:00",
        "bid": 1.1000,
        "ask": 1.1002,
        "bid_volume": 1.0,
        "ask_volume": 1.0,
    }
    merged = td._merge_tick_frames(_frame([row]), _frame([row]))
    assert len(merged) == 1


def test_broker_offset_uses_fresh_tick() -> None:
    """When the broker tick is recent, offset = -(tick.time - now)."""
    now = int(_time.time())
    fake_mt5 = SimpleNamespace(
        symbol_info_tick=lambda symbol: SimpleNamespace(time=now + 3 * 3600),
    )
    offset = td._broker_utc_offset_sec(fake_mt5, "EURUSD")
    assert offset == pytest.approx(-3 * 3600, abs=2)


def test_broker_offset_stale_tick_falls_back_to_zero() -> None:
    """A 2-day-stale tick (weekend run) must not produce a 2-day offset."""
    now = int(_time.time())
    stale_time = now - 2 * 24 * 3600  # broker last quoted 2 days ago
    fake_mt5 = SimpleNamespace(
        symbol_info_tick=lambda symbol: SimpleNamespace(time=stale_time),
    )
    logs: list[str] = []
    offset = td._broker_utc_offset_sec(fake_mt5, "EURUSD", log_cb=logs.append)
    assert offset == 0
    assert any("market closed" in line.lower() or "stale" in line.lower() for line in logs)


def test_broker_offset_no_tick_falls_back_to_zero() -> None:
    fake_mt5 = SimpleNamespace(symbol_info_tick=lambda symbol: None)
    assert td._broker_utc_offset_sec(fake_mt5, "EURUSD") == 0
