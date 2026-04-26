"""Post-pass BT trade-gate filter.

Walks a ``trades`` DataFrame and stamps each row with a ``gated_out_reason``
(or None). Gated rows have their ``effective_pnl_pips`` zeroed for metric
roll-up but remain visible in the output for diagnostics.
"""

from __future__ import annotations

import pandas as pd

from .gate_rules import should_block


def _safe_float(value) -> float:
    """Coerce a row value to float, mapping None / pd.NA / unparseable to NaN.

    ``gate_rules.should_block`` treats NaN as *unknown* and fails closed, so
    converting bad inputs to NaN here keeps the diagnostic reason meaningful
    instead of letting ``float(None)`` raise from inside the apply loop.
    """
    if value is None or pd.isna(value):
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def apply(trades: pd.DataFrame) -> pd.DataFrame:
    """Return ``trades`` with two new columns: ``gated_out_reason``,
    ``effective_pnl_pips``.

    Required input columns: ``entry_ts``, ``duka_bt_spread_pips``,
    ``telemetry_slippage_pips``, ``raw_pnl_pips``.
    """
    out = trades.copy()
    reasons: list[str | None] = []
    for _, row in out.iterrows():
        reasons.append(
            should_block(
                row["entry_ts"],
                spread_pips=_safe_float(row["duka_bt_spread_pips"]),
                slippage_pips=_safe_float(row["telemetry_slippage_pips"]),
            )
        )
    # Use pd.Series(dtype=object) so None stays None (not NaN) in tolist().
    out["gated_out_reason"] = pd.Series(reasons, dtype=object, index=out.index)
    out["effective_pnl_pips"] = out.apply(
        lambda r: 0.0 if r["gated_out_reason"] is not None else r["raw_pnl_pips"],
        axis=1,
    )
    return out
