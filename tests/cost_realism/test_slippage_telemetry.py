import json

import pandas as pd
import pytest

from ff.cost_realism import slippage_telemetry as st


def _seed_table(path):
    table = {
        "schema_version": 1,
        "pairs": {
            "EUR_USD": {
                "sessions": {"Asian": {"spread_pips": 0.0}},
                "commission_per_side_pips": 0.35,
                "slippage_per_side_pips": 0.5,
                "slippage_source": "default",
            },
            "GBP_USD": {
                "sessions": {"Asian": {"spread_pips": 0.0}},
                "commission_per_side_pips": 0.35,
                "slippage_per_side_pips": 0.5,
                "slippage_source": "default",
            },
        },
    }
    path.write_text(json.dumps(table))


def test_telemetry_skips_pairs_below_min_trades(tmp_path):
    table_path = tmp_path / "cost_table.json"
    _seed_table(table_path)
    forensic = pd.DataFrame(
        {
            "pair": ["EUR_USD"] * 5,
            "entry_slippage_pips": [0.4] * 5,
        }
    )
    st.update_from_forensic(forensic, table_path, min_trades=20)
    table = json.loads(table_path.read_text())
    assert table["pairs"]["EUR_USD"]["slippage_per_side_pips"] == 0.5
    assert table["pairs"]["EUR_USD"]["slippage_source"] == "default"


def test_telemetry_updates_when_enough_trades(tmp_path):
    table_path = tmp_path / "cost_table.json"
    _seed_table(table_path)
    forensic = pd.DataFrame(
        {
            "pair": ["EUR_USD"] * 25,
            "entry_slippage_pips": [0.7] * 25,
        }
    )
    st.update_from_forensic(forensic, table_path, min_trades=20)
    table = json.loads(table_path.read_text())
    assert table["pairs"]["EUR_USD"]["slippage_per_side_pips"] == pytest.approx(0.7, abs=0.01)
    assert table["pairs"]["EUR_USD"]["slippage_source"] == "telemetry_n=25"
    # Other pairs unchanged
    assert table["pairs"]["GBP_USD"]["slippage_per_side_pips"] == 0.5


def test_telemetry_uses_rolling_window(tmp_path):
    table_path = tmp_path / "cost_table.json"
    _seed_table(table_path)
    forensic = pd.DataFrame(
        {
            "pair": ["EUR_USD"] * 30,
            "entry_slippage_pips": [10.0] * 10 + [0.5] * 20,  # newest 20 are 0.5
            "fired_at_utc": pd.date_range("2026-01-01", periods=30, freq="h"),
        }
    )
    st.update_from_forensic(forensic, table_path, min_trades=20, rolling_window=20)
    table = json.loads(table_path.read_text())
    # Median of newest 20 = 0.5 (excludes the older 10.0 outliers)
    assert table["pairs"]["EUR_USD"]["slippage_per_side_pips"] == pytest.approx(0.5, abs=0.01)
