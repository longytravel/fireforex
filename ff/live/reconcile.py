"""Backtest↔live trade reconciler.

Joins the per-trade log written by :func:`ff.harness.run` (via the extended
Rust engine) against MT5 deal history for the same window. Classifies every
trade as:

  - ``matched`` — entry price, exit price, pnl all within tolerance
  - ``missing_in_live`` — backtest trade with no corresponding MT5 deal
  - ``extra_in_live`` — MT5 deal with no backtest trade
  - ``mismatched_entry_price`` / ``mismatched_exit_price`` / ``mismatched_pnl``
    — joined pair whose price/pnl exceeds tolerance (flags stack)

Matching key: ``(pair, direction, signal_bar_ts)``. Exact-key join first,
then sweep unmatched BT rows against unmatched live rows within
``signal_bar_window`` main-TF bars to absorb clock drift.

This module has no MT5 dependency — it takes pre-fetched deal data as
input so unit tests can mock both sides.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

# ── Tolerances ─────────────────────────────────────────────────────────


@dataclass
class Tolerances:
    entry_price_pips: float = 2.0
    exit_price_pips: float = 2.0
    pnl_pips: float = 1.0
    signal_bar_minutes: int = 60
    signal_bar_window: int = 1  # ± N main-TF bars
    # Parity-v2 tolerances.
    spread_pips: float = 0.5
    slippage_pips: float = 1.0


# Close-reason canonicalisation. Backtest exit codes come from the Rust
# engine; live comes from MT5 DEAL_REASON_*. The only hard-equal matches
# worth flagging are SL and TP. Engine-managed exits (trailing, breakeven,
# chandelier, max_bars, stale) close via our ``close_position`` call, which
# MT5 marks ``EXPERT`` — hiding intent. Canonicalise both sides to
# ``{SL, TP, OTHER}`` so the category only flags genuine divergence.
_CANONICAL_CLOSE_REASON = {
    # Backtest side (ff.exit_codes).
    "SL": "SL",
    "TP": "TP",
    "TRAILING": "OTHER",
    "BREAKEVEN": "OTHER",
    "CHANDELIER": "OTHER",
    "MAX_BARS": "OTHER",
    "STALE": "OTHER",
    "NONE": "OTHER",
    # Live side (MT5 DEAL_REASON_*).
    "EXPERT": "OTHER",
    "CLIENT": "OTHER",
    "MOBILE": "OTHER",
    "WEB": "OTHER",
    "SO": "OTHER",
    "": "OTHER",
    "UNKNOWN": "OTHER",
}


def _canonical_close_reason(name: str | None) -> str:
    return _CANONICAL_CLOSE_REASON.get(str(name or ""), "OTHER")


# ── Report types ───────────────────────────────────────────────────────


@dataclass
class MatchedRow:
    plan_id: str
    pair: str
    direction: int
    signal_bar_ts: str
    bt_entry_price: float
    live_entry_price: float
    entry_delta_pips: float
    bt_exit_price: float
    live_exit_price: float
    exit_delta_pips: float
    bt_pnl_pips: float
    live_pnl_pips: float
    pnl_delta_pips: float
    # Parity-v2 comparison fields.
    bt_signal_variant: int = -1
    live_signal_variant: int = -1
    bt_signal_family: str = ""
    live_signal_family: str = ""
    bt_spread_pips: float = float("nan")
    live_spread_pips: float = float("nan")
    spread_delta_pips: float = float("nan")
    live_slippage_pips: float = float("nan")
    bt_close_reason: str = ""
    live_close_reason: str = ""
    # Cost-realism columns — written by the harness after PR #31/#35.
    # Legacy backtests (pre-#31) carry no values; raw + effective fall back
    # to bt_pnl_pips so the matched-row view never shows a blank cell.
    bt_raw_pnl_pips: float = float("nan")
    bt_overlay_delta_pips: float = float("nan")
    bt_adjusted_pnl_pips: float = float("nan")
    bt_gated_out_reason: str = ""
    bt_effective_pnl_pips: float = float("nan")
    categories: list[str] = field(default_factory=list)


@dataclass
class ReconcileReport:
    matched: list[MatchedRow]
    missing_in_live: list[dict[str, Any]]
    extra_in_live: list[dict[str, Any]]
    tolerances: Tolerances
    generated_at: str

    @property
    def counts(self) -> dict[str, int]:
        cat_counts: dict[str, int] = {}
        for m in self.matched:
            if not m.categories:
                cat_counts.setdefault("matched", 0)
                cat_counts["matched"] += 1
            else:
                for c in m.categories:
                    cat_counts.setdefault(c, 0)
                    cat_counts[c] += 1
        cat_counts["missing_in_live"] = len(self.missing_in_live)
        cat_counts["extra_in_live"] = len(self.extra_in_live)
        return cat_counts

    def by_pair(self) -> dict[str, dict[str, Any]]:
        """Per-pair rollup suitable for the Live tab pair-card grid.

        Each pair gets trade counts (matched / missing / extra), pnl
        aggregates (live minus bt), mean spread/slippage deltas, and
        per-field mismatch counters. Unlike ``counts`` which is global
        cats, this is keyed by pair with nested breakdowns.
        """
        out: dict[str, dict[str, Any]] = {}

        def _slot(pair: str) -> dict[str, Any]:
            if pair not in out:
                out[pair] = {
                    "matched": 0,
                    "missing_in_live": 0,
                    "extra_in_live": 0,
                    "matched_pnl_pips_live": 0.0,
                    "matched_pnl_pips_bt": 0.0,
                    "matched_effective_pnl_pips_bt": 0.0,
                    "delta_pips": 0.0,
                    "n_mismatched_signal": 0,
                    "n_mismatched_spread": 0,
                    "n_mismatched_slippage": 0,
                    "n_mismatched_closure": 0,
                    "n_mismatched_entry_price": 0,
                    "n_mismatched_exit_price": 0,
                    "n_mismatched_pnl": 0,
                    "mean_spread_delta_pips": float("nan"),
                    "mean_slippage_pips": float("nan"),
                }
            return out[pair]

        spread_deltas: dict[str, list[float]] = {}
        slippages: dict[str, list[float]] = {}

        for m in self.matched:
            slot = _slot(m.pair)
            slot["matched"] += 1
            slot["matched_pnl_pips_live"] += float(m.live_pnl_pips) if np.isfinite(m.live_pnl_pips) else 0.0
            slot["matched_pnl_pips_bt"] += float(m.bt_pnl_pips)
            if np.isfinite(m.bt_effective_pnl_pips):
                slot["matched_effective_pnl_pips_bt"] += float(m.bt_effective_pnl_pips)
            for cat in m.categories:
                key = f"n_{cat}"
                if key in slot:
                    slot[key] += 1
            if np.isfinite(m.spread_delta_pips):
                spread_deltas.setdefault(m.pair, []).append(float(m.spread_delta_pips))
            if np.isfinite(m.live_slippage_pips):
                slippages.setdefault(m.pair, []).append(float(m.live_slippage_pips))

        for row in self.missing_in_live:
            _slot(str(row.get("pair", "UNKNOWN")))["missing_in_live"] += 1
        for row in self.extra_in_live:
            _slot(str(row.get("pair", "UNKNOWN")))["extra_in_live"] += 1

        for pair, slot in out.items():
            slot["delta_pips"] = slot["matched_pnl_pips_live"] - slot["matched_pnl_pips_bt"]
            if spread_deltas.get(pair):
                slot["mean_spread_delta_pips"] = float(np.mean(spread_deltas[pair]))
            if slippages.get(pair):
                slot["mean_slippage_pips"] = float(np.mean(slippages[pair]))
        return out


# ── Input shaping ──────────────────────────────────────────────────────


def load_backtest_trades(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True)
    df["exit_ts"] = pd.to_datetime(df["exit_ts"], utc=True)
    return df


def build_live_df(
    plans: Iterable[dict[str, Any]],
    tickets: Iterable[dict[str, Any]],
    deals: Iterable[dict[str, Any]],
) -> pd.DataFrame:
    """Stitch plan log + ticket log + MT5 deal history into one DataFrame.

    One row per live trade with columns that mirror the backtest trade log
    plus the parity-v2 fields: signal_variant, signal_family,
    spread_pips (at fire), commission_ccy, swap_ccy, close_reason,
    slippage_pips (derived from plan.entry_ref_price vs actual fill).
    """
    plans_df = pd.DataFrame(list(plans))
    tickets_df = pd.DataFrame(list(tickets))
    deals_df = pd.DataFrame(list(deals))

    base_cols = [
        "plan_id",
        "pair",
        "direction",
        "signal_bar_ts",
        "entry_price",
        "exit_price",
        "pnl_pips",
        # Parity-v2 columns — always present, NaN/empty if missing upstream.
        "signal_variant",
        "signal_family",
        "spread_pips",
        "commission_ccy",
        "swap_ccy",
        "close_reason",
        "slippage_pips",
    ]
    if plans_df.empty:
        return pd.DataFrame(columns=base_cols)

    plans_df["signal_bar_ts"] = pd.to_datetime(plans_df["signal_bar_ts"], utc=True)

    # Backfill parity cols so downstream select never KeyErrors on legacy
    # plans produced before C₀ landed.
    for col, default in (
        ("signal_variant", -1),
        ("signal_family", ""),
        ("spread_at_fire_pips", np.nan),
    ):
        if col not in plans_df.columns:
            plans_df[col] = default

    # Tickets carry plan_id → MT5 ticket. Deals carry the fill events.
    if not tickets_df.empty:
        tickets_df = tickets_df.rename(columns={"ticket": "ticket_id"})
        merged = plans_df.merge(tickets_df, on="plan_id", how="left")
    else:
        merged = plans_df.copy()
        merged["ticket_id"] = np.nan
        merged["fill_price"] = np.nan

    if deals_df.empty:
        merged["exit_price"] = np.nan
        merged["pnl_pips"] = np.nan
        merged["commission_ccy"] = np.nan
        merged["swap_ccy"] = np.nan
        merged["close_reason"] = ""
    else:
        # Each position has 2 deals: open + close. Group by position_id.
        opens = deals_df[deals_df["position_id"] == deals_df["ticket"]]
        closes = deals_df[deals_df["position_id"] != deals_df["ticket"]]
        open_px = opens.set_index("position_id")["price"].rename("_open_px")
        close_px = closes.set_index("position_id")["price"].rename("_close_px")
        profit = closes.set_index("position_id")["profit"].rename("_close_profit")
        merged = merged.merge(open_px, left_on="ticket_id", right_index=True, how="left")
        merged = merged.merge(close_px, left_on="ticket_id", right_index=True, how="left")
        merged = merged.merge(profit, left_on="ticket_id", right_index=True, how="left")
        # Commission + swap: sum across both legs of the position (MT5 charges
        # each deal separately). Close reason comes from the closing deal.
        if "commission" in deals_df.columns:
            commission = deals_df.groupby("position_id")["commission"].sum().rename("_commission_ccy")
            merged = merged.merge(commission, left_on="ticket_id", right_index=True, how="left")
            merged["commission_ccy"] = merged["_commission_ccy"]
        else:
            merged["commission_ccy"] = np.nan
        if "swap" in deals_df.columns:
            swap = deals_df.groupby("position_id")["swap"].sum().rename("_swap_ccy")
            merged = merged.merge(swap, left_on="ticket_id", right_index=True, how="left")
            merged["swap_ccy"] = merged["_swap_ccy"]
        else:
            merged["swap_ccy"] = np.nan
        if "reason" in deals_df.columns:
            reason = closes.set_index("position_id")["reason"].rename("_close_reason")
            merged = merged.merge(reason, left_on="ticket_id", right_index=True, how="left")
            merged["close_reason"] = merged["_close_reason"].fillna("")
        else:
            merged["close_reason"] = ""

        merged["exit_price"] = merged["_close_px"]
        # Raw pnl in pips: directional diff / pip_value. Fall back to broker profit scaled
        # elsewhere. For majors this is good enough for sanity; JPY pairs need pip scale.
        pip_map = merged["pair"].map(_pair_pip_value).fillna(0.0001)
        merged["pnl_pips"] = np.where(
            merged["direction"] > 0,
            (merged["_close_px"] - merged["_open_px"]) / pip_map,
            (merged["_open_px"] - merged["_close_px"]) / pip_map,
        )

    merged = merged.rename(
        columns={
            "entry_ref_price": "entry_price",
            "spread_at_fire_pips": "spread_pips",
        }
    )

    # Slippage in pips: signed distance between plan.entry_ref_price (what
    # the engine saw) and the broker's fill. Positive = against us.
    pip_map = merged["pair"].map(_pair_pip_value).fillna(0.0001)
    if "fill_price" in merged.columns:
        direction_sign = merged["direction"].astype(float)
        merged["slippage_pips"] = (merged["fill_price"] - merged["entry_price"]) / pip_map * direction_sign
    else:
        merged["slippage_pips"] = np.nan

    return merged[base_cols].copy()


# ── Matching ──────────────────────────────────────────────────────────


def _pair_pip_value(pair: str) -> float:
    """Minimal pip-unit table. Matches ``ff.pair_util`` defaults."""
    return 0.01 if "JPY" in pair else 0.0001


def reconcile(
    bt_trades: pd.DataFrame,
    live_trades: pd.DataFrame,
    tolerances: Tolerances | None = None,
) -> ReconcileReport:
    """Join backtest ↔ live per-trade and classify every pair.

    Exact-key join on ``(pair, direction, signal_bar_ts)`` first; residual
    rows swept across ± ``signal_bar_window`` main-TF bars.
    """
    tol = tolerances or Tolerances()

    bt = bt_trades.copy()
    live = live_trades.copy()

    # Empty-input guards — nothing to match, return a pristine report.
    if bt.empty and live.empty:
        return ReconcileReport(
            matched=[],
            missing_in_live=[],
            extra_in_live=[],
            tolerances=tol,
            generated_at=pd.Timestamp.now("UTC").isoformat(),
        )

    # Normalise signal_bar_ts on the backtest side: derive from entry_ts
    # rounded down to the main-TF boundary if the caller didn't supply it.
    if not bt.empty and "signal_bar_ts" not in bt.columns:
        entry = pd.to_datetime(bt["entry_ts"], utc=True)
        floor = entry.dt.floor(f"{tol.signal_bar_minutes}min")
        bt["signal_bar_ts"] = floor

    if bt.empty:
        bt["_k"] = []
    else:
        bt["_k"] = list(zip(bt["pair"], bt["direction"].astype(int), bt["signal_bar_ts"]))
    if live.empty:
        live["_k"] = []
    else:
        live["_k"] = list(zip(live["pair"], live["direction"].astype(int), live["signal_bar_ts"]))

    bt_by_key = {k: i for i, k in enumerate(bt["_k"])}
    live_by_key = {k: i for i, k in enumerate(live["_k"])}

    bt_used = set()
    live_used = set()

    matched: list[MatchedRow] = []

    # 1. Exact-key join
    for k, bt_i in bt_by_key.items():
        if k in live_by_key:
            live_i = live_by_key[k]
            matched.append(_classify(bt.iloc[bt_i], live.iloc[live_i], tol))
            bt_used.add(bt_i)
            live_used.add(live_i)

    # 2. Fuzzy sweep — same (pair, direction), ± window bars
    if tol.signal_bar_window > 0:
        window = timedelta(minutes=tol.signal_bar_minutes * tol.signal_bar_window)
        for bt_i in set(range(len(bt))) - bt_used:
            bt_row = bt.iloc[bt_i]
            bt_ts = pd.Timestamp(bt_row["signal_bar_ts"])
            candidates = [
                i
                for i in set(range(len(live))) - live_used
                if live.iloc[i]["pair"] == bt_row["pair"]
                and int(live.iloc[i]["direction"]) == int(bt_row["direction"])
                and abs(pd.Timestamp(live.iloc[i]["signal_bar_ts"]) - bt_ts) <= window
            ]
            if candidates:
                best = min(
                    candidates,
                    key=lambda i: abs(pd.Timestamp(live.iloc[i]["signal_bar_ts"]) - bt_ts),
                )
                matched.append(_classify(bt_row, live.iloc[best], tol))
                bt_used.add(bt_i)
                live_used.add(best)

    missing = [bt.iloc[i].to_dict() for i in set(range(len(bt))) - bt_used]
    extra = [live.iloc[i].to_dict() for i in set(range(len(live))) - live_used]

    return ReconcileReport(
        matched=matched,
        missing_in_live=missing,
        extra_in_live=extra,
        tolerances=tol,
        generated_at=pd.Timestamp.now("UTC").isoformat(),
    )


def _classify(bt_row: pd.Series, live_row: pd.Series, tol: Tolerances) -> MatchedRow:
    pair = str(bt_row["pair"])
    pip = _pair_pip_value(pair)
    direction = int(bt_row["direction"])

    bt_entry = float(bt_row["entry_price"])
    live_entry = float(live_row["entry_price"]) if pd.notna(live_row["entry_price"]) else np.nan
    bt_exit = float(bt_row["exit_price"])
    live_exit = float(live_row["exit_price"]) if pd.notna(live_row["exit_price"]) else np.nan
    bt_pnl = float(bt_row["pnl_pips"])
    live_pnl = float(live_row["pnl_pips"]) if pd.notna(live_row["pnl_pips"]) else np.nan

    # Directional delta — a higher live entry for a long is slippage against us.
    entry_delta = (live_entry - bt_entry) / pip * (1 if direction > 0 else -1)
    exit_delta = (live_exit - bt_exit) / pip * (1 if direction > 0 else -1)
    pnl_delta = live_pnl - bt_pnl

    # Parity-v2 extractions. Every field is defensive — older plans / tickets
    # won't carry them and tolerances silently skip on NaN.
    bt_variant = int(bt_row["signal_variant_id"]) if "signal_variant_id" in bt_row else -1
    live_variant = int(live_row["signal_variant"]) if "signal_variant" in live_row and pd.notna(live_row.get("signal_variant")) else -1
    bt_family = str(bt_row.get("signal_family", ""))
    live_family = str(live_row.get("signal_family", ""))
    bt_spread = float(bt_row.get("spread_entry_pips", np.nan))
    live_spread = float(live_row.get("spread_pips", np.nan))
    spread_delta = (live_spread - bt_spread) if (np.isfinite(bt_spread) and np.isfinite(live_spread)) else np.nan
    live_slippage = float(live_row.get("slippage_pips", np.nan))
    bt_close = str(bt_row.get("exit_reason_name", ""))
    live_close = str(live_row.get("close_reason", ""))

    # Cost-realism columns. Legacy bt rows have none of these; fall back to
    # bt_pnl_pips so raw/effective always have a number to display.
    bt_raw_pnl = float(bt_row.get("raw_pnl_pips", bt_pnl)) if pd.notna(bt_row.get("raw_pnl_pips", bt_pnl)) else bt_pnl
    bt_overlay_delta = float(bt_row.get("overlay_delta_pips", np.nan)) if pd.notna(bt_row.get("overlay_delta_pips", np.nan)) else np.nan
    bt_adjusted = float(bt_row.get("adjusted_pnl_pips", np.nan)) if pd.notna(bt_row.get("adjusted_pnl_pips", np.nan)) else np.nan
    bt_gated_reason = str(bt_row.get("gated_out_reason", "") or "")
    raw_effective = bt_row.get("effective_pnl_pips", np.nan)
    if pd.notna(raw_effective):
        bt_effective = float(raw_effective)
    elif np.isfinite(bt_adjusted):
        bt_effective = 0.0 if bt_gated_reason else bt_adjusted
    else:
        bt_effective = bt_pnl

    categories = []
    if abs(entry_delta) > tol.entry_price_pips:
        categories.append("mismatched_entry_price")
    if abs(exit_delta) > tol.exit_price_pips:
        categories.append("mismatched_exit_price")
    if abs(pnl_delta) > tol.pnl_pips:
        categories.append("mismatched_pnl")

    # Signal parity — hard equality. Skip if either side is missing.
    if bt_variant >= 0 and live_variant >= 0 and bt_variant != live_variant:
        categories.append("mismatched_signal")

    # Spread parity — skip when either side is missing.
    if np.isfinite(spread_delta) and abs(spread_delta) > tol.spread_pips:
        categories.append("mismatched_spread")

    # Slippage — flag when magnitude exceeds tolerance. Live-side metric only.
    if np.isfinite(live_slippage) and abs(live_slippage) > tol.slippage_pips:
        categories.append("mismatched_slippage")

    # Close reason — canonicalise to {SL, TP, OTHER}. Only SL↔!SL and TP↔!TP
    # count as divergence. Engine-managed exits (trailing/BE/chandelier) all
    # canonicalise to OTHER and never flag against live's EXPERT.
    if _canonical_close_reason(bt_close) != _canonical_close_reason(live_close):
        if bt_close or live_close:  # at least one side must be populated
            categories.append("mismatched_closure")

    return MatchedRow(
        plan_id=str(live_row.get("plan_id", "")),
        pair=pair,
        direction=direction,
        signal_bar_ts=str(bt_row["signal_bar_ts"]),
        bt_entry_price=bt_entry,
        live_entry_price=live_entry,
        entry_delta_pips=entry_delta,
        bt_exit_price=bt_exit,
        live_exit_price=live_exit,
        exit_delta_pips=exit_delta,
        bt_pnl_pips=bt_pnl,
        live_pnl_pips=live_pnl,
        pnl_delta_pips=pnl_delta,
        bt_signal_variant=bt_variant,
        live_signal_variant=live_variant,
        bt_signal_family=bt_family,
        live_signal_family=live_family,
        bt_spread_pips=bt_spread,
        live_spread_pips=live_spread,
        spread_delta_pips=spread_delta,
        live_slippage_pips=live_slippage,
        bt_close_reason=bt_close,
        live_close_reason=live_close,
        bt_raw_pnl_pips=bt_raw_pnl,
        bt_overlay_delta_pips=bt_overlay_delta,
        bt_adjusted_pnl_pips=bt_adjusted,
        bt_gated_out_reason=bt_gated_reason,
        bt_effective_pnl_pips=bt_effective,
        categories=categories,
    )


# ── Rendering ──────────────────────────────────────────────────────────


def render_report_json(report: ReconcileReport) -> str:
    payload = {
        "generated_at": report.generated_at,
        "counts": report.counts,
        "tolerances": report.tolerances.__dict__,
        "matched": [m.__dict__ for m in report.matched],
        "missing_in_live": report.missing_in_live,
        "extra_in_live": report.extra_in_live,
    }
    return json.dumps(payload, default=str, indent=2)


def render_report_html(report: ReconcileReport) -> str:
    """Self-contained HTML. Dark monospace, re-uses the comparison look.

    Layout: per-pair summary at the top, then per-trade table with every
    parity field (signal, spread, slippage, close reason) side-by-side.
    """

    def _fmt(v, fmt: str = ".2f") -> str:
        if v is None:
            return "—"
        try:
            if isinstance(v, float) and not np.isfinite(v):
                return "—"
            return format(v, fmt)
        except (TypeError, ValueError):
            return str(v)

    # Per-pair summary.
    pair_rows = []
    for pair, slot in sorted(report.by_pair().items()):
        pair_rows.append(
            f"<tr>"
            f"<td>{pair}</td>"
            f"<td>{slot['matched']}</td>"
            f"<td>{slot['missing_in_live']}</td>"
            f"<td>{slot['extra_in_live']}</td>"
            f"<td>{slot['delta_pips']:+.2f}</td>"
            f"<td>{_fmt(slot['mean_spread_delta_pips'])}</td>"
            f"<td>{_fmt(slot['mean_slippage_pips'])}</td>"
            f"<td>{slot['n_mismatched_signal']}</td>"
            f"<td>{slot['n_mismatched_closure']}</td>"
            f"</tr>"
        )

    # Per-trade table — every compared field.
    rows = []
    for m in report.matched:
        cls = "matched" if not m.categories else "mismatch"
        cats = ", ".join(m.categories) if m.categories else "ok"
        rows.append(
            f"<tr class='{cls}'>"
            f"<td>{m.pair}</td><td>{m.direction:+d}</td>"
            f"<td>{m.signal_bar_ts}</td>"
            f"<td>{m.bt_signal_variant}</td><td>{m.live_signal_variant}</td>"
            f"<td>{m.bt_signal_family or '—'}</td>"
            f"<td>{m.bt_entry_price:.5f}</td><td>{_fmt(m.live_entry_price, '.5f')}</td>"
            f"<td>{m.entry_delta_pips:+.2f}</td>"
            f"<td>{m.bt_exit_price:.5f}</td><td>{_fmt(m.live_exit_price, '.5f')}</td>"
            f"<td>{m.exit_delta_pips:+.2f}</td>"
            f"<td>{_fmt(m.bt_spread_pips)}</td><td>{_fmt(m.live_spread_pips)}</td>"
            f"<td>{_fmt(m.live_slippage_pips, '+.2f')}</td>"
            f"<td>{m.bt_close_reason or '—'}</td><td>{m.live_close_reason or '—'}</td>"
            f"<td>{m.bt_pnl_pips:+.2f}</td><td>{_fmt(m.live_pnl_pips, '+.2f')}</td>"
            f"<td>{m.pnl_delta_pips:+.2f}</td>"
            f"<td>{cats}</td>"
            f"</tr>"
        )

    rows_missing = "\n".join(f"<tr class='missing'><td colspan='20'>missing: {r}</td></tr>" for r in report.missing_in_live)
    rows_extra = "\n".join(f"<tr class='extra'><td colspan='20'>extra: {r}</td></tr>" for r in report.extra_in_live)
    counts = report.counts

    return f"""<!doctype html>
<html><head><meta charset='utf-8'><title>Fire Forex · live reconcile</title>
<style>
body {{ font-family: 'Consolas', monospace; background:#0b0d11; color:#ddd; }}
table {{ border-collapse:collapse; width:100%; margin-bottom:24px; }}
th, td {{ padding:4px 8px; border-bottom:1px solid #2a2f38; text-align:right; }}
th {{ color:#8ca; text-align:left; }}
td:first-child, td:nth-child(3) {{ text-align:left; }}
tr.matched td {{ color:#dfe; }}
tr.mismatch td {{ color:#fc7; }}
tr.missing td {{ color:#f77; }}
tr.extra td {{ color:#77d; }}
h1, h2 {{ color:#fff; }}
.counts {{ color:#9ad; margin:8px 0 16px; }}
</style></head>
<body>
<h1>Fire Forex · live parity reconcile</h1>
<div class='counts'>Generated: {report.generated_at} · {counts}</div>

<h2>Per-pair summary</h2>
<table>
<thead><tr>
<th>pair</th><th>matched</th><th>missing</th><th>extra</th>
<th>Δpnl (pips)</th><th>mean Δspread</th><th>mean slippage</th>
<th>#signal mismatch</th><th>#closure mismatch</th>
</tr></thead>
<tbody>
{chr(10).join(pair_rows)}
</tbody></table>

<h2>Per-trade breakdown</h2>
<table>
<thead><tr>
<th>pair</th><th>dir</th><th>signal_bar_ts</th>
<th>bt variant</th><th>live variant</th><th>family</th>
<th>bt_entry</th><th>live_entry</th><th>Δentry</th>
<th>bt_exit</th><th>live_exit</th><th>Δexit</th>
<th>bt_spread</th><th>live_spread</th>
<th>slippage</th>
<th>bt_close</th><th>live_close</th>
<th>bt_pnl</th><th>live_pnl</th><th>Δpnl</th>
<th>category</th>
</tr></thead>
<tbody>
{chr(10).join(rows)}
{rows_missing}
{rows_extra}
</tbody></table>
</body></html>
"""


def write_report(report: ReconcileReport, out_dir: Path, stamp: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{stamp}.html"
    json_path = out_dir / f"{stamp}.json"
    html_path.write_text(render_report_html(report), encoding="utf-8")
    json_path.write_text(render_report_json(report), encoding="utf-8")
    return html_path, json_path
