"""Downloader: normalisation, append math, crash-safe write."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from ff import harness
from ff.data import downloader, inventory


@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(inventory, "ROOTS", (tmp_path,))
    monkeypatch.setattr(inventory, "_CACHE_PATH", tmp_path / "inv.json")
    harness._PARQUET_CACHE.clear()
    return tmp_path


def _fake_bars(start: date, end: date) -> pd.DataFrame:
    ts = pd.date_range(
        datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
        datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc),
        freq="h",
    )
    return pd.DataFrame({
        "timestamp": ts,
        "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0,
        "volume": 0, "spread": 0.0001,
    })


def test_full_download_writes_parquet(tmp_root, monkeypatch):
    def fake_fetch(**kwargs):
        # dukascopy_python.fetch is not needed — we replace _fetch_chunk below.
        raise AssertionError("unreachable")

    monkeypatch.setattr(
        downloader, "_fetch_chunk",
        lambda pair, tf, s, e, log_cb: _fake_bars(s, e),
    )

    out = downloader.download("EUR_USD", "H1",
                              date(2024, 1, 1), date(2024, 1, 1), append=False)
    path = tmp_root / "EUR_USD_H1.parquet"
    assert path.exists()
    assert out["new_bars"] > 0
    df = pd.read_parquet(path)
    assert "spread" in df.columns  # normalisation fills it


def test_append_only_fetches_tail(tmp_root, monkeypatch):
    # Seed an existing file up to 2024-01-02.
    seed = _fake_bars(date(2024, 1, 1), date(2024, 1, 2))
    (tmp_root / "EUR_USD_H1.parquet").write_bytes(b"")  # reserve the name
    seed["spread"] = 0.0
    seed.to_parquet(tmp_root / "EUR_USD_H1.parquet", index=False)

    observed_starts: list = []

    def spy(pair, tf, s, e, log_cb):
        observed_starts.append(s)
        return _fake_bars(s, e)

    monkeypatch.setattr(downloader, "_fetch_chunk", spy)

    out = downloader.download("EUR_USD", "H1",
                              date(2024, 1, 1), date(2024, 1, 5), append=True)
    # The first fetched chunk must start *after* the existing max ts.
    assert observed_starts and observed_starts[0] >= date(2024, 1, 2)
    assert out["appended"] is True
    assert out["new_bars"] > 0


def test_download_invalidates_inventory_cache(tmp_root, monkeypatch):
    # Prime a stale cache file so we can confirm invalidate() clears it.
    inventory._CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    inventory._CACHE_PATH.write_text('{"rows":[]}', encoding="utf-8")

    monkeypatch.setattr(
        downloader, "_fetch_chunk",
        lambda pair, tf, s, e, log_cb: _fake_bars(s, e),
    )
    downloader.download("EUR_USD", "H1",
                        date(2024, 1, 1), date(2024, 1, 1), append=False)
    assert not inventory._CACHE_PATH.exists()
