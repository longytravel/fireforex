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

    One row per live trade with columns that mirror the backtest trade log:
    plan_id, pair, direction, signal_bar_ts, entry_price, exit_price,
    pnl_pips. The reconciler matches on those.
    """
    plans_df = pd.DataFrame(list(plans))
    tickets_df = pd.DataFrame(list(tickets))
    deals_df = pd.DataFrame(list(deals))

    if plans_df.empty:
        return pd.DataFrame(columns=[
            "plan_id", "pair", "direction", "signal_bar_ts",
            "entry_price", "exit_price", "pnl_pips",
        ])

    plans_df["signal_bar_ts"] = pd.to_datetime(plans_df["signal_bar_ts"], utc=True)

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
        merged["exit_price"] = merged["_close_px"]
        # Raw pnl in pips: directional diff / pip_value. Fall back to broker profit scaled
        # elsewhere. For majors this is good enough for sanity; JPY pairs need pip scale.
        merged["pnl_pips"] = np.where(
            merged["direction"] > 0,
            (merged["_close_px"] - merged["_open_px"]) / 1e-4,
            (merged["_open_px"] - merged["_close_px"]) / 1e-4,
        )

    cols = ["plan_id", "pair", "direction", "signal_bar_ts",
            "entry_ref_price", "exit_price", "pnl_pips"]
    merged = merged.rename(columns={"entry_ref_price": "entry_price"})
    out_cols = ["plan_id", "pair", "direction", "signal_bar_ts",
                "entry_price", "exit_price", "pnl_pips"]
    return merged[out_cols].copy()


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
        return ReconcileReport(matched=[], missing_in_live=[], extra_in_live=[],
                               tolerances=tol,
                               generated_at=pd.Timestamp.now("UTC").isoformat())

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
                i for i in set(range(len(live))) - live_used
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

    categories = []
    if abs(entry_delta) > tol.entry_price_pips:
        categories.append("mismatched_entry_price")
    if abs(exit_delta) > tol.exit_price_pips:
        categories.append("mismatched_exit_price")
    if abs(pnl_delta) > tol.pnl_pips:
        categories.append("mismatched_pnl")

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
    """Small self-contained HTML. Re-uses the visual style of
    ``artifacts/comparison.html`` (dark, monospace) without importing it.
    """
    rows = []
    for m in report.matched:
        cls = "matched" if not m.categories else "mismatch"
        cats = ", ".join(m.categories) if m.categories else "ok"
        rows.append(
            f"<tr class='{cls}'>"
            f"<td>{m.pair}</td><td>{m.direction:+d}</td>"
            f"<td>{m.signal_bar_ts}</td>"
            f"<td>{m.bt_entry_price:.5f}</td><td>{m.live_entry_price:.5f}</td>"
            f"<td>{m.entry_delta_pips:+.2f}</td>"
            f"<td>{m.bt_exit_price:.5f}</td><td>{m.live_exit_price:.5f}</td>"
            f"<td>{m.exit_delta_pips:+.2f}</td>"
            f"<td>{m.bt_pnl_pips:+.2f}</td><td>{m.live_pnl_pips:+.2f}</td>"
            f"<td>{m.pnl_delta_pips:+.2f}</td>"
            f"<td>{cats}</td>"
            f"</tr>"
        )

    rows_missing = "\n".join(
        f"<tr class='missing'><td colspan='13'>missing: {r}</td></tr>"
        for r in report.missing_in_live
    )
    rows_extra = "\n".join(
        f"<tr class='extra'><td colspan='13'>extra: {r}</td></tr>"
        for r in report.extra_in_live
    )
    counts = report.counts

    return f"""<!doctype html>
<html><head><meta charset='utf-8'><title>Fire Forex · live reconcile</title>
<style>
body {{ font-family: 'Consolas', monospace; background:#0b0d11; color:#ddd; }}
table {{ border-collapse:collapse; width:100%; }}
th, td {{ padding:4px 8px; border-bottom:1px solid #2a2f38; text-align:right; }}
th {{ color:#8ca; text-align:left; }}
td:first-child, td:nth-child(3) {{ text-align:left; }}
tr.matched td {{ color:#dfe; }}
tr.mismatch td {{ color:#fc7; }}
tr.missing td {{ color:#f77; }}
tr.extra td {{ color:#77d; }}
h1 {{ color:#fff; }}
.counts {{ color:#9ad; margin:8px 0 16px; }}
</style></head>
<body>
<h1>Fire Forex · live parity reconcile</h1>
<div class='counts'>Generated: {report.generated_at} · {counts}</div>
<table>
<thead><tr>
<th>pair</th><th>dir</th><th>signal_bar_ts</th>
<th>bt_entry</th><th>live_entry</th><th>Δentry</th>
<th>bt_exit</th><th>live_exit</th><th>Δexit</th>
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
