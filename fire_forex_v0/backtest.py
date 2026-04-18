from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import vectorbt as vbt

from .params import Params
from .strategy import compute_signals


@dataclass
class BacktestResult:
    sharpe: float
    total_return: float
    max_drawdown: float
    win_rate: float
    trade_count: int
    profit_factor: float
    calmar: float
    score: float

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _safe_float(x, default: float = 0.0) -> float:
    try:
        v = float(x)
        if np.isnan(v) or np.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def run_backtest(df: pd.DataFrame, p: Params) -> BacktestResult:
    sig = compute_signals(df, p)
    close = df["close"]
    atr = sig["atr"].reindex(close.index)

    # ATR-based SL/TP as % of price
    sl_pct = (p.sl_atr_mult * atr / close).clip(lower=0.0001, upper=0.2)
    tp_pct = (p.tp_atr_mult * atr / close).clip(lower=0.0001, upper=0.4)
    trail_pct = (p.trail_stop_mult * atr / close).clip(lower=0.0001, upper=0.2) if p.use_trailing else None

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

    trades = pf.trades
    n_trades = int(trades.count())
    if n_trades == 0:
        return BacktestResult(0, 0, 0, 0, 0, 0, 0, score=-1e9)

    sharpe = _safe_float(pf.sharpe_ratio())
    ret = _safe_float(pf.total_return())
    dd = _safe_float(pf.max_drawdown())
    win_rate = _safe_float(trades.win_rate())
    pf_factor = _safe_float(trades.profit_factor(), default=0.0)
    calmar = _safe_float(pf.calmar_ratio())

    # Composite score — sharpe as primary, penalise very low trade count
    trade_penalty = 0.0 if n_trades >= 30 else (30 - n_trades) * 0.05
    score = sharpe - trade_penalty

    return BacktestResult(
        sharpe=sharpe,
        total_return=ret,
        max_drawdown=dd,
        win_rate=win_rate,
        trade_count=n_trades,
        profit_factor=pf_factor,
        calmar=calmar,
        score=score,
    )
