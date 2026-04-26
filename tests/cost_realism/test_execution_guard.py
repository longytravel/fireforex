import pandas as pd

from ff.live.execution_guard import evaluate


def test_evaluate_blocks_rollover():
    decision = evaluate(
        ts=pd.Timestamp("2026-04-24 22:00:00", tz="UTC"),
        live_spread_pips=0.5,
    )
    assert decision.block is True
    assert decision.reason == "rollover"


def test_evaluate_blocks_wide_spread():
    decision = evaluate(
        ts=pd.Timestamp("2026-04-24 10:00:00", tz="UTC"),
        live_spread_pips=4.0,
    )
    assert decision.block is True
    assert decision.reason == "spread_3p"


def test_evaluate_passes_quiet_minute():
    decision = evaluate(
        ts=pd.Timestamp("2026-04-24 10:00:00", tz="UTC"),
        live_spread_pips=0.5,
    )
    assert decision.block is False
    assert decision.reason is None


def test_evaluate_blocks_unknown_spread():
    """NaN spread fails closed — same fail-closed contract as bt_gate."""
    decision = evaluate(
        ts=pd.Timestamp("2026-04-24 10:00:00", tz="UTC"),
        live_spread_pips=float("nan"),
    )
    assert decision.block is True
    assert decision.reason == "unknown_spread"
