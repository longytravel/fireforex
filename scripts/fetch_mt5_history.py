"""Fetch MT5 M1 history to ``BackTestData_MT5/{pair}_M1.parquet`` and fan
out to higher TFs via ``ff.data.resample.derive_higher_tfs``.

Mirrors ``scripts/fetch_dukascopy.py`` in spirit — one pair, date range,
writes parquet, prints bar counts. Windows-only (MT5 constraint).

Usage:
    python scripts/fetch_mt5_history.py --pair EUR_USD --days 30
    python scripts/fetch_mt5_history.py --pair EUR_USD --start 2026-03-01 --end 2026-04-22
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Force UTF-8 stdout — reconcile script pattern, Windows cp1252 default
# would choke on the arrows used below.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ff.data import mt5_m1_downloader  # noqa: E402
from ff.data import resample  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pair", required=True,
                   help="Fire Forex pair symbol, e.g. EUR_USD")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--days", type=int,
                   help="Fetch last N days (ending today UTC)")
    g.add_argument("--start", type=str,
                   help="Start date YYYY-MM-DD (use with --end)")
    p.add_argument("--end", type=str,
                   help="End date YYYY-MM-DD (defaults to today UTC)")
    p.add_argument("--no-append", action="store_true",
                   help="Overwrite existing parquet instead of merging")
    p.add_argument("--no-resample", action="store_true",
                   help="Skip higher-TF fan-out (M5/M15/M30/H1/H4/D)")
    return p.parse_args()


def _resolve_window(args: argparse.Namespace) -> tuple[date, date]:
    today = datetime.now(timezone.utc).date()
    if args.days:
        return today - timedelta(days=args.days), today
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else today
    if end < start:
        raise ValueError(f"end ({end}) before start ({start})")
    return start, end


def main() -> int:
    args = _parse_args()
    start, end = _resolve_window(args)

    print(f"Fire Forex · MT5 fetch · {args.pair}  {start} → {end}")
    print(f"  root: {mt5_m1_downloader.MT5_DATA_ROOT}")

    result = mt5_m1_downloader.download(
        args.pair, start, end,
        append=not args.no_append,
        log_cb=lambda m: print(f"  {m}"),
    )
    print(f"  wrote {result['total_bars']:,} bars  "
          f"(+{result['new_bars']:,} new)  → {result['path']}")
    if result["start_ts"] and result["end_ts"]:
        print(f"  window {result['start_ts']} → {result['end_ts']}")

    if args.no_resample or result["total_bars"] == 0:
        return 0

    print(f"[resample] deriving higher TFs from M1 under "
          f"{mt5_m1_downloader.MT5_DATA_ROOT.name}")
    resample.derive_higher_tfs(
        args.pair,
        source_tf="M1",
        root=mt5_m1_downloader.MT5_DATA_ROOT,
    )
    print("[done]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
