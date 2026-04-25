"""Daily live-vs-backtest parity check.

Loads the latest MT5 trade history (from ``artifacts/live/incoming/``) and the
latest BT replay output (from ``artifacts/replay/``) and compares trade-by-trade
within a recent time window (default: last 48 hours of MT5 closes).

Usage::

    .venv/Scripts/python.exe scripts/daily_parity_check.py [--hours 48]
                                                          [--data-source dukascopy|mt5|both]

Outputs:
  * Stdout: one-screen summary (matched / missing / extra counts + P&L drift).
  * ``artifacts/parity/<stamp>_parity.md`` — markdown table per data source.

Match key: (pair, direction, entry_ts within tolerance). MT5 symbols like
``EURUSD`` are normalised to ``EUR_USD``. MT5 type ``buy`` -> direction +1,
``sell`` -> -1. Default tolerance: ±30 minutes (we trade M15, so an off-by-one
bar shouldn't fail the match).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
INCOMING = ROOT / "artifacts" / "live" / "incoming"
REPLAY_DIR = ROOT / "artifacts" / "replay"
OUT_DIR = ROOT / "artifacts" / "parity"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _latest_mt5_history() -> Path | None:
    matches = sorted(INCOMING.glob("mt5_history_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _active_bundle_run_ids() -> list[str]:
    """Read deploy/instances/active.json and strip deploy-stamp suffixes."""
    p = ROOT / "deploy" / "instances" / "active.json"
    active = json.loads(p.read_text(encoding="utf-8-sig"))["active"]
    # instance_id = "<source_run_id>__<deploy_stamp>"; we want source_run_id only.
    return [a.split("__")[0] for a in active]


def _latest_bt_npzs(data_source: str) -> list[Path]:
    """One latest NPZ per active bundle's source_run_id (3 frozen variants run
    in parallel — comparing against just the most-recent NPZ globally would
    miss two thirds of the BT trades)."""
    out: list[Path] = []
    for run_id in _active_bundle_run_ids():
        candidates = sorted(
            (REPLAY_DIR / run_id).glob(f"*_{data_source}/trades.npz"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            out.append(candidates[0])
    return out


def _parse_mt5_time(s: str) -> datetime:
    # MT5 importer outputs "2026.04.24 13:33:27" — already broker→UTC converted.
    return datetime.strptime(s, "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _normalize_symbol(sym: str) -> str:
    if "_" in sym or len(sym) != 6:
        return sym
    return f"{sym[:3]}_{sym[3:]}"


def load_mt5_trades(path: Path, hours: int) -> list[dict]:
    data = json.load(path.open(encoding="utf-8"))
    trades = data if isinstance(data, list) else data
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out: list[dict] = []
    for t in trades:
        try:
            ct = _parse_mt5_time(str(t["time_close"]))
            ot = _parse_mt5_time(str(t["time_open"]))
        except (KeyError, ValueError):
            continue
        if ct < cutoff:
            continue
        out.append(
            {
                "pair": _normalize_symbol(str(t["symbol"])),
                "direction": 1 if str(t["type"]).lower() in ("buy", "0") else -1,
                "entry_ts": ot,
                "exit_ts": ct,
                "entry_price": float(t["price_open"]),
                "exit_price": float(t["price_close"]),
                "pnl": float(t["profit"]),
                "comment": str(t.get("comment", "") or ""),
            }
        )
    out.sort(key=lambda x: x["exit_ts"])
    return out


def load_bt_trades(npz_path: Path, hours: int) -> list[dict]:
    arr = np.load(npz_path, allow_pickle=True)["trades"]
    cutoff_ns = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1e9)
    out: list[dict] = []
    for r in arr:
        try:
            exit_ns = int(r["exit_ts"].astype("int64"))
            entry_ns = int(r["entry_ts"].astype("int64"))
        except Exception:
            continue
        if exit_ns < cutoff_ns:
            continue
        out.append(
            {
                "pair": str(r["pair"]),
                "direction": int(r["direction"]),
                "entry_ts": datetime.fromtimestamp(entry_ns / 1e9, tz=timezone.utc),
                "exit_ts": datetime.fromtimestamp(exit_ns / 1e9, tz=timezone.utc),
                "entry_price": float(r["entry_price"]),
                "exit_price": float(r["exit_price"]),
                "pnl_pips": float(r["pnl_pips"]),
                "signal_family": str(r["signal_family"]),
                "exit_reason": str(r["exit_reason_name"]),
            }
        )
    out.sort(key=lambda x: x["exit_ts"])
    return out


def match_trades(mt5: list[dict], bt: list[dict], time_tol_min: int = 30) -> list[dict]:
    """Greedy nearest-neighbour match keyed on (pair, direction, entry_ts)."""
    used: set[int] = set()
    rows: list[dict] = []
    tol = timedelta(minutes=time_tol_min)
    for m in mt5:
        best_idx = -1
        best_dt = tol + timedelta(seconds=1)
        for i, b in enumerate(bt):
            if i in used:
                continue
            if b["pair"] != m["pair"] or b["direction"] != m["direction"]:
                continue
            dt = abs(b["entry_ts"] - m["entry_ts"])
            if dt < best_dt:
                best_dt = dt
                best_idx = i
        if best_idx >= 0:
            used.add(best_idx)
            rows.append({"status": "matched", "mt5": m, "bt": bt[best_idx], "delta_min": best_dt.total_seconds() / 60})
        else:
            rows.append({"status": "missing_in_bt", "mt5": m, "bt": None, "delta_min": None})
    for i, b in enumerate(bt):
        if i not in used:
            rows.append({"status": "extra_in_bt", "mt5": None, "bt": b, "delta_min": None})
    return rows


def _format_md_row(r: dict) -> str:
    if r["status"] == "matched":
        m, b = r["mt5"], r["bt"]
        side = "BUY" if m["direction"] == 1 else "SELL"
        return (
            f"| {m['pair']} | {side} | {m['entry_ts']:%m-%d %H:%M} | "
            f"{b['entry_ts']:%m-%d %H:%M} | {r['delta_min']:.0f} | "
            f"{m['pnl']:+.2f} | {b['pnl_pips']:+.1f} | matched |"
        )
    if r["status"] == "missing_in_bt":
        m = r["mt5"]
        side = "BUY" if m["direction"] == 1 else "SELL"
        return f"| {m['pair']} | {side} | {m['entry_ts']:%m-%d %H:%M} | — | — | {m['pnl']:+.2f} | — | missing |"
    b = r["bt"]
    side = "BUY" if b["direction"] == 1 else "SELL"
    return f"| {b['pair']} | {side} | — | {b['entry_ts']:%m-%d %H:%M} | — | — | {b['pnl_pips']:+.1f} | extra |"


def main() -> int:
    ap = argparse.ArgumentParser(prog="daily_parity_check.py")
    ap.add_argument("--hours", type=int, default=48, help="window in hours (default 48)")
    ap.add_argument("--data-source", choices=("dukascopy", "mt5", "both"), default="both")
    ap.add_argument("--tol-min", type=int, default=30, help="entry-time match tolerance (min)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    mt5_path = _latest_mt5_history()
    if mt5_path is None:
        print("ERROR: no MT5 history in artifacts/live/incoming/")
        return 1

    sources = ("dukascopy", "mt5") if args.data_source == "both" else (args.data_source,)
    bt_paths = {src: _latest_bt_npzs(src) for src in sources}

    print(f"MT5 history : {mt5_path.name}")
    for src, paths in bt_paths.items():
        if not paths:
            print(f"BT ({src:9s}): MISSING (no NPZ for any active bundle)")
        else:
            print(f"BT ({src:9s}): {len(paths)} NPZ(s) across active bundles")
            for p in paths:
                print(f"             - {p.relative_to(ROOT)}")
    print(f"Window      : last {args.hours}h, entry-time tol ±{args.tol_min} min")

    mt5_trades = load_mt5_trades(mt5_path, args.hours)
    print(f"\nMT5 closes in window: {len(mt5_trades)}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = OUT_DIR / f"{stamp}_parity.md"
    lines = [
        f"# Parity report — {stamp}",
        "",
        f"- MT5 history: `{mt5_path.relative_to(ROOT)}`",
        f"- Window: last {args.hours}h",
        f"- Entry-time match tolerance: ±{args.tol_min} min",
        f"- MT5 closes in window: **{len(mt5_trades)}**",
    ]

    for src, paths in bt_paths.items():
        print(f"\n=== {src.upper()} ===")
        if not paths:
            print(f"  BT replay output MISSING — run `python run.py replay <bundle> --data-source {src}` first.")
            lines += ["", f"## {src.upper()}", "", "**Missing BT replay output.**"]
            continue
        bt_trades: list[dict] = []
        for p in paths:
            bt_trades.extend(load_bt_trades(p, args.hours))
        bt_trades.sort(key=lambda t: t["exit_ts"])
        rows = match_trades(mt5_trades, bt_trades, time_tol_min=args.tol_min)
        matched = sum(1 for r in rows if r["status"] == "matched")
        missing = sum(1 for r in rows if r["status"] == "missing_in_bt")
        extra = sum(1 for r in rows if r["status"] == "extra_in_bt")
        mt5_pnl = sum(r["mt5"]["pnl"] for r in rows if r["status"] == "matched")
        bt_pips = sum(r["bt"]["pnl_pips"] for r in rows if r["status"] == "matched")
        print(f"  matched         : {matched}")
        print(f"  missing in BT   : {missing}  (MT5 traded, BT didn't)")
        print(f"  extra in BT     : {extra}  (BT traded, MT5 didn't)")
        print(f"  MT5 pnl matched : {mt5_pnl:+.2f} GBP")
        print(f"  BT pips matched : {bt_pips:+.1f}")
        lines += (
            [
                "",
                f"## {src.upper()}",
                "",
                f"- BT NPZs ({len(paths)} active bundle(s)):",
            ]
            + [f"  - `{p.relative_to(ROOT)}`" for p in paths]
            + [
                f"- BT trades in window: **{len(bt_trades)}**",
                f"- Matched: **{matched}**, Missing in BT: **{missing}**, Extra in BT: **{extra}**",
                f"- MT5 P&L on matched: **{mt5_pnl:+.2f} GBP**, BT pips on matched: **{bt_pips:+.1f}**",
                "",
                "| Pair | Side | MT5 entry | BT entry | Δ min | MT5 P&L | BT pips | Status |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        rows.sort(key=lambda r: (r["status"], (r["mt5"] or r["bt"])["entry_ts"]))
        lines += [_format_md_row(r) for r in rows]

    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport written: {md_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
