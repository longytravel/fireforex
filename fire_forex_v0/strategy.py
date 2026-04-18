from __future__ import annotations

import numpy as np
import pandas as pd
import vectorbt as vbt

from .params import Params


# Returns a dict of precomputed series keyed by what the signal combiner needs.
# Recomputing per-trial is fine; the cost is dominated by the backtest engine.
def _indicators(df: pd.DataFrame, p: Params) -> dict[str, pd.Series]:
    close = df["close"]
    high = df["high"]
    low = df["low"]

    # Guard against degenerate periods (Optuna can propose fast >= slow)
    ema_f = vbt.MA.run(close, window=max(p.ema_fast, 2), ewm=True).ma
    ema_s = vbt.MA.run(close, window=max(p.ema_slow, p.ema_fast + 1), ewm=True).ma
    trend = vbt.MA.run(close, window=max(p.trend_ema, 2), ewm=True).ma
    htf = vbt.MA.run(close, window=max(p.htf_ema, 2), ewm=True).ma

    rsi = vbt.RSI.run(close, window=max(p.rsi_period, 2)).rsi
    atr = vbt.ATR.run(high, low, close, window=max(p.atr_period, 2)).atr
    bb = vbt.BBANDS.run(close, window=max(p.bb_period, 2), alpha=p.bb_std)
    macd = vbt.MACD.run(
        close,
        fast_window=max(p.macd_fast, 2),
        slow_window=max(p.macd_slow, p.macd_fast + 1),
        signal_window=max(p.macd_signal, 2),
    )

    # Donchian breakout channels
    donch_high = high.rolling(max(p.donchian_period, 2)).max()
    donch_low = low.rolling(max(p.donchian_period, 2)).min()

    # Keltner channels (EMA ± mult*ATR)
    kelt_up = ema_s + p.keltner_mult * atr
    kelt_dn = ema_s - p.keltner_mult * atr

    # Momentum (pct change over N bars)
    mom = close.pct_change(max(p.momentum_period, 1))

    return dict(
        ema_f=ema_f, ema_s=ema_s, trend=trend, htf=htf,
        rsi=rsi, atr=atr,
        bb_upper=bb.upper, bb_lower=bb.lower,
        macd_line=macd.macd, macd_sig=macd.signal,
        donch_high=donch_high, donch_low=donch_low,
        kelt_up=kelt_up, kelt_dn=kelt_dn,
        mom=mom,
    )


def _time_masks(index: pd.DatetimeIndex, p: Params) -> dict[str, np.ndarray]:
    hour = index.hour.to_numpy()
    dow = index.dayofweek.to_numpy()

    session = np.zeros(len(index), dtype=bool)
    if p.session_london:
        session |= (hour >= 7) & (hour < 16)
    if p.session_ny:
        session |= (hour >= 12) & (hour < 21)
    if p.session_asian:
        session |= ((hour >= 22) | (hour < 7))

    day = np.zeros(len(index), dtype=bool)
    if p.day_mon: day |= dow == 0
    if p.day_tue: day |= dow == 1
    if p.day_wed: day |= dow == 2
    if p.day_thu: day |= dow == 3
    if p.day_fri: day |= dow == 4

    # Session close = last bar of an hour window we care about (simple end-of-london marker)
    session_close = np.zeros(len(index), dtype=bool)
    if p.exit_end_of_session:
        session_close = (hour == 20) & (index.minute.to_numpy() >= 55)

    return dict(session=session, day=day, session_close=session_close)


def compute_signals(df: pd.DataFrame, p: Params) -> dict[str, pd.Series]:
    """Return `entries`, `exits`, `short_entries`, `short_exits`, and `atr` aligned to df.index."""
    ind = _indicators(df, p)
    masks = _time_masks(df.index, p)

    close = df["close"]

    # Base long signal: EMA crossover with confirmation + MACD bull + above BB midband
    ema_cross_up = (ind["ema_f"] > ind["ema_s"]) & (ind["ema_f"].shift(1) <= ind["ema_s"].shift(1))
    ema_cross_dn = (ind["ema_f"] < ind["ema_s"]) & (ind["ema_f"].shift(1) >= ind["ema_s"].shift(1))

    macd_bull = ind["macd_line"] > ind["macd_sig"]
    macd_bear = ind["macd_line"] < ind["macd_sig"]

    donch_break_up = close > ind["donch_high"].shift(1)
    donch_break_dn = close < ind["donch_low"].shift(1)

    kelt_ok_long = close > ind["kelt_dn"]
    kelt_ok_short = close < ind["kelt_up"]

    mom_ok_long = ind["mom"] > p.momentum_threshold
    mom_ok_short = ind["mom"] < -p.momentum_threshold

    rsi_long_ok = ind["rsi"] < p.rsi_overbought
    rsi_short_ok = ind["rsi"] > p.rsi_oversold

    atr_pct = ind["atr"] / close
    vol_ok = (atr_pct >= p.min_atr_pct) & (atr_pct <= p.max_atr_pct)

    trend_long = close > ind["trend"] if p.require_trend else pd.Series(True, index=close.index)
    trend_short = close < ind["trend"] if p.require_trend else pd.Series(True, index=close.index)

    if p.use_htf_filter:
        htf_long = close > ind["htf"].shift(p.htf_bars_back)
        htf_short = close < ind["htf"].shift(p.htf_bars_back)
    else:
        htf_long = pd.Series(True, index=close.index)
        htf_short = pd.Series(True, index=close.index)

    session = pd.Series(masks["session"], index=close.index)
    day = pd.Series(masks["day"], index=close.index)

    raw_long = (
        (ema_cross_up | donch_break_up)
        & macd_bull & kelt_ok_long & mom_ok_long
        & rsi_long_ok & vol_ok & trend_long & htf_long
        & session & day
    )
    raw_short = (
        (ema_cross_dn | donch_break_dn)
        & macd_bear & kelt_ok_short & mom_ok_short
        & rsi_short_ok & vol_ok & trend_short & htf_short
        & session & day
    )

    # Confirmation: require N consecutive bars of raw signal
    if p.confirm_bars > 1:
        raw_long = raw_long.rolling(p.confirm_bars).sum() >= p.confirm_bars
        raw_short = raw_short.rolling(p.confirm_bars).sum() >= p.confirm_bars

    # Min bars between trades: gate subsequent signals. Simple approximation: mask first signal per N-bar window.
    if p.min_bars_between > 1:
        def space(sig: pd.Series) -> pd.Series:
            arr = sig.to_numpy()
            last = -10**9
            out = np.zeros_like(arr)
            gap = p.min_bars_between
            for i in range(len(arr)):
                if arr[i] and (i - last) >= gap:
                    out[i] = True
                    last = i
            return pd.Series(out.astype(bool), index=sig.index)
        raw_long = space(raw_long)
        raw_short = space(raw_short)

    # Exits (indicator-based; SL/TP/trail handled by vbt.Portfolio.from_signals)
    long_exit = pd.Series(False, index=close.index)
    short_exit = pd.Series(False, index=close.index)

    if p.reverse_exit:
        long_exit |= raw_short
        short_exit |= raw_long
    if p.exit_on_rsi_extreme:
        long_exit |= ind["rsi"] > p.rsi_exit_long
        short_exit |= ind["rsi"] < p.rsi_exit_short
    if p.exit_end_of_session:
        sc = pd.Series(masks["session_close"], index=close.index)
        long_exit |= sc
        short_exit |= sc

    return dict(
        entries=raw_long.fillna(False),
        exits=long_exit.fillna(False),
        short_entries=raw_short.fillna(False),
        short_exits=short_exit.fillna(False),
        atr=ind["atr"],
    )
