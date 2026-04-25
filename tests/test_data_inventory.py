"""Inventory scan, caching, and back-compat shape."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ff.data import inventory as inv


def _write_parquet(path: Path, bars: int = 100) -> None:
    idx = pd.date_range("2024-01-01", periods=bars, freq="h", tz="UTC")
    df = pd.DataFrame(
        {
            "timestamp": idx,
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.05,
            "volume": 0,
            "spread": 0.0001,
        }
    )
    df.to_parquet(path, index=False)


@pytest.fixture
def tmp_roots(tmp_path, monkeypatch):
    """Point inventory at a temp root with known files + isolate the cache."""
    _write_parquet(tmp_path / "EUR_USD_H1.parquet", bars=600)
    _write_parquet(tmp_path / "USD_JPY_M15.parquet", bars=800)
    cache = tmp_path / "inventory.json"
    monkeypatch.setattr(inv, "ROOTS", (tmp_path,))
    monkeypatch.setattr(inv, "_CACHE_PATH", cache)
    inv.invalidate()
    return tmp_path


def test_scan_returns_one_row_per_file(tmp_roots):
    rows = inv.scan(force=True)
    pairs = {(r["pair"], r["tf"]) for r in rows}
    assert ("EUR_USD", "H1") in pairs
    assert ("USD_JPY", "M15") in pairs


def test_scan_records_bars_and_range(tmp_roots):
    rows = inv.scan(force=True)
    h1 = next(r for r in rows if r["pair"] == "EUR_USD" and r["tf"] == "H1")
    assert h1["bars"] == 600
    assert h1["start_ts"] is not None
    assert h1["end_ts"] is not None
    assert h1["has_spread"] is True
    assert h1["status"] == "ok"


def test_scan_caches_to_disk(tmp_roots):
    inv.scan(force=True)
    assert inv._CACHE_PATH.exists()
    payload = json.loads(inv._CACHE_PATH.read_text(encoding="utf-8"))
    assert "rows" in payload and len(payload["rows"]) == 2


def test_inventory_by_pair_back_compat(tmp_roots):
    by_pair = inv.inventory_by_pair(force=True)
    assert by_pair["EUR_USD"] == ["H1"]
    assert by_pair["USD_JPY"] == ["M15"]


def test_invalidate_forces_rescan(tmp_roots):
    inv.scan(force=True)
    inv.invalidate()
    assert not inv._CACHE_PATH.exists()
