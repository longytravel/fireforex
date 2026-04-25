"""Resample: OHLC aggregation, weekend gaps, spread averaging."""

from __future__ import annotations

from datetime import datetime, timezone

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


def _m1_frame(start: datetime, minutes: int) -> pd.DataFrame:
    """Deterministic walking price — open=1+i*1e-5, high=open+1e-5, low=open-1e-5."""
    ts = pd.date_range(start, periods=minutes, freq="1min", tz="UTC")
    base = 1.0 + pd.Series(range(minutes)) * 1e-5
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": base.values,
            "high": (base + 1e-5).values,
            "low": (base - 1e-5).values,
            "close": base.values,
            "volume": 1.0,
            "spread": 0.0002,
        }
    )


def test_60_m1_bars_roll_into_one_h1_row(tmp_root):
    df = _m1_frame(datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc), 60)
    df.to_parquet(tmp_root / "EUR_USD_M1.parquet", index=False)

    written = resample.derive_higher_tfs("EUR_USD", source_tf="M1", targets=("H1",))
    assert len(written) == 1
    h1 = pd.read_parquet(tmp_root / "EUR_USD_H1.parquet")
    assert len(h1) == 1

    row = h1.iloc[0]
    assert row["open"] == pytest.approx(1.0)  # first of 60
    assert row["close"] == pytest.approx(1.0 + 59 * 1e-5)  # last of 60
    assert row["high"] == pytest.approx(1.0 + 59 * 1e-5 + 1e-5)  # max
    assert row["low"] == pytest.approx(1.0 - 1e-5)  # min
    assert row["volume"] == pytest.approx(60.0)  # sum
    assert row["spread"] == pytest.approx(0.0002)  # mean


def test_weekend_gap_produces_no_empty_rows(tmp_root):
    # Friday 21:00 UTC → 60 bars, then Sunday 21:00 UTC → 60 bars. Saturday/
    # early-Sunday bars are missing from the source. The H1 resample must not
    # fabricate NaN rows for those empty hours.
    fri = _m1_frame(datetime(2024, 1, 5, 21, 0, tzinfo=timezone.utc), 60)
    sun = _m1_frame(datetime(2024, 1, 7, 21, 0, tzinfo=timezone.utc), 60)
    df = pd.concat([fri, sun], ignore_index=True)
    df.to_parquet(tmp_root / "EUR_USD_M1.parquet", index=False)

    resample.derive_higher_tfs("EUR_USD", source_tf="M1", targets=("H1",))
    h1 = pd.read_parquet(tmp_root / "EUR_USD_H1.parquet")

    assert not h1["open"].isna().any()
    assert len(h1) == 2  # one row for Friday 21:00, one for Sunday 21:00


def test_spread_is_averaged_not_summed(tmp_root):
    # 60 M1 bars with spread alternating 1e-4 and 3e-4 → mean = 2e-4.
    df = _m1_frame(datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc), 60)
    df["spread"] = [1e-4 if i % 2 == 0 else 3e-4 for i in range(60)]
    df.to_parquet(tmp_root / "EUR_USD_M1.parquet", index=False)

    resample.derive_higher_tfs("EUR_USD", source_tf="M1", targets=("H1",))
    h1 = pd.read_parquet(tmp_root / "EUR_USD_H1.parquet")
    assert h1.iloc[0]["spread"] == pytest.approx(2e-4)


def test_fan_out_writes_every_target(tmp_root):
    df = _m1_frame(datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc), 60 * 24 * 2)  # 2 days
    df.to_parquet(tmp_root / "EUR_USD_M1.parquet", index=False)

    targets = ("M5", "M15", "M30", "H1", "H4", "D")
    written = resample.derive_higher_tfs("EUR_USD", source_tf="M1", targets=targets)
    assert {p.stem.split("_")[-1] for p in written} == set(targets)
    for tf in targets:
        assert (tmp_root / f"EUR_USD_{tf}.parquet").exists()


def test_invalidates_inventory_cache(tmp_root):
    inventory._CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    inventory._CACHE_PATH.write_text('{"rows":[]}', encoding="utf-8")

    df = _m1_frame(datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc), 60)
    df.to_parquet(tmp_root / "EUR_USD_M1.parquet", index=False)

    resample.derive_higher_tfs("EUR_USD", source_tf="M1", targets=("H1",))
    assert not inventory._CACHE_PATH.exists()
