"""Shared trade-eligibility rules — the "3-and-3" module.

One source of truth imported by:
- ``ff.cost_realism.bt_gate`` (BT post-pass)
- ``ff.live.execution_guard`` (live runner pre-submission)

So BT and live can never drift on what counts as a "do not fire" condition.
"""

from __future__ import annotations

import pandas as pd

# Hard caps applied to every EA. Not pair-specific.
SPREAD_CAP_PIPS: float = 3.0
SLIPPAGE_CAP_PIPS: float = 3.0

# Rollover window (UTC). London/NY close handover; spreads spike here even on
# raw-spread accounts, and most strategies should not initiate new positions.
ROLLOVER_START_HOUR_UTC: int = 21
ROLLOVER_END_HOUR_UTC: int = 24

# Session boundaries (UTC, fixed — no DST shift).
_SESSION_BOUNDARIES = [
    (0, 8, "Asian"),
    (8, 13, "London"),
    (13, 17, "Lon-NY"),
    (17, 21, "NY"),
    (21, 24, "Rollover"),
]


def session_of_hour(hour: int) -> str:
    """Return the canonical session name for a UTC hour 0..23."""
    for lo, hi, name in _SESSION_BOUNDARIES:
        if lo <= hour < hi:
            return name
    raise ValueError(f"hour out of range: {hour}")


def _to_utc(ts: pd.Timestamp) -> pd.Timestamp:
    """Coerce a Timestamp to UTC. Naive timestamps are treated as UTC."""
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def is_rollover(ts: pd.Timestamp) -> bool:
    """True when the entry timestamp falls in the daily rollover window."""
    h = _to_utc(ts).hour
    return ROLLOVER_START_HOUR_UTC <= h < ROLLOVER_END_HOUR_UTC


def is_spread_too_wide(spread_pips: float) -> bool:
    """True when spread strictly exceeds the 3-pip cap."""
    return spread_pips > SPREAD_CAP_PIPS


def is_slippage_too_wide(slippage_pips: float) -> bool:
    """True when realised slippage strictly exceeds the 3-pip cap."""
    return slippage_pips > SLIPPAGE_CAP_PIPS


def should_block(
    ts: pd.Timestamp,
    spread_pips: float,
    slippage_pips: float,
) -> str | None:
    """Return the block reason or None.

    Order of evaluation matters for diagnostics — rollover first because
    the reason is most informative for the user.
    """
    if is_rollover(ts):
        return "rollover"
    if is_spread_too_wide(spread_pips):
        return "spread_3p"
    if is_slippage_too_wide(slippage_pips):
        return "slippage_3p"
    return None
