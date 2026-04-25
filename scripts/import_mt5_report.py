"""Import MT5 closed-trade history into ``artifacts/live/incoming/``.

Two source modes:

1. ``--source mt5`` (default) — query the running MT5 terminal directly via the
   ``MetaTrader5`` Python package. The user does NOT need to export anything.
   The terminal must be open and logged in (which it is during live trading).
2. ``--source <path>`` — fall back to parsing an exported ``ReportHistory-*.html``
   from the MT5 terminal (UTF-16 LE). Useful when MT5 isn't running locally.

Default behaviour: try MT5 direct; if that fails, look for the newest
``ReportHistory*.html`` on the Desktop. So the typical user runs::

    .\\.venv\\Scripts\\python.exe scripts/import_mt5_report.py

and gets the last ``--days`` (default 14) of closed trades, with no manual
HTML export needed.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
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

_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}")


# ───────────────────────────── direct MT5 path ─────────────────────────────


def _connect_mt5() -> Any:
    """Attach to the running MT5 terminal. Returns the imported module."""
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
    return mt5


def fetch_from_mt5(days_back: int) -> list[dict[str, str]]:
    """Pull closed positions from MT5 for the last ``days_back`` days."""
    mt5 = _connect_mt5()
    try:
        date_to = datetime.now(timezone.utc)
        date_from = date_to - timedelta(days=days_back)
        deals = mt5.history_deals_get(date_from, date_to) or ()

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
            t_open = datetime.fromtimestamp(entry.time, tz=timezone.utc).strftime("%Y.%m.%d %H:%M:%S")
            t_close = datetime.fromtimestamp(exit_.time, tz=timezone.utc).strftime("%Y.%m.%d %H:%M:%S")
            side = "buy" if entry.type == mt5.DEAL_TYPE_BUY else "sell"
            positions.append(
                {
                    "time_open": t_open,
                    "position": str(pos_id),
                    "symbol": entry.symbol,
                    "type": side,
                    "comment": entry.comment or "",
                    "volume": f"{entry.volume:.2f}",
                    "price_open": f"{entry.price:.5f}",
                    "sl": "",
                    "tp": "",
                    "time_close": t_close,
                    "price_close": f"{exit_.price:.5f}",
                    "commission": f"{net_commission:.2f}",
                    "swap": f"{net_swap:.2f}",
                    "profit": f"{net_profit:.2f}",
                }
            )
        return sorted(positions, key=lambda p: p["time_open"])
    finally:
        mt5.shutdown()


# ───────────────────────────── HTML fallback path ─────────────────────────────


def _newest_desktop_report() -> Path | None:
    desktop = Path.home() / "Desktop"
    candidates = sorted(
        desktop.glob("ReportHistory*.html"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _decode_mt5_html(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le").lstrip("﻿")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be").lstrip("﻿")
    return raw.decode("utf-8-sig", errors="replace")


def _strip(cell: str) -> str:
    return _TAG_RE.sub("", cell).replace("&nbsp;", " ").strip()


def parse_positions_from_html(html: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for tr in _TR_RE.finditer(html):
        cells = [_strip(td.group(1)) for td in _TD_RE.finditer(tr.group(1))]
        if len(cells) == 14 and _DATE_RE.match(cells[0]) and _DATE_RE.match(cells[9]):
            rows.append(dict(zip(COLUMNS, cells)))
    return rows


# ───────────────────────────── shared write + summary ─────────────────────────────


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
    parser.add_argument(
        "--source",
        default="auto",
        help="'mt5' (direct), 'auto' (mt5 then html fallback, default), or a path to a ReportHistory-*.html.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="MT5 mode: how many days back to fetch (default 14).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output directory (default: {DEFAULT_OUT}).",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress summary output.")
    args = parser.parse_args(argv)

    positions: list[dict[str, str]] = []
    used_source = ""

    if args.source == "mt5" or args.source == "auto":
        try:
            positions = fetch_from_mt5(args.days)
            used_source = f"MT5 terminal (last {args.days} days)"
        except RuntimeError as e:
            if args.source == "mt5":
                print(f"ERROR: {e}", file=sys.stderr)
                return 2
            print(f"MT5 direct unavailable: {e}", file=sys.stderr)
            print("Falling back to newest ReportHistory*.html on Desktop...", file=sys.stderr)

    if not positions and args.source != "mt5":
        path = Path(args.source) if args.source not in ("auto", "html") else _newest_desktop_report()
        if not path or not path.exists():
            print("No HTML source found (looked for newest ReportHistory*.html on Desktop).", file=sys.stderr)
            return 2
        positions = parse_positions_from_html(_decode_mt5_html(path))
        used_source = str(path)

    if not positions:
        print("No closed positions found in chosen source.", file=sys.stderr)
        return 1

    csv_path, json_path = _write(positions, args.out_dir)

    if not args.quiet:
        print(f"Source: {used_source}")
        print(f"Wrote:  {csv_path}")
        print(f"Wrote:  {json_path}")
        print()
        print(_summarise(positions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
