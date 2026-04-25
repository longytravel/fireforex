"""Helpers for replaying one deployed signal fingerprint across pairs."""

from __future__ import annotations

from typing import Any

from ff.schema import Choice


def pin_frozen_signal(ea: dict[str, Any], best_trial: dict[str, Any] | None) -> dict[str, Any]:
    """Force ``ea['signals']`` to include the deployed signal fingerprint.

    A live deploy is calibrated on one reference pair, then often traded
    across many pairs. Pair-specific defaults can produce a signal grid that
    does not naturally contain the reference pair's exact parameters. In live
    and replay mode we are no longer searching the grid; we are replaying one
    frozen fingerprint, so inject that exact combo as single-value Choices.
    """
    if not best_trial:
        return ea
    family = best_trial.get("signal_family")
    params = best_trial.get("signal_params") or {}
    if not family or not isinstance(params, dict):
        return ea

    signals = ea.setdefault("signals", {})
    spec = dict(signals.get(family) or {})
    for key, value in params.items():
        spec[str(key)] = Choice((value,))
    signals[family] = spec
    return ea
