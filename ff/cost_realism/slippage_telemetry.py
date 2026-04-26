"""Per-pair slippage telemetry — read forensic data, write back to cost_table.json.

Maintains a rolling-window per-pair median entry slippage. Pairs with fewer
than ``min_trades`` recent fills keep the default 0.5 pips. Source label
(``default`` vs ``telemetry_n=N``) is stamped so the UI can show data
maturity.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

LOG = logging.getLogger(__name__)


def update_from_forensic(
    forensic_df: pd.DataFrame,
    cost_table_path: Path,
    min_trades: int = 20,
    rolling_window: int = 20,
) -> None:
    """Update per-pair ``slippage_per_side_pips`` in ``cost_table.json`` from
    a ``forensic_df`` containing ``pair`` and ``entry_slippage_pips``.

    If a ``fired_at_utc`` column exists, sort newest-last so that the rolling
    window reflects the most recent fills. Pairs not present in the cost
    table are ignored.
    """
    if not cost_table_path.exists():
        LOG.warning("[telemetry] %s does not exist — nothing to update", cost_table_path)
        return

    table = json.loads(cost_table_path.read_text())
    pairs_block = table.get("pairs", {})
    if not pairs_block:
        return

    df = forensic_df.copy()
    if "fired_at_utc" in df.columns:
        df = df.sort_values("fired_at_utc")

    for pair in list(pairs_block.keys()):
        pair_rows = df[df["pair"] == pair]
        # Count VALID numeric telemetry points, not raw rows — otherwise a
        # batch full of NaN slippages can pass min_trades and then write a
        # NaN slippage to the cost table (invalid JSON).
        slip = pd.to_numeric(pair_rows["entry_slippage_pips"], errors="coerce")
        slip = slip.replace([np.inf, -np.inf], np.nan).dropna()
        n_valid = int(slip.size)
        if n_valid < min_trades:
            continue
        recent = slip.tail(rolling_window)
        median = recent.median()
        if not np.isfinite(median):
            # Defensive: with the dropna above this should be unreachable, but
            # if a future change reintroduces NaN we refuse to write it.
            continue
        new_slip = float(round(median, 4))
        pairs_block[pair]["slippage_per_side_pips"] = new_slip
        pairs_block[pair]["slippage_source"] = f"telemetry_n={n_valid}"

    cost_table_path.write_text(json.dumps(table, indent=2))
    LOG.info("[telemetry] updated %s", cost_table_path)
