import pandas as pd

from ff.cost_realism import bt_gate


def _trade_row(*, ts, pair="EUR_USD", spread=0.3, slippage=0.5, pnl=5.0):
    return {
        "pair": pair,
        "entry_ts": pd.Timestamp(ts, tz="UTC"),
        "duka_bt_spread_pips": spread,
        "telemetry_slippage_pips": slippage,
        "raw_pnl_pips": pnl,
    }


def test_gate_drops_rollover_trade():
    df = pd.DataFrame(
        [
            _trade_row(ts="2026-04-24 22:00:00"),
            _trade_row(ts="2026-04-24 10:00:00"),
        ]
    )
    out = bt_gate.apply(df)
    assert out["gated_out_reason"].tolist() == ["rollover", None]


def test_gate_drops_spread_spike():
    df = pd.DataFrame(
        [
            _trade_row(ts="2026-04-24 10:00:00", spread=4.0),
            _trade_row(ts="2026-04-24 10:00:00", spread=2.5),
        ]
    )
    out = bt_gate.apply(df)
    assert out["gated_out_reason"].tolist() == ["spread_3p", None]


def test_gate_drops_slippage_spike():
    df = pd.DataFrame(
        [
            _trade_row(ts="2026-04-24 10:00:00", slippage=5.0),
        ]
    )
    out = bt_gate.apply(df)
    assert out["gated_out_reason"].tolist() == ["slippage_3p"]


def test_gated_pnl_zeroed_in_metric_view():
    df = pd.DataFrame(
        [
            _trade_row(ts="2026-04-24 22:00:00", pnl=10.0),
            _trade_row(ts="2026-04-24 10:00:00", pnl=10.0),
        ]
    )
    out = bt_gate.apply(df)
    assert out["effective_pnl_pips"].tolist() == [0.0, 10.0]
