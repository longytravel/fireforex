"""Refuse deploys that use management knobs live cannot honour yet.

``ff.live.exit_manager`` covers trailing, breakeven, chandelier and
partial close. Stale / session / max_bars are time-based knobs that
would need their own porting work and a real-time clock source. Until
that lands, any backtest trial that selects them would silently diverge
live-side — the position would sit on its original SL while the
backtest exits on a stale-range check.

The deploy endpoint calls :func:`un_portable_knobs` and returns
``400`` with the offending groups so the user can repick or tune.
"""
from __future__ import annotations

from typing import Iterable


UN_PORTABLE_GROUPS: tuple[str, ...] = ("stale", "session", "max_bars")


def un_portable_knobs(best_trial: dict | None) -> list[str]:
    """Return groups the trial has active that the live runner cannot
    honour yet. Empty list means the trial is safe to deploy.

    A group is considered active when ``engine.<group>.when_on.test``
    is truthy — mirroring how the sampler encodes on/off for
    :class:`ff.schema.Group` entries.
    """
    if not best_trial:
        return []
    eng = (best_trial.get("engine") or {})
    flagged: list[str] = []
    for name in UN_PORTABLE_GROUPS:
        group = eng.get(name) or {}
        when_on = group.get("when_on") or {}
        if when_on.get("test"):
            flagged.append(name)
    return flagged


def assert_portable(best_trial: dict | None) -> None:
    """Raise :class:`ValueError` with the offending groups if the
    trial is not portable. Preferred call site for CLI paths; the HTTP
    endpoint wraps the result in a 400 directly using the list form.
    """
    bad = un_portable_knobs(best_trial)
    if bad:
        raise ValueError(
            "Trial uses management groups the live runner does not "
            "yet honour: " + ", ".join(bad)
        )
