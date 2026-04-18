"""Run one backtest with default params and print a metrics summary."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fire_forex_v0 import DEFAULT_PARAMS, load_ohlc, run_backtest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=None, help="Path to OHLC parquet (optional)")
    ap.add_argument("--start", default=None, help="ISO start date (e.g. 2024-01-01)")
    ap.add_argument("--end", default=None, help="ISO end date")
    ap.add_argument("--max-rows", type=int, default=None, help="Cap rows (smoke test)")
    args = ap.parse_args()

    t0 = time.perf_counter()
    df = load_ohlc(path=args.data, start=args.start, end=args.end, max_rows=args.max_rows)
    t_load = time.perf_counter() - t0
    print(f"loaded {len(df):,} bars from {df.index.min()} → {df.index.max()} in {t_load:.2f}s")

    t1 = time.perf_counter()
    res = run_backtest(df, DEFAULT_PARAMS)
    t_bt = time.perf_counter() - t1

    print(f"\nbacktest completed in {t_bt*1000:.0f} ms")
    for k, v in res.to_dict().items():
        if isinstance(v, float):
            print(f"  {k:16s} {v:>12.4f}")
        else:
            print(f"  {k:16s} {v:>12}")

    print(f"\ntotal: {t_load + t_bt:.2f}s")


if __name__ == "__main__":
    main()
