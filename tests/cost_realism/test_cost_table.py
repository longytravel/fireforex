import json
from pathlib import Path

import pandas as pd
import pytest

from ff.cost_realism import cost_table as ct


def _write_fixture_parquet(path: Path, pair: str, *, days: int = 30) -> None:
    """Create a synthetic MT5 M1 parquet for `pair` covering `days` weekdays."""
    rows = []
    start = pd.Timestamp("2026-03-01 00:00:00")
    pip = 0.01 if "JPY" in pair else 0.0001
    for d in range(days):
        ts = start + pd.Timedelta(days=d)
        if ts.dayofweek >= 5:
            continue
        for minute in range(24 * 60):
            ts_min = ts + pd.Timedelta(minutes=minute)
            hour = ts_min.hour
            spread_pips = 0.1 if 8 <= hour < 21 else 0.5
            spread = spread_pips * pip
            rows.append(
                {
                    "timestamp": ts_min,
                    "open": 1.10000,
                    "high": 1.10010,
                    "low": 1.09990,
                    "close": 1.10005,
                    "volume": 1.0,
                    "spread": spread,
                }
            )
    pd.DataFrame(rows).to_parquet(path, index=False)


def test_build_cost_table_per_session_medians(tmp_path):
    mt5_root = tmp_path / "BackTestData_MT5"
    mt5_root.mkdir()
    _write_fixture_parquet(mt5_root / "EUR_USD_M1.parquet", "EUR_USD")

    out_path = tmp_path / "cost_table.json"
    ct.build_cost_table(
        pairs=["EUR_USD"],
        mt5_root=mt5_root,
        out_path=out_path,
    )

    table = json.loads(out_path.read_text())
    assert table["schema_version"] == 1
    assert "EUR_USD" in table["pairs"]
    pair_entry = table["pairs"]["EUR_USD"]

    assert pair_entry["sessions"]["Asian"]["spread_pips"] == pytest.approx(0.5, abs=0.01)
    assert pair_entry["sessions"]["London"]["spread_pips"] == pytest.approx(0.1, abs=0.01)
    assert pair_entry["sessions"]["Lon-NY"]["spread_pips"] == pytest.approx(0.1, abs=0.01)
    assert pair_entry["sessions"]["NY"]["spread_pips"] == pytest.approx(0.1, abs=0.01)
    assert pair_entry["sessions"]["Rollover"]["spread_pips"] == pytest.approx(0.5, abs=0.01)

    assert pair_entry["commission_per_side_pips"] == pytest.approx(0.35, abs=0.01)
    assert pair_entry["slippage_per_side_pips"] == 0.5
    assert pair_entry["slippage_source"] == "default"


def test_commission_lookup_jpy_pair():
    assert ct.commission_per_side_pips("EUR_USD") == pytest.approx(0.35, abs=0.01)
    assert ct.commission_per_side_pips("USD_JPY") == pytest.approx(0.35, abs=0.05)
    assert ct.commission_per_side_pips("UNKNOWN_PAIR") == pytest.approx(0.35, abs=0.05)


def test_missing_parquet_pair_skipped(tmp_path):
    mt5_root = tmp_path / "BackTestData_MT5"
    mt5_root.mkdir()
    out_path = tmp_path / "cost_table.json"
    ct.build_cost_table(
        pairs=["DOES_NOT_EXIST"],
        mt5_root=mt5_root,
        out_path=out_path,
    )
    table = json.loads(out_path.read_text())
    assert table["pairs"] == {}


def _write_tz_aware_parquet(path: Path, *, broker_tz: str = "Etc/GMT-2") -> None:
    """Synthetic EUR_USD parquet with broker-local tz-aware timestamps.

    All rows are at broker-local 22:30, which corresponds to 20:30 UTC
    when ``broker_tz`` is +02:00 — so a UTC-correct grouping bins them as
    NY (17–21 UTC), not Rollover (21–24 UTC).
    """
    pip = 0.0001
    rows = []
    base_local = pd.Timestamp("2026-03-04 22:30:00", tz=broker_tz)
    for d in range(40):
        ts = base_local + pd.Timedelta(days=d)
        if ts.dayofweek >= 5:
            continue
        rows.append(
            {
                "timestamp": ts,
                "open": 1.10000,
                "high": 1.10010,
                "low": 1.09990,
                "close": 1.10005,
                "volume": 1.0,
                "spread": 0.2 * pip,
            }
        )
    pd.DataFrame(rows).to_parquet(path, index=False)


def test_tz_aware_non_utc_timestamps_grouped_by_utc_hour(tmp_path):
    """Broker-local +02:00 timestamps must be converted to UTC before session
    classification — otherwise rows end up bucketed by broker hour."""
    mt5_root = tmp_path / "BackTestData_MT5"
    mt5_root.mkdir()
    _write_tz_aware_parquet(mt5_root / "EUR_USD_M1.parquet")

    out_path = tmp_path / "cost_table.json"
    ct.build_cost_table(pairs=["EUR_USD"], mt5_root=mt5_root, out_path=out_path)

    sessions = json.loads(out_path.read_text())["pairs"]["EUR_USD"]["sessions"]
    # 22:30 +02:00 = 20:30 UTC → NY session, NOT Rollover.
    assert "NY" in sessions
    assert "Rollover" not in sessions


def test_absurd_spread_raises_on_pre_412_pips_units(tmp_path):
    """If the upstream parquet's `spread` column is already in pips (a
    pre-412edf9 regression), the per-pip division turns it into thousands
    of pips. The builder should raise rather than ship a poisoned table."""
    mt5_root = tmp_path / "BackTestData_MT5"
    mt5_root.mkdir()
    rows = []
    start = pd.Timestamp("2026-03-01 00:00:00")
    for d in range(20):
        ts = start + pd.Timedelta(days=d)
        if ts.dayofweek >= 5:
            continue
        for minute in range(24 * 60):
            rows.append(
                {
                    "timestamp": ts + pd.Timedelta(minutes=minute),
                    "open": 1.10000,
                    "high": 1.10010,
                    "low": 1.09990,
                    "close": 1.10005,
                    "volume": 1.0,
                    "spread": 0.5,  # WRONG — already in pips, not price units
                }
            )
    pd.DataFrame(rows).to_parquet(mt5_root / "EUR_USD_M1.parquet", index=False)

    out_path = tmp_path / "cost_table.json"
    with pytest.raises(ValueError, match="implausible per-session spreads"):
        ct.build_cost_table(pairs=["EUR_USD"], mt5_root=mt5_root, out_path=out_path)
