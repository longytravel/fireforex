# Fire Forex v0 — Core Loop

Four verbs, nothing else: **load → strategy → backtest → optimize**.

Built on VectorBT + Optuna. No Rust. No MT5. No dashboards. Proves the core loop is fast enough before any other layer goes on top.

## Hardware target

- Intel i9-14900HX (32 threads), 64 GB RAM
- Windows 11, Python 3.12

## Data

The loader looks for EUR/USD M1 Parquet in this order:

1. `G:\My Drive\BackTestData\EUR_USD_M1.parquet` (multi-year, ~93 MB)
2. `G:\My Drive\ForexPipeline\parquet\EURUSD_2025_full\v1\market-data.parquet`
3. `G:\My Drive\ForexPipeline\EURUSD_M1_chunks\EURUSD_M1_2024.parquet`

Override with `--data <path>`.

## Setup

```powershell
cd "C:\Users\ROG\Projects\Fire Forex\v0"
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```powershell
# Single backtest with default 50-param set
python scripts/run_single.py

# Optuna sweep (1000 trials, 50-param Bayesian search)
python scripts/run_sweep.py --trials 1000 --jobs 16

# Raw speed benchmark (how many backtests/sec on your laptop)
python scripts/bench.py --n 100
```

## What's in the strategy

A 50-parameter EA combining:

- **Entry signals (15 params):** EMA cross, RSI, BBANDS, MACD, Donchian breakout, Keltner, momentum filter
- **Filters (12):** session (London/NY/Asian), volatility band, trend alignment, day-of-week
- **Risk/sizing (10):** ATR-based SL/TP, trailing, breakeven, spread/fee/slippage modeling
- **Exits (8):** time-based, reverse signal, RSI extreme, session close
- **Meta (5):** signal confirmation bars, trade spacing, higher-timeframe filter

See `fire_forex_v0/params.py` for the full parameter table and Optuna search ranges.

## Acceptance test

1. `run_single.py` completes in under **5 seconds** (load + one backtest on 2 M rows)
2. `run_sweep.py --trials 1000` completes in under **5 minutes** (Optuna TPE on 50 dims)
3. `bench.py --n 100` reports **≥ 20 backtests/sec** on the target hardware

If all three pass, the core loop is fast enough. Everything else (MT5, reconciliation, validation gauntlet, dashboard) bolts on top of this foundation.
