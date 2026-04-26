"""Build ``artifacts/cost_table.json`` from MT5 tick or M1 parquets,
and expose lookup helpers for the optimiser.

Prefers ``{pair}_TICK.parquet`` (real bid/ask per quote change — no
floor bias). Falls back to ``{pair}_M1.parquet`` when ticks aren't on
disk yet, but flags the resulting entries with ``spread_source: "m1"``
so downstream readers can distinguish realistic data from the legacy
floor-biased path. See issue #39 for why M1 ``spread`` is unreliable.

Per-pair × per-session **mean** spread (pips), plus a static commission
lookup keyed by the quote currency, plus a default 0.5 pips slippage
that the telemetry module overrides as live trades close.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .gate_rules import session_of_hour

_HOUR_TO_SESSION: dict[int, str] = {h: session_of_hour(h) for h in range(24)}

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


# Sanity-check thresholds.
#
# Upper bound: real-world per-session means are sub-pip on majors, a few pips
# on JPY exotics. Beyond 50 pips means the upstream parquet's ``spread``
# column is in pips (pre-commit 412edf9) rather than price units, and the
# per-pip division has multiplied the value by 1/pip. Bail rather than
# silently shipping a poisoned cost table.
_ABSURD_SPREAD_PIPS: float = 50.0
#
# Lower bounds: real IC Markets Raw spreads are bounded below by physical
# broker quotes. Below these floors means the input data is bottoming out
# at a quote-rounding floor — the symptom of the median-on-M1-bar-close
# bug shipped 2026-04-26, where every cross/exotic session pinned to ~0.1
# pips because over half of M1 bars closed on the broker's 1-point quote
# increment.
#
# Empirically calibrated against 90-day IC Markets tick history:
# - USD-majors: liquid-hour means run 0.07-0.18 pips (EUR_USD 0.07,
#   USD_JPY 0.10, GBP_USD 0.18). Floor 0.05 catches the M1-floor bug
#   without false-rejecting realistic ultra-tight quotes.
# - Crosses/exotics: liquid-hour means run 0.18-0.60 pips (EUR_GBP 0.19,
#   AUD_CAD 0.27, AUD_NZD 0.58). Floor 0.15 catches the M1-floor bug
#   (which produced ~0.10-0.14 means on every cross) without rejecting
#   real tight-cross tick data.
_USD_MAJORS: frozenset[str] = frozenset({"EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CHF", "USD_CAD", "NZD_USD"})
_TIGHT_SPREAD_FLOOR_USD_MAJOR_PIPS: float = 0.05
_TIGHT_SPREAD_FLOOR_OTHER_PIPS: float = 0.15


def _tight_spread_floor_pips(pair: str) -> float:
    return _TIGHT_SPREAD_FLOOR_USD_MAJOR_PIPS if pair.upper() in _USD_MAJORS else _TIGHT_SPREAD_FLOOR_OTHER_PIPS


def _per_session_mean_spread_pips(df: pd.DataFrame, pair: str) -> dict[str, float]:
    """Return {session_name: mean_spread_pips} from an MT5 M1 parquet.

    Uses the per-session **mean**, not the median: real MT5 M1 ``spread``
    values are heavily biased toward the broker's 1-point quote-rounding
    floor during liquid periods (over half of EUR_USD bars sit at 0). The
    median therefore reports the floor, not the typical execution cost. The
    mean weights the long tail of wider quotes correctly. Spike sessions
    (Rollover) are bucketed separately so they do not contaminate the
    in-session means.

    Timestamps are coerced to UTC so broker-local tz-aware data (e.g. MT5
    server time at +02:00 / +03:00) is bucketed by UTC hour, not local hour.
    Non-finite spreads are dropped before the per-session mean.
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
    df["session"] = df["hour"].map(_HOUR_TO_SESSION)
    df["spread_pips"] = df["spread"] / pip
    # Drop NaN AND +/-inf — the docstring promised non-finite values are
    # excluded, and a trailing +inf mean would poison the JSON output.
    df = df[np.isfinite(df["spread_pips"]) & (df["spread_pips"] >= 0)]
    if df.empty:
        return {}
    grouped = df.groupby("session")["spread_pips"].mean()
    # Filter NaN out of the dict — json.dumps emits a bare `NaN` token that
    # is not valid JSON and breaks downstream parsers.
    out = {session: float(round(val, 4)) for session, val in grouped.items() if pd.notna(val)}
    # Catch the pre-412 "spread already in pips" regression early.
    extreme = {s: v for s, v in out.items() if v > _ABSURD_SPREAD_PIPS}
    if extreme:
        raise ValueError(
            f"[cost_table] {pair}: implausible per-session spreads {extreme} "
            f"(threshold {_ABSURD_SPREAD_PIPS} pips). "
            f"Likely the upstream parquet's `spread` column is in pips, "
            f"not price units — re-fetch with the post-412 MT5 downloader."
        )
    # Catch the median-on-floor-biased-distribution regression (2026-04-26):
    # if any session's spread is below the per-pair plausible floor, the
    # input data is bottoming out at a quote-rounding floor and the table
    # would silently understate execution cost.
    floor = _tight_spread_floor_pips(pair)
    too_tight = {s: v for s, v in out.items() if v < floor}
    if too_tight:
        raise ValueError(
            f"[cost_table] {pair}: implausibly tight per-session spreads "
            f"{too_tight} (floor {floor} pips for "
            f"{'USD-major' if pair.upper() in _USD_MAJORS else 'cross/exotic'} "
            f"pair). Real broker quotes do not undercut this. Likely the "
            f"input distribution is dominated by the broker's 1-point "
            f"quote-rounding floor; verify the MT5 parquet's `spread` "
            f"column is in price units and contains real quote samples, "
            f"not pre-aggregated floors."
        )
    return out


def _load_pair_spread_frame(pair: str, mt5_root: Path) -> tuple[pd.DataFrame, str] | None:
    """Return ``(df, spread_source)`` for ``pair``, preferring tick data.

    Tick parquet (``{pair}_TICK.parquet``) has bid/ask per tick; spread
    is computed as ``ask - bid``. When ticks are unavailable, falls back
    to M1 OHLC (``{pair}_M1.parquet``) which already carries a ``spread``
    column from the broker bar-close (floor-biased; see issue #39).

    Returns ``None`` if neither parquet exists or both are empty.
    """
    tick_path = mt5_root / f"{pair}_TICK.parquet"
    if tick_path.exists():
        try:
            df = pd.read_parquet(tick_path, columns=["timestamp", "bid", "ask"])
        except Exception as exc:
            LOG.warning("[cost_table] %s tick parquet unreadable (%s) — falling back to M1", pair, exc)
        else:
            if not df.empty:
                df = df.copy()
                df["spread"] = df["ask"].astype("float64") - df["bid"].astype("float64")
                return df[["timestamp", "spread"]], "tick"

    m1_path = mt5_root / f"{pair}_M1.parquet"
    if m1_path.exists():
        try:
            df = pd.read_parquet(m1_path, columns=["timestamp", "spread"])
        except Exception as exc:
            LOG.warning("[cost_table] %s M1 parquet unreadable (%s) — skipping", pair, exc)
            return None
        if not df.empty:
            return df, "m1"

    return None


# Liquid sessions used for the optimiser's per-trade overlay-charge
# proxy. NY is excluded because IC spreads widen sharply during the US
# afternoon (e.g. AUD_NZD: 0.6 pips Asian/London, 3.4 pips NY) and that
# tail would dominate the aggregate. Rollover is excluded because
# trades there are blocked by the 3-and-3 gate anyway.
_LIQUID_SESSIONS_FOR_PROXY: tuple[str, ...] = ("Asian", "London", "Lon-NY")

# Aggregate Dukascopy commission proxy used by the optimiser pick-best
# overlay charge. Per-bar Dukascopy spread varies trial-by-trial and is
# not available at the time of pick_best, so we treat the bt-cost side
# as commission-only (the engine already applies Dukascopy spread
# inside the fills). The signed difference between this constant and
# the IC real-cost-RT captures the trial-invariant part of the overlay.
_BT_COMMISSION_PROXY_RT_PIPS: float = 2 * 0.3


def per_trade_overlay_charge_pips(pair: str, cost_table_path: Path) -> float:
    """Aggregate per-trade overlay charge (RT pips) for ``pair``.

    Returns ``bt_cost_rt − real_cost_rt`` averaged across the liquid
    sessions in ``cost_table.json``. Sign matches the cost-realism
    overlay's ``Cost`` column: positive means Dukascopy was over-charging
    vs IC (so the realistic P&L is *higher* than the BT P&L); negative
    means under-charging (realistic P&L is *lower*).

    Used by ``ff.harness.pick_best`` to rank trials by IC-aligned
    adjusted P&L (``total_pnl + n_trades * charge``) instead of raw
    Dukascopy quality. Returns ``0.0`` — and lets ``pick_best`` fall
    back to its default objective — if the cost table is missing or
    has no entry for the pair.

    The exact per-trade overlay (``ff.cost_realism.overlay``) varies
    by Dukascopy bar spread, which is not observable from
    summary metrics. This aggregate proxy ranks trials nearly
    identically to the true overlay because the per-trial cost
    adjustment is roughly constant within a single-pair sweep.
    """
    if not cost_table_path.exists():
        return 0.0
    try:
        table = json.loads(cost_table_path.read_text())
    except (OSError, ValueError) as exc:
        LOG.warning("[cost_table] cannot read %s for pick_best charge: %s", cost_table_path, exc)
        return 0.0

    pair_block = (table.get("pairs") or {}).get(pair)
    if pair_block is None:
        return 0.0
    sessions = pair_block.get("sessions") or {}
    spreads = [
        sessions[s].get("spread_pips")
        for s in _LIQUID_SESSIONS_FOR_PROXY
        if s in sessions and isinstance(sessions[s].get("spread_pips"), (int, float))
    ]
    if not spreads:
        return 0.0
    real_spread_rt = float(sum(spreads) / len(spreads))
    commission_rt = 2 * float(pair_block.get("commission_per_side_pips", commission_per_side_pips(pair)))
    slippage_rt = 2 * float(pair_block.get("slippage_per_side_pips", DEFAULT_SLIPPAGE_PIPS))
    real_cost_rt = real_spread_rt + commission_rt + slippage_rt
    return _BT_COMMISSION_PROXY_RT_PIPS - real_cost_rt


def build_cost_table(
    pairs: list[str],
    mt5_root: Path,
    out_path: Path,
    allow_empty: bool = False,
) -> int:
    """Write a fresh ``cost_table.json`` covering ``pairs`` from MT5 parquets.

    Pairs without an MT5 parquet are silently skipped with a logged warning.
    Returns the number of pairs that were built.

    If ``allow_empty`` is ``False`` (default) and zero pairs were built, the
    function refuses to overwrite ``out_path`` — a previous valid cost table
    on disk is preserved instead of being clobbered with an empty stub.
    """
    if not pairs:
        if not allow_empty:
            raise ValueError(
                "[cost_table] empty `pairs` list and allow_empty=False; refusing to overwrite existing cost table with an empty one."
            )
    pairs_block: dict[str, dict] = {}
    earliest, latest = None, None

    for pair in pairs:
        loaded = _load_pair_spread_frame(pair, mt5_root)
        if loaded is None:
            LOG.warning("[cost_table] missing tick/M1 parquet for %s — skipping", pair)
            continue
        df, spread_source = loaded
        try:
            sessions = _per_session_mean_spread_pips(df, pair)
        except ValueError as exc:
            # Per-pair validation failure (e.g. unit error or floor-biased
            # data). Log loudly and skip rather than aborting the whole
            # rebuild — other pairs may still be valid.
            LOG.warning("[cost_table] %s — skipping (%s)", pair, exc)
            continue
        if not sessions:
            continue
        pairs_block[pair] = {
            "sessions": {s: {"spread_pips": v} for s, v in sessions.items()},
            "commission_per_side_pips": commission_per_side_pips(pair),
            "slippage_per_side_pips": DEFAULT_SLIPPAGE_PIPS,
            "slippage_source": "default",
            "spread_source": spread_source,  # "tick" (preferred) or "m1" (legacy floor-biased fallback, see issue #39)
        }
        ts_min, ts_max = pd.to_datetime(df["timestamp"]).min(), pd.to_datetime(df["timestamp"]).max()
        earliest = ts_min if earliest is None else min(earliest, ts_min)
        latest = ts_max if latest is None else max(latest, ts_max)

    if not pairs_block and not allow_empty:
        # Never clobber an existing cost table with an empty one — a transient
        # MT5 outage would otherwise wipe live coverage. Caller must opt in
        # via allow_empty=True.
        LOG.warning(
            "[cost_table] zero pairs built; refusing to overwrite %s (pass allow_empty=True to override).",
            out_path,
        )
        return 0
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
    return len(pairs_block)
