import pandas as pd

from ff.cost_realism import gate_rules as gr


def test_session_of_hour_boundaries():
    assert gr.session_of_hour(0) == "Asian"
    assert gr.session_of_hour(7) == "Asian"
    assert gr.session_of_hour(8) == "London"
    assert gr.session_of_hour(12) == "London"
    assert gr.session_of_hour(13) == "Lon-NY"
    assert gr.session_of_hour(16) == "Lon-NY"
    assert gr.session_of_hour(17) == "NY"
    assert gr.session_of_hour(20) == "NY"
    assert gr.session_of_hour(21) == "Rollover"
    assert gr.session_of_hour(23) == "Rollover"


def test_is_rollover_boundaries():
    assert gr.is_rollover(pd.Timestamp("2026-04-24 20:59:59", tz="UTC")) is False
    assert gr.is_rollover(pd.Timestamp("2026-04-24 21:00:00", tz="UTC")) is True
    assert gr.is_rollover(pd.Timestamp("2026-04-24 23:59:59", tz="UTC")) is True
    assert gr.is_rollover(pd.Timestamp("2026-04-25 00:00:00", tz="UTC")) is False


def test_is_spread_too_wide():
    assert gr.is_spread_too_wide(2.99) is False
    assert gr.is_spread_too_wide(3.0) is False
    assert gr.is_spread_too_wide(3.01) is True


def test_is_slippage_too_wide():
    assert gr.is_slippage_too_wide(2.99) is False
    assert gr.is_slippage_too_wide(3.0) is False
    assert gr.is_slippage_too_wide(3.01) is True


def test_should_block_returns_reason_or_none():
    ts_quiet = pd.Timestamp("2026-04-24 10:00:00", tz="UTC")
    ts_roll = pd.Timestamp("2026-04-24 22:00:00", tz="UTC")
    assert gr.should_block(ts_quiet, spread_pips=0.5, slippage_pips=0.5) is None
    assert gr.should_block(ts_roll, spread_pips=0.5, slippage_pips=0.5) == "rollover"
    assert gr.should_block(ts_quiet, spread_pips=4.0, slippage_pips=0.5) == "spread_3p"
    assert gr.should_block(ts_quiet, spread_pips=0.5, slippage_pips=5.0) == "slippage_3p"


def test_naive_timestamp_treated_as_utc():
    ts = pd.Timestamp("2026-04-24 22:00:00")  # no tz
    assert gr.is_rollover(ts) is True


def test_should_block_unknown_inputs_fail_closed():
    """NaN / inf must NOT pass — the gate fails closed on missing readings."""
    ts_quiet = pd.Timestamp("2026-04-24 10:00:00", tz="UTC")
    assert gr.should_block(ts_quiet, spread_pips=float("nan"), slippage_pips=0.5) == "unknown_spread"
    assert gr.should_block(ts_quiet, spread_pips=float("inf"), slippage_pips=0.5) == "unknown_spread"
    assert gr.should_block(ts_quiet, spread_pips=0.5, slippage_pips=float("nan")) == "unknown_slippage"
    assert gr.should_block(ts_quiet, spread_pips=0.5, slippage_pips=float("-inf")) == "unknown_slippage"


def test_is_spread_too_wide_rejects_non_finite():
    assert gr.is_spread_too_wide(float("nan")) is True
    assert gr.is_spread_too_wide(float("inf")) is True


def test_is_slippage_too_wide_rejects_non_finite():
    assert gr.is_slippage_too_wide(float("nan")) is True
    assert gr.is_slippage_too_wide(float("inf")) is True
