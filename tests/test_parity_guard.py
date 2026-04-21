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
            "trailing": {"when_on": {"test": True}},
            "breakeven": {"when_on": {"test": True}},
            "chandelier": {"when_on": {"test": True}},
            "partial": {"when_on": {"test": True}},
        }
    }
    assert un_portable_knobs(trial) == []


def test_stale_flagged():
    trial = {"engine": {"stale": {"when_on": {"test": True}}}}
    assert un_portable_knobs(trial) == ["stale"]


def test_session_and_max_bars_flagged_in_canonical_order():
    trial = {
        "engine": {
            "max_bars": {"when_on": {"test": True}},
            "session": {"when_on": {"test": True}},
        }
    }
    assert un_portable_knobs(trial) == ["session", "max_bars"]


def test_inactive_group_not_flagged():
    trial = {"engine": {"stale": {"when_on": {"test": False}}}}
    assert un_portable_knobs(trial) == []


def test_assert_portable_raises_with_group_names():
    trial = {"engine": {"stale": {"when_on": {"test": True}}}}
    with pytest.raises(ValueError, match="stale"):
        assert_portable(trial)


def test_canonical_groups_defined():
    """Belt-and-braces — keep the three groups co-located."""
    assert set(UN_PORTABLE_GROUPS) == {"stale", "session", "max_bars"}
