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


def test_build_cost_table_per_session_means(tmp_path):
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
    # Default behaviour now refuses to overwrite the file when no pairs were
    # built — preserves prior valid coverage. Set ``allow_empty=True`` to opt
    # into the empty-stub for this isolation test.
    n_built = ct.build_cost_table(
        pairs=["DOES_NOT_EXIST"],
        mt5_root=mt5_root,
        out_path=out_path,
        allow_empty=True,
    )
    assert n_built == 0
    table = json.loads(out_path.read_text())
    assert table["pairs"] == {}


def test_zero_pairs_built_does_not_clobber_existing_table(tmp_path):
    """If MT5 is offline / parquets missing, build_cost_table must NOT
    overwrite a previously valid cost_table.json with an empty stub.
    Codex round 3 finding."""
    mt5_root = tmp_path / "BackTestData_MT5"
    mt5_root.mkdir()
    out_path = tmp_path / "cost_table.json"
    # Plant a "previous valid" cost table on disk.
    out_path.write_text('{"schema_version": 1, "pairs": {"EUR_USD": {"sentinel": true}}}')

    n_built = ct.build_cost_table(
        pairs=["DOES_NOT_EXIST"],
        mt5_root=mt5_root,
        out_path=out_path,
        # Default: allow_empty=False — refuses to overwrite.
    )

    assert n_built == 0
    # Existing file must be untouched.
    table = json.loads(out_path.read_text())
    assert table["pairs"]["EUR_USD"]["sentinel"] is True


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


def _write_units_error_parquet(path: Path) -> None:
    """Pre-412edf9 regression shape: spread already in pips, not price units."""
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
                    "spread": 0.5,  # WRONG — already in pips
                }
            )
    pd.DataFrame(rows).to_parquet(path, index=False)


def test_absurd_spread_validator_raises_on_pre_412_pips_units(tmp_path):
    """Validator function itself must raise on unit-error data so callers
    can detect and react. (The orchestrator catches and skips per-pair —
    see `test_absurd_spread_pair_skipped_in_build`.)"""
    mt5_root = tmp_path / "BackTestData_MT5"
    mt5_root.mkdir()
    _write_units_error_parquet(mt5_root / "EUR_USD_M1.parquet")
    df = pd.read_parquet(mt5_root / "EUR_USD_M1.parquet")
    with pytest.raises(ValueError, match="implausible per-session spreads"):
        ct._per_session_mean_spread_pips(df, "EUR_USD")


def test_absurd_spread_pair_skipped_in_build(tmp_path):
    """One pair with unit-error data should be skipped with a warning,
    not abort the whole table build. Other valid pairs must still be
    written."""
    mt5_root = tmp_path / "BackTestData_MT5"
    mt5_root.mkdir()
    # GOOD pair
    _write_fixture_parquet(mt5_root / "EUR_USD_M1.parquet", "EUR_USD")
    # BAD pair (unit error)
    _write_units_error_parquet(mt5_root / "GBP_USD_M1.parquet")

    out_path = tmp_path / "cost_table.json"
    n_built = ct.build_cost_table(pairs=["EUR_USD", "GBP_USD"], mt5_root=mt5_root, out_path=out_path)
    assert n_built == 1
    table = json.loads(out_path.read_text())
    assert "EUR_USD" in table["pairs"]
    assert "GBP_USD" not in table["pairs"]


def _write_floor_biased_parquet(
    path: Path,
    pair: str,
    *,
    floor_spread_pips: float,
    spike_spread_pips: float,
    spike_every_n: int = 20,
    n_minutes: int = 4000,
) -> None:
    """Synthesise a floor-biased M1 parquet (real MT5 distribution shape).

    Most bars sit at ``floor_spread_pips`` (broker quote-rounding floor);
    every ``spike_every_n``-th bar reports ``spike_spread_pips``. Median is
    the floor; mean is a weighted average of floor and spike.
    """
    pip = 0.01 if "JPY" in pair else 0.0001
    rows = []
    base = pd.Timestamp("2026-03-02 10:00:00", tz="UTC")  # Monday, London
    for i in range(n_minutes):
        s_pips = spike_spread_pips if (i % spike_every_n == 0) else floor_spread_pips
        rows.append(
            {
                "timestamp": base + pd.Timedelta(minutes=i),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
                "spread": s_pips * pip,
            }
        )
    pd.DataFrame(rows).to_parquet(path, index=False)


def test_floor_biased_distribution_uses_mean_not_median(tmp_path):
    """Real MT5 M1 bars are heavily biased toward the broker's 1-point
    quote-rounding floor — over 50% of survivors of a typical pair sit at
    the smallest representable spread (e.g. 0.1 pip on AUD_NZD). A median
    statistic returns that floor and silently understates real execution
    cost. Mean-per-session weights the long tail correctly.

    Regression for the silent 0.1-pip cost-table bug shipped 2026-04-26
    (artifacts/cost_table.json had every cross/exotic at 0.1 pips, making
    the overlay refund pips on every survivor of every run).
    """
    mt5_root = tmp_path / "BackTestData_MT5"
    mt5_root.mkdir()
    # AUD_NZD-shaped distribution: 95% at 0.5 pips (still above the 0.3 pip
    # cross-pair floor), 5% at 12 pips (typical wider-quote spike).
    # Median = 0.5 (floor), Mean = 0.95*0.5 + 0.05*12 = 1.075.
    _write_floor_biased_parquet(
        mt5_root / "AUD_NZD_M1.parquet",
        "AUD_NZD",
        floor_spread_pips=0.5,
        spike_spread_pips=12.0,
    )
    out_path = tmp_path / "cost_table.json"
    ct.build_cost_table(pairs=["AUD_NZD"], mt5_root=mt5_root, out_path=out_path)

    london = json.loads(out_path.read_text())["pairs"]["AUD_NZD"]["sessions"]["London"]["spread_pips"]
    assert london == pytest.approx(1.075, abs=0.05), (
        f"expected mean ~1.075 (weighted avg of floor 0.5 + 5% spike 12), got {london} — likely reverted to median (which would be 0.5)"
    )


def test_implausibly_tight_spread_validator_raises_on_cross_pair(tmp_path):
    """Validator must raise on cross/exotic pair spreads below 0.3 pips —
    real broker quotes do not undercut that floor.

    Regression for the 2026-04-26 cost-table bug shipped with every
    cross/exotic pair at ~0.1 pips per session.
    """
    mt5_root = tmp_path / "BackTestData_MT5"
    mt5_root.mkdir()
    _write_floor_biased_parquet(
        mt5_root / "AUD_NZD_M1.parquet",
        "AUD_NZD",
        floor_spread_pips=0.1,
        spike_spread_pips=0.1,
    )
    df = pd.read_parquet(mt5_root / "AUD_NZD_M1.parquet")
    with pytest.raises(ValueError, match="implausibly tight per-session spreads"):
        ct._per_session_mean_spread_pips(df, "AUD_NZD")


def test_implausibly_tight_spread_pair_skipped_in_build(tmp_path):
    """Floor-biased data on one cross pair must not abort the build — the
    pair is skipped with a warning so other valid pairs can still ship."""
    mt5_root = tmp_path / "BackTestData_MT5"
    mt5_root.mkdir()
    # GOOD pair
    _write_fixture_parquet(mt5_root / "EUR_USD_M1.parquet", "EUR_USD")
    # BAD pair (floor-biased)
    _write_floor_biased_parquet(
        mt5_root / "AUD_NZD_M1.parquet",
        "AUD_NZD",
        floor_spread_pips=0.1,
        spike_spread_pips=0.1,
    )
    out_path = tmp_path / "cost_table.json"
    n_built = ct.build_cost_table(pairs=["EUR_USD", "AUD_NZD"], mt5_root=mt5_root, out_path=out_path)
    assert n_built == 1
    table = json.loads(out_path.read_text())
    assert "EUR_USD" in table["pairs"]
    assert "AUD_NZD" not in table["pairs"]


def test_usd_major_can_quote_below_cross_floor(tmp_path):
    """EUR_USD raw spreads legitimately run at ~0.1 pips during liquid
    hours; the lower-bound check must scope by pair so majors are not
    falsely flagged.
    """
    mt5_root = tmp_path / "BackTestData_MT5"
    mt5_root.mkdir()
    # EUR_USD at 0.1 pip floor + occasional 1-pip spike → mean ~0.145.
    # That's below the cross-pair 0.3 floor but above the major 0.05 floor.
    _write_floor_biased_parquet(
        mt5_root / "EUR_USD_M1.parquet",
        "EUR_USD",
        floor_spread_pips=0.1,
        spike_spread_pips=1.0,
    )
    out_path = tmp_path / "cost_table.json"
    # Should NOT raise.
    ct.build_cost_table(pairs=["EUR_USD"], mt5_root=mt5_root, out_path=out_path)
    london = json.loads(out_path.read_text())["pairs"]["EUR_USD"]["sessions"]["London"]["spread_pips"]
    assert 0.05 < london < 0.5
