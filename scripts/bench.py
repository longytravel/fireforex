"""Raw speed benchmark: how many full 50-param backtests per second on this hardware."""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fire_forex_v0 import load_ohlc, run_backtest
from fire_forex_v0.params import Params


def random_params(rng: random.Random) -> Params:
    return Params(
        ema_fast=rng.randint(5, 40),
        ema_slow=rng.randint(50, 240),
        rsi_period=rng.randint(5, 30),
        rsi_oversold=rng.uniform(15, 40),
        rsi_overbought=rng.uniform(60, 85),
        atr_period=rng.randint(5, 30),
        bb_period=rng.randint(10, 60),
        bb_std=rng.uniform(1.0, 3.5),
        macd_fast=rng.randint(5, 20),
        macd_slow=rng.randint(20, 60),
        macd_signal=rng.randint(3, 20),
        donchian_period=rng.randint(10, 120),
        keltner_mult=rng.uniform(0.8, 3.0),
        momentum_period=rng.randint(3, 30),
        momentum_threshold=rng.uniform(-0.001, 0.001),
        require_trend=rng.choice([True, False]),
        use_htf_filter=rng.choice([True, False]),
        use_trailing=rng.choice([True, False]),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=None)
    ap.add_argument("--max-rows", type=int, default=None)
    ap.add_argument("--n", type=int, default=50, help="Backtests to run")
    args = ap.parse_args()

    df = load_ohlc(path=args.data, max_rows=args.max_rows)
    print(f"benchmark data: {len(df):,} bars")

    rng = random.Random(0)
    # Warmup — lets Numba JIT compile
    run_backtest(df, random_params(rng))

    t0 = time.perf_counter()
    for _ in range(args.n):
        run_backtest(df, random_params(rng))
    elapsed = time.perf_counter() - t0

    rate = args.n / elapsed
    print(f"\nran {args.n} backtests in {elapsed:.2f}s")
    print(f"  = {rate:.1f} backtests/sec")
    print(f"  = {elapsed/args.n*1000:.0f} ms/backtest")


if __name__ == "__main__":
    main()
