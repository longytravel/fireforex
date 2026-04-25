"""Tests for ff.defaults.complexity — the 1..10 complexity-level EA builder."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the project root is on sys.path so `import ff` works when pytest is
# invoked from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from eas.complex01 import ENGINE_MAPPING as COMPLEX01_MAPPING
from ff.defaults.complexity import complexity_to_ea
from ff.preflight import preflight_report

_OPTIONAL_KEYS = ("trailing", "breakeven", "partial", "stale", "session", "max_bars")


def _optional_present(ea: dict) -> set[str]:
    return {k for k in _OPTIONAL_KEYS if k in ea["engine_schema"]}


# ── Individual level tests ─────────────────────────────────────────────


def test_level_1() -> None:
    ea = complexity_to_ea(1, "EUR_USD", "H1")

    # Basic structural invariants.
    assert "stop_loss" in ea["engine_schema"]
    assert "take_profit" in ea["engine_schema"]
    assert "days" in ea["engine_schema"]

    # Level 1 has no optional groups.
    assert _optional_present(ea) == set()

    # Name default applied.
    assert ea["name"] == "complexity_L1_EUR_USD_H1"

    # sub_tf defaulted from YAML (M1 for H1).
    assert ea["data"]["sub_tf"] == "M1"

    # Preflight reports without raising.
    report = preflight_report(ea, n_trials=500)
    assert isinstance(report, str) and len(report) > 0


def test_level_5() -> None:
    ea = complexity_to_ea(5, "GBP_USD", "M30")
    present = _optional_present(ea)
    assert "trailing" in present
    assert "breakeven" in present
    assert "partial" not in present
    assert "stale" not in present
    assert "session" not in present

    # Level 5 enables trailing+breakeven+max_bars.
    assert "max_bars" in present


def test_level_10() -> None:
    ea = complexity_to_ea(10, "USD_JPY", "H1")
    present = _optional_present(ea)
    for key in _OPTIONAL_KEYS:
        assert key in present, f"level 10 missing optional group {key!r}"

    # Preflight reports without raising.
    preflight_report(ea, n_trials=500)


# ── Cross-level invariants ─────────────────────────────────────────────


def test_monotonic_complexity() -> None:
    """Active-optional-key sets are non-decreasing across levels 1..10."""
    prev: set[str] = set()
    for lvl in range(1, 11):
        ea = complexity_to_ea(lvl, "EUR_USD", "H1")
        present = _optional_present(ea)
        assert prev.issubset(present), f"level {lvl} active keys {present} is not a superset of level {lvl - 1}'s active keys {prev}"
        prev = present


def test_mapping_matches_complex01_shape() -> None:
    """Every slot used at level 10 must correspond to a slot in complex01."""
    ea = complexity_to_ea(10, "EUR_USD", "H1")
    complex01_slots = {pl for pl, _encoder in COMPLEX01_MAPPING}
    for pl, _encoder in ea["engine_mapping"]:
        assert pl in complex01_slots, f"level-10 mapping uses PL slot {pl} which is not present in eas.complex01.ENGINE_MAPPING"


# ── Sampler structural check ───────────────────────────────────────────


def test_sampler_runs_level_6() -> None:
    from ff.sampler import RandomSampler

    ea = complexity_to_ea(6, "EUR_USD", "H1")
    n_variants = len(ea["signals"])
    sampler = RandomSampler(ea["engine_schema"], n_variants=n_variants, seed=42)
    trials = sampler.sample(5)
    assert isinstance(trials, list)
    assert len(trials) == 5
    for t in trials:
        assert isinstance(t, dict)


# ── Standalone entry (pytest fallback) ─────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
