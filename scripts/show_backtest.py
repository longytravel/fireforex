"""Run a backtest and open an interactive visualization in your browser.

Opens 3 plots: equity curve + trades, drawdown, trade PnL distribution.
"""
from __future__ import annotations

import argparse
import sys
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import vectorbt as vbt

from fire_forex_v0 import DEFAULT_PARAMS, load_ohlc
from fire_forex_v0.strategy import compute_signals


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=None)
    ap.add_argument("--start", default=None, help="ISO start (e.g. 2024-01-01)")
    ap.add_argument("--end", default=None)
    ap.add_argument("--max-rows", type=int, default=200_000,
                    help="Bars to plot (default 200k; plotly struggles above 500k)")
    ap.add_argument("--out", default="artifacts/backtest.html")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    t0 = time.perf_counter()
    df = load_ohlc(path=args.data, start=args.start, end=args.end, max_rows=args.max_rows)
    print(f"loaded {len(df):,} bars from {df.index.min()} → {df.index.max()} "
          f"in {time.perf_counter()-t0:.2f}s")

    p = DEFAULT_PARAMS
    sig = compute_signals(df, p)
    close = df["close"]
    atr = sig["atr"].reindex(close.index)
    sl_pct = (p.sl_atr_mult * atr / close).clip(lower=0.0001, upper=0.2)
    tp_pct = (p.tp_atr_mult * atr / close).clip(lower=0.0001, upper=0.4)

    pf = vbt.Portfolio.from_signals(
        close=close,
        entries=sig["entries"],
        exits=sig["exits"],
        short_entries=sig["short_entries"],
        short_exits=sig["short_exits"],
        init_cash=p.initial_cash,
        fees=p.fee_pct,
        slippage=p.slippage_pct,
        sl_stop=sl_pct,
        tp_stop=tp_pct,
        sl_trail=p.use_trailing,
        size=p.risk_pct,
        size_type="percent",
        freq="1T",
    )

    n_trades = int(pf.trades.count())
    print(f"\ntrades: {n_trades}  total_return: {float(pf.total_return()):.4f}  "
          f"sharpe: {float(pf.sharpe_ratio()):.3f}  "
          f"max_dd: {float(pf.max_drawdown()):.4f}")

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    # vbt.Portfolio.plot() returns a plotly Figure combining price + trades + equity
    fig = pf.plot(subplots=["orders", "trade_pnl", "cum_returns", "drawdowns"])
    fig.write_html(str(out), include_plotlyjs="cdn")
    print(f"\nwrote {out}")

    if not args.no_open:
        webbrowser.open(out.as_uri())
        print("opened in browser")


if __name__ == "__main__":
    main()
