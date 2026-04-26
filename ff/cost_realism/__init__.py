"""Cost-realism overlay subsystem.

Public API:
    gate_rules.should_block(ts, spread_pips, slippage_pips) -> str | None
    gate_rules.session_of_hour(hour) -> str
"""

from . import gate_rules

__all__ = ["gate_rules"]
