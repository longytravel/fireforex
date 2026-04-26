import json
import logging

import pandas as pd
import pytest

from ff.cost_realism import overlay


@pytest.fixture
def cost_table(tmp_path):
    table = {
        "schema_version": 1,
        "pairs": {
            "EUR_USD": {
                "sessions": {
                    "Asian": {"spread_pips": 0.05},
                    "London": {"spread_pips": 0.0},
                    "Lon-NY": {"spread_pips": 0.0},
                    "NY": {"spread_pips": 0.0},
                    "Rollover": {"spread_pips": 0.5},
                },
                "commission_per_side_pips": 0.35,
                "slippage_per_side_pips": 0.5,
                "slippage_source": "default",
            }
        },
    }
    p = tmp_path / "cost_table.json"
    p.write_text(json.dumps(table))
    return p


def test_overlay_adds_three_columns(cost_table):
    trades = pd.DataFrame(
        {
            "pair": ["EUR_USD"],
            "entry_ts": [pd.Timestamp("2026-04-24 10:00:00", tz="UTC")],
            "duka_bt_spread_pips": [0.32],
            "raw_pnl_pips": [10.0],
        }
    )
    out = overlay.apply(trades, cost_table_path=cost_table, bt_commission_per_side_pips=0.3)
    # BT cost RT = 0.32 + 2*0.3 = 0.92 pips; real cost RT = 0 (London) + 2*0.35 + 2*0.5 = 1.7 pips
    # delta = bt_cost - real_cost = 0.92 - 1.7 = -0.78 (real costs more, so adjusted P&L is lower)
    assert out["overlay_delta_pips"].iloc[0] == pytest.approx(-0.78, abs=0.001)
    assert out["adjusted_pnl_pips"].iloc[0] == pytest.approx(10.0 - 0.78, abs=0.001)
    assert "raw_pnl_pips" in out.columns


def test_overlay_uses_session_specific_spread(cost_table):
    trades = pd.DataFrame(
        {
            "pair": ["EUR_USD", "EUR_USD"],
            "entry_ts": [
                pd.Timestamp("2026-04-24 04:00:00", tz="UTC"),  # Asian
                pd.Timestamp("2026-04-24 10:00:00", tz="UTC"),  # London
            ],
            "duka_bt_spread_pips": [0.32, 0.32],
            "raw_pnl_pips": [10.0, 10.0],
        }
    )
    out = overlay.apply(trades, cost_table_path=cost_table, bt_commission_per_side_pips=0.3)
    # Asian: real_cost = 0.05 + 0.7 + 1.0 = 1.75; delta = 0.92 - 1.75 = -0.83
    # London: real_cost = 0.0 + 0.7 + 1.0 = 1.70; delta = 0.92 - 1.70 = -0.78
    assert out["overlay_delta_pips"].iloc[0] == pytest.approx(-0.83, abs=0.001)
    assert out["overlay_delta_pips"].iloc[1] == pytest.approx(-0.78, abs=0.001)


def test_overlay_unknown_pair_passes_through(cost_table, caplog):
    trades = pd.DataFrame(
        {
            "pair": ["XAU_USD"],
            "entry_ts": [pd.Timestamp("2026-04-24 10:00:00", tz="UTC")],
            "duka_bt_spread_pips": [1.0],
            "raw_pnl_pips": [5.0],
        }
    )
    caplog.set_level(logging.WARNING)
    out = overlay.apply(trades, cost_table_path=cost_table, bt_commission_per_side_pips=0.3)
    assert out["overlay_delta_pips"].iloc[0] == 0.0
    assert out["adjusted_pnl_pips"].iloc[0] == 5.0
    assert any("XAU_USD" in r.getMessage() for r in caplog.records)
