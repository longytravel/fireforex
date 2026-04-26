"""Bulk-fetch MT5 tick history for the 28 default pairs.

Append-only — if a pair already has a partial parquet, the downloader
picks up from the last timestamp. One failure per pair does not abort
the rest.

Usage:
    .\\.venv\\Scripts\\python.exe scripts/fetch_mt5_ticks.py [--days 90] [--pairs EUR_USD,GBP_USD]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ff.data.mt5_tick_downloader import download  # noqa: E402

DEFAULT_PAIRS = [
    "AUD_CAD",
    "AUD_CHF",
    "AUD_JPY",
    "AUD_NZD",
    "AUD_USD",
    "CAD_CHF",
    "CAD_JPY",
    "CHF_JPY",
    "EUR_AUD",
    "EUR_CAD",
    "EUR_CHF",
    "EUR_GBP",
    "EUR_JPY",
    "EUR_NZD",
    "EUR_USD",
    "GBP_AUD",
    "GBP_CAD",
    "GBP_CHF",
    "GBP_JPY",
    "GBP_NZD",
    "GBP_USD",
    "NZD_CAD",
    "NZD_CHF",
    "NZD_JPY",
    "NZD_USD",
    "USD_CAD",
    "USD_CHF",
    "USD_JPY",
]


def _print(msg: str) -> None:
    print(msg, flush=True)


def _positive_days(value: str) -> int:
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("--days must be > 0")
    return n


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser()
    p.add_argument(
        "--days",
        type=_positive_days,
        default=90,
        help="how many days of history to ensure on disk (default: 90)",
    )
    p.add_argument(
        "--pairs",
        default=",".join(DEFAULT_PAIRS),
        help="comma-separated pair list (default: 28 standard pairs)",
    )
    args = p.parse_args()

    pairs = [s.strip() for s in args.pairs.split(",") if s.strip()]
    end = date.today()
    start = end - timedelta(days=args.days)

    _print(f"[fetch_mt5_ticks] window {start.isoformat()} -> {end.isoformat()} ({args.days} days)")
    _print(f"[fetch_mt5_ticks] {len(pairs)} pairs queued")

    successes = 0
    skipped: list[tuple[str, str]] = []

    for i, pair in enumerate(pairs, start=1):
        _print(f"[{i}/{len(pairs)}] {pair}")
        try:
            result = download(pair, start, end, log_cb=_print)
            new_ticks = result["new_ticks"]
            total_ticks = result["total_ticks"]
            _print(f"  OK {pair}: +{new_ticks:,} new, {total_ticks:,} total")
            successes += 1
        except Exception as e:
            _print(f"  FAIL {pair}: {e}")
            skipped.append((pair, str(e)))

    _print("")
    _print(f"[fetch_mt5_ticks] done: {successes}/{len(pairs)} succeeded")
    if skipped:
        _print(f"[fetch_mt5_ticks] skipped {len(skipped)} pairs:")
        for pair, reason in skipped:
            _print(f"  - {pair}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
