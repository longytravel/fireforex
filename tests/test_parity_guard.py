"""Unit tests for ``ff.live.parity_guard``."""

from __future__ import annotations

import pytest

from ff.live.parity_guard import (
    UN_PORTABLE_GROUPS,
    assert_portable,
    un_portable_knobs,
)


def test_empty_trial_is_portable():
    assert un_portable_knobs(None) == []
    assert un_portable_knobs({}) == []
    assert un_portable_knobs({"engine": {}}) == []


def test_missing_engine_is_portable():
    assert un_portable_knobs({"other": 1}) == []


def test_portable_management_only():
    trial = {
        "engine": {
            "trailing": {"test": True, "when_on": {}},
            "breakeven": {"test": True, "when_on": {}},
            "chandelier": {"test": True, "when_on": {}},
            "partial": {"test": True, "when_on": {}},
        }
    }
    assert un_portable_knobs(trial) == []


def test_stale_flagged():
    trial = {"engine": {"stale": {"test": True, "when_on": {}}}}
    assert un_portable_knobs(trial) == ["stale"]


def test_session_and_max_bars_flagged_in_canonical_order():
    trial = {
        "engine": {
            "max_bars": {"test": True, "when_on": {}},
            "session": {"test": True, "when_on": {"hours_start": 9, "hours_end": 14}},
        }
    }
    assert un_portable_knobs(trial) == ["session", "max_bars"]


def test_inactive_group_not_flagged():
    trial = {"engine": {"stale": {"test": False}}}
    assert un_portable_knobs(trial) == []


def test_assert_portable_raises_with_group_names():
    trial = {"engine": {"stale": {"test": True}}}
    with pytest.raises(ValueError, match="stale"):
        assert_portable(trial)


def test_real_trial_shape_session_flagged():
    """Regression from the 2026-04-21 deploy audit: the trial uses the
    flat ``test`` key on groups, not ``when_on.test``. The earlier
    guard missed this and let a session-constrained trial deploy live."""
    trial = {
        "engine": {
            "stop_loss": {"selector": "fixed", "fixed": {"pips": 43.0}},
            "take_profit": {"selector": "fixed", "fixed": {"pips": 5.5}},
            "trailing": {"test": False},
            "breakeven": {"test": False},
            "partial": {"test": True, "when_on": {"pct": 66.5, "trigger": 12.75}},
            "chandelier": {"test": True, "when_on": {"activate": 11.0, "atr_mult": 2.4}},
            "stale": {"test": False},
            "session": {"test": True, "when_on": {"hours_start": 9, "hours_end": 14}},
            "max_bars": {"test": False},
        }
    }
    assert un_portable_knobs(trial) == ["session"]


def test_canonical_groups_defined():
    """Belt-and-braces — keep the three groups co-located."""
    assert set(UN_PORTABLE_GROUPS) == {"stale", "session", "max_bars"}
