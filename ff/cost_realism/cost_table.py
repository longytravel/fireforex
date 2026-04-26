"""Build ``artifacts/cost_table.json`` from MT5 M1 parquets.

Per-pair × per-session median spread (pips), plus a static commission lookup
keyed by the quote currency, plus a default 0.5 pips slippage that the
telemetry module overrides as live trades close.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .gate_rules import session_of_hour

LOG = logging.getLogger(__name__)

DEFAULT_SLIPPAGE_PIPS: float = 0.5

# IC Markets Raw Spread: $7 USD round-turn per standard lot.
# 1 pip on 1 standard lot ≈ $10 for USD-quoted pairs → 0.7 pips RT, 0.35/side.
# Cross / non-USD-quoted pairs translate roughly the same in pip-equivalent
# at typical cross rates; v1 uses the flat 0.35/side. Refine per-pair when
# we have account-statement evidence of a different per-pair commission.
_COMMISSION_PER_SIDE_PIPS_USD_QUOTED: float = 0.35


def commission_per_side_pips(pair: str) -> float:
    """Per-side commission in pips for ``pair``.

    v1 returns the same 0.35 pips/side for every pair. Per-pair overrides
    can be added by extending this lookup once we have evidence.
    """
    return _COMMISSION_PER_SIDE_PIPS_USD_QUOTED


def _pip_value(pair: str) -> float:
    return 0.01 if "JPY" in pair.upper() else 0.0001


# Sanity-check threshold. Real-world per-session medians are sub-pip on majors,
# a few pips at most on JPY exotics. Anything beyond 50 pips means the upstream
# parquet's ``spread`` column is in pips (pre-commit 412edf9) rather than price
# units, and the per-pip division has multiplied the value by 1/pip. Bail
# rather than silently shipping a poisoned cost table.
_ABSURD_SPREAD_PIPS: float = 50.0


def _per_session_median_spread_pips(df: pd.DataFrame, pair: str) -> dict[str, float]:
    """Return {session_name: median_spread_pips} from an MT5 M1 parquet.

    Timestamps are coerced to UTC so broker-local tz-aware data (e.g. MT5
    server time at +02:00 / +03:00) is bucketed by UTC hour, not local hour.
    Non-finite spreads are dropped before the per-session median.
    """
    if df.empty:
        return {}
    pip = _pip_value(pair)
    df = df.copy()
    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("UTC")
    else:
        ts = ts.dt.tz_convert("UTC")
    df["hour"] = ts.dt.hour
    df["session"] = df["hour"].map(session_of_hour)
    df["spread_pips"] = df["spread"] / pip
    df = df[df["spread_pips"].notna() & (df["spread_pips"] >= 0)]
    if df.empty:
        return {}
    grouped = df.groupby("session")["spread_pips"].median()
    out = {session: float(round(val, 4)) for session, val in grouped.items()}
    # Catch the pre-412 "spread already in pips" regression early.
    extreme = {s: v for s, v in out.items() if v > _ABSURD_SPREAD_PIPS}
    if extreme:
        raise ValueError(
            f"[cost_table] {pair}: implausible per-session spreads {extreme} "
            f"(threshold {_ABSURD_SPREAD_PIPS} pips). "
            f"Likely the upstream parquet's `spread` column is in pips, "
            f"not price units — re-fetch with the post-412 MT5 downloader."
        )
    return out


def build_cost_table(
    pairs: list[str],
    mt5_root: Path,
    out_path: Path,
) -> None:
    """Write a fresh ``cost_table.json`` covering ``pairs`` from MT5 parquets.

    Pairs without an MT5 parquet are silently skipped with a logged warning.
    """
    pairs_block: dict[str, dict] = {}
    earliest, latest = None, None

    for pair in pairs:
        path = mt5_root / f"{pair}_M1.parquet"
        if not path.exists():
            LOG.warning("[cost_table] missing %s — skipping", path)
            continue
        df = pd.read_parquet(path)
        if df.empty:
            LOG.warning("[cost_table] empty parquet %s — skipping", path)
            continue
        sessions = _per_session_median_spread_pips(df, pair)
        if not sessions:
            continue
        pairs_block[pair] = {
            "sessions": {s: {"spread_pips": v} for s, v in sessions.items()},
            "commission_per_side_pips": commission_per_side_pips(pair),
            "slippage_per_side_pips": DEFAULT_SLIPPAGE_PIPS,
            "slippage_source": "default",
        }
        ts_min, ts_max = pd.to_datetime(df["timestamp"]).min(), pd.to_datetime(df["timestamp"]).max()
        earliest = ts_min if earliest is None else min(earliest, ts_min)
        latest = ts_max if latest is None else max(latest, ts_max)

    table = {
        "schema_version": 1,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "mt5_history_window": [
            earliest.date().isoformat() if earliest is not None else None,
            latest.date().isoformat() if latest is not None else None,
        ],
        "pairs": pairs_block,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(table, indent=2))
    LOG.info("[cost_table] wrote %d pairs to %s", len(pairs_block), out_path)
