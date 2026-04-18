"""Optuna TPE sweep over all 50 parameters. Writes results to artifacts/."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fire_forex_v0 import load_ohlc
from fire_forex_v0.optimize import optimize, save_study_summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--max-rows", type=int, default=None)
    ap.add_argument("--trials", type=int, default=500)
    ap.add_argument("--jobs", type=int, default=1, help="Optuna n_jobs. 1 = sequential (safest with vbt).")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="artifacts/sweep")
    args = ap.parse_args()

    t0 = time.perf_counter()
    df = load_ohlc(path=args.data, start=args.start, end=args.end, max_rows=args.max_rows)
    print(f"loaded {len(df):,} bars in {time.perf_counter()-t0:.2f}s")

    t1 = time.perf_counter()
    result = optimize(df, n_trials=args.trials, n_jobs=args.jobs, seed=args.seed)
    elapsed = time.perf_counter() - t1

    per_trial = elapsed / max(args.trials, 1) * 1000
    print(f"\nswept {args.trials} trials in {elapsed:.1f}s  ({per_trial:.0f} ms/trial)")
    print(f"best score: {result.best_score:.4f}")
    print("best params:")
    for k, v in sorted(result.best_params.items()):
        print(f"  {k:22s} {v}")

    out = Path(args.out)
    save_study_summary(result, out)
    print(f"\nwrote {out.with_suffix('.parquet')} and {out.with_suffix('.top20.csv')}")


if __name__ == "__main__":
    main()
