"""Pull MT5 closed-trade history directly from the running terminal.

Queries MT5 via the ``MetaTrader5`` Python package — no manual export needed.
The terminal must be open and logged in. Output: normalised CSV + JSON in
``artifacts/live/incoming/mt5_history_<stamp>.{csv,json}``.

Run::

    .\\.venv\\Scripts\\python.exe scripts/import_mt5_report.py
    .\\.venv\\Scripts\\python.exe scripts/import_mt5_report.py --days 30
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "artifacts" / "live" / "incoming"

COLUMNS = [
    "time_open",
    "position",
    "symbol",
    "type",
    "comment",
    "volume",
    "price_open",
    "sl",
    "tp",
    "time_close",
    "price_close",
    "commission",
    "swap",
    "profit",
]

# ───────────────────────────── direct MT5 path ─────────────────────────────


def _connect_mt5() -> tuple[Any, int]:
    """Attach to MT5; return (mt5 module, broker-to-UTC offset in seconds).

    MT5 timestamps are broker-local (e.g. IC Markets = GMT+2/+3). Always
    apply the offset before formatting / comparing against backtest UTC.
    Same pattern as `ff/live/broker_mt5.py`.
    """
    try:
        import MetaTrader5 as mt5
    except ImportError as e:
        raise RuntimeError(
            "MetaTrader5 Python package is not installed. "
            "Install with `pip install MetaTrader5` (Windows-only) or use --source <html-path>."
        ) from e

    if not mt5.initialize():
        err = mt5.last_error()
        raise RuntimeError(f"MT5 initialize() failed: {err}. Open MetaTrader 5 terminal and ensure it's logged in, then retry.")

    import time as _time

    broker_to_utc_sec = 0
    tick = mt5.symbol_info_tick("EURUSD")
    if tick is not None and tick.time:
        broker_to_utc_sec = -(int(tick.time) - int(_time.time()))
    return mt5, broker_to_utc_sec


def _to_utc_str(broker_ts: int, broker_to_utc_sec: int) -> str:
    return datetime.fromtimestamp(broker_ts + broker_to_utc_sec, tz=timezone.utc).strftime("%Y.%m.%d %H:%M:%S")


def fetch_from_mt5(days_back: int) -> list[dict[str, str]]:
    """Pull closed positions from MT5 for the last ``days_back`` days."""
    mt5, offset = _connect_mt5()
    try:
        date_to = datetime.now(timezone.utc)
        date_from = date_to - timedelta(days=days_back)
        deals = mt5.history_deals_get(date_from, date_to) or ()
        # Order map for SL/TP enrichment — deals don't carry SL/TP, but the
        # entry order does. One round-trip beats N per-position lookups.
        orders = mt5.history_orders_get(date_from, date_to) or ()
        order_by_id = {o.ticket: o for o in orders}

        # Group deals by position_id; ENTRY=in (entry leg), OUT=out (closing leg).
        by_position: dict[int, list[Any]] = defaultdict(list)
        for d in deals:
            if d.position_id:
                by_position[d.position_id].append(d)

        positions: list[dict[str, str]] = []
        for pos_id, leg in by_position.items():
            leg_sorted = sorted(leg, key=lambda d: d.time)
            entries = [d for d in leg_sorted if d.entry == mt5.DEAL_ENTRY_IN]
            exits = [d for d in leg_sorted if d.entry == mt5.DEAL_ENTRY_OUT]
            if not entries or not exits:
                continue  # still open or partial — skip until closed
            entry, exit_ = entries[0], exits[-1]
            net_profit = sum(d.profit for d in leg_sorted)
            net_commission = sum(d.commission for d in leg_sorted)
            net_swap = sum(d.swap for d in leg_sorted)
            entry_order = order_by_id.get(entry.order)
            sl = f"{entry_order.sl:.5f}" if entry_order and entry_order.sl else ""
            tp = f"{entry_order.tp:.5f}" if entry_order and entry_order.tp else ""
            side = "buy" if entry.type == mt5.DEAL_TYPE_BUY else "sell"
            positions.append(
                {
                    "time_open": _to_utc_str(entry.time, offset),
                    "position": str(pos_id),
                    "symbol": entry.symbol,
                    "type": side,
                    "comment": entry.comment or "",
                    "volume": f"{entry.volume:.2f}",
                    "price_open": f"{entry.price:.5f}",
                    "sl": sl,
                    "tp": tp,
                    "time_close": _to_utc_str(exit_.time, offset),
                    "price_close": f"{exit_.price:.5f}",
                    "commission": f"{net_commission:.2f}",
                    "swap": f"{net_swap:.2f}",
                    "profit": f"{net_profit:.2f}",
                }
            )
        return sorted(positions, key=lambda p: p["time_open"])
    finally:
        mt5.shutdown()


# ───────────────────────────── write + summary ─────────────────────────────


def _summarise(positions: list[dict[str, str]]) -> str:
    if not positions:
        return "No closed positions to summarise."

    total = len(positions)
    profits = [float(p["profit"]) for p in positions]
    wins = sum(1 for p in profits if p > 0)
    net = sum(profits)
    dates = sorted(p["time_open"] for p in positions)

    by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "wins": 0, "profit": 0.0})
    for p in positions:
        b = by_symbol[p["symbol"]]
        b["n"] += 1
        b["profit"] += float(p["profit"])
        if float(p["profit"]) > 0:
            b["wins"] += 1

    lines = [
        f"TOTAL: {total} closed trades, {wins} wins ({100 * wins / total:.0f}%), net {net:+.2f}",
        f"Date range: {dates[0]} -> {dates[-1]}",
        "By symbol:",
    ]
    for sym in sorted(by_symbol):
        b = by_symbol[sym]
        lines.append(f"  {sym}: {int(b['n'])} trades, {int(b['wins'])} wins ({100 * b['wins'] / b['n']:.0f}%), net {b['profit']:+.2f}")
    return "\n".join(lines)


def _write(positions: list[dict[str, str]], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    csv_path = out_dir / f"mt5_history_{stamp}.csv"
    json_path = out_dir / f"mt5_history_{stamp}.json"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(positions)
    json_path.write_text(json.dumps(positions, indent=2), encoding="utf-8")
    return csv_path, json_path


# ───────────────────────────── CLI ─────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14, help="How many days back to fetch (default 14).")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT, help=f"Output directory (default: {DEFAULT_OUT}).")
    parser.add_argument("--quiet", action="store_true", help="Suppress summary output.")
    args = parser.parse_args(argv)

    try:
        positions = fetch_from_mt5(args.days)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if not positions:
        print(f"No closed positions found in the last {args.days} days.", file=sys.stderr)
        return 1

    csv_path, json_path = _write(positions, args.out_dir)

    if not args.quiet:
        print(f"Source: MT5 terminal (last {args.days} days)")
        print(f"Wrote:  {csv_path}")
        print(f"Wrote:  {json_path}")
        print()
        print(_summarise(positions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
