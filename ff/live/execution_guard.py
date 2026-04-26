"""Pre-trade execution guard for the live runner.

Mirrors ``ff.cost_realism.gate_rules`` so BT and live can never disagree
on which minutes are allowed to trade. Imported into ``ff.live.runner``
and called once per plan candidate before submission to the broker.

The guard does NOT enforce the slippage cap — slippage is a fill-time
property; the runner is responsible for inspecting the realised slippage
after ``submit_market_order`` returns and may close out a trade that
slipped beyond ``SLIPPAGE_CAP_PIPS``. See ``ff.cost_realism.gate_rules``
module docstring for the rationale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from ff.cost_realism import gate_rules


@dataclass
class GuardDecision:
    block: bool
    reason: str | None


def evaluate(ts: pd.Timestamp, live_spread_pips: float) -> GuardDecision:
    """Decide whether a candidate plan may proceed.

    Returns ``GuardDecision(block=True, reason="rollover")`` during the
    21:00–24:00 UTC window, ``GuardDecision(block=True, reason="spread_3p")``
    when the live spread exceeds the cap, and
    ``GuardDecision(block=True, reason="unknown_spread")`` if the spread
    reading is non-finite (fail-closed). Quiet minutes return
    ``GuardDecision(block=False, reason=None)``.
    """
    if gate_rules.is_rollover(ts):
        return GuardDecision(block=True, reason="rollover")
    # ``is_spread_too_wide`` returns True on NaN/inf — distinguish those
    # from the explicit cap so dashboards can tell "we said no" from
    # "we couldn't tell".
    if not math.isfinite(live_spread_pips):
        return GuardDecision(block=True, reason="unknown_spread")
    if gate_rules.is_spread_too_wide(live_spread_pips):
        return GuardDecision(block=True, reason="spread_3p")
    return GuardDecision(block=False, reason=None)
