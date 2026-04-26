"""Post-pass cost-realism overlay.

Computes the delta between what the BT engine charged a trade (Dukascopy
spread + 0.3 pip commission) and what live IC Markets execution would
charge (MT5 session-median spread + per-pair commission + telemetry-fed
slippage), then folds that delta into a third "adjusted P&L" column.

The trade list is unchanged — SL/TP triggers are price-driven and
independent of cost assumptions, so this can be safely post-pass without
producing different trade decisions than an inline implementation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from .gate_rules import session_of_hour

LOG = logging.getLogger(__name__)


def _load_table(path: Path) -> dict:
    if not path.exists():
        LOG.warning("[overlay] cost_table.json missing at %s — overlay returns zero delta", path)
        return {"pairs": {}}
    return json.loads(path.read_text())


def apply(
    trades: pd.DataFrame,
    cost_table_path: Path | str,
    bt_commission_per_side_pips: float = 0.3,
) -> pd.DataFrame:
    """Return ``trades`` with three new columns: ``raw_pnl_pips``,
    ``overlay_delta_pips``, ``adjusted_pnl_pips``.

    ``trades`` must contain ``pair``, ``entry_ts``, ``duka_bt_spread_pips``,
    and ``raw_pnl_pips``. Unknown pairs receive zero delta and pass through
    unchanged with a logged warning.
    """
    table = _load_table(Path(cost_table_path))
    pairs_block = table.get("pairs", {})
    out = trades.copy()

    deltas = []
    for _, row in out.iterrows():
        pair = row["pair"]
        if pair not in pairs_block:
            LOG.warning("[overlay] no entry for %s — passing through unchanged", pair)
            deltas.append(0.0)
            continue
        entry = pairs_block[pair]
        ts = row["entry_ts"]
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        sess = session_of_hour(ts.hour)
        sess_spread = entry["sessions"].get(sess, {}).get("spread_pips")
        if sess_spread is None:
            LOG.warning(
                "[overlay] %s missing %s session — falling back to all-session median",
                pair,
                sess,
            )
            sess_vals = [s["spread_pips"] for s in entry["sessions"].values()]
            sess_spread = sum(sess_vals) / len(sess_vals) if sess_vals else 0.0

        real_comm = entry["commission_per_side_pips"]
        real_slip = entry["slippage_per_side_pips"]

        bt_cost_rt = float(row["duka_bt_spread_pips"]) + 2 * bt_commission_per_side_pips
        real_cost_rt = sess_spread + 2 * real_comm + 2 * real_slip
        deltas.append(bt_cost_rt - real_cost_rt)

    out["overlay_delta_pips"] = deltas
    out["adjusted_pnl_pips"] = out["raw_pnl_pips"] + out["overlay_delta_pips"]
    return out
