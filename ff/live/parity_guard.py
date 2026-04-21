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

    A group is considered active when ``engine.<group>.test`` is truthy
    — that is the Group's own on/off switch in the sampled trial dict
    (see :class:`ff.schema.Group`: ``test`` lives at the Group level,
    ``when_on`` is the sibling dict holding the enabled knob values).
    The earlier implementation checked ``when_on.test``, which is always
    absent and let broken trials through. See 2026-04-21 parity audit.
    """
    if not best_trial:
        return []
    eng = (best_trial.get("engine") or {})
    flagged: list[str] = []
    for name in UN_PORTABLE_GROUPS:
        group = eng.get(name) or {}
        if group.get("test"):
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
