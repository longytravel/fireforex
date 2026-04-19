"""One-off Phase 5 sensitivity runner — BE off vs BE (trigger=5, offset=10).

Runs batch_evaluate on the same 800-bar synthetic fixture used by
tests/test_knob_sensitivity.py, with three configurations:

    A: BE off.
    B: BE on, trigger=5, offset=2 (normal, safe).
    C: BE on, trigger=5, offset=10 (the bug configuration).

Reports trade count, win rate, total pips for each. Expect C to produce
wildly inflated win rate because of the SL-above-price bug.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from the validation folder even though it is not on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import numpy as np
import pandas as pd
import ff_core as bc
from ff import signal_lib as sl


def _build_data():
    rng = np.random.default_rng(1234)
    n_h = 800
    n_m = n_h * 60
    drift = np.linspace(-0.02, 0.02, n_h)
    noise = rng.normal(0, 0.0015, n_h)
    h_c = 1.1 + np.cumsum(drift / n_h + noise)
    h_h = h_c + rng.uniform(0.0002, 0.0010, n_h)
    h_l = h_c - rng.uniform(0.0002, 0.0010, n_h)
    h_s = np.full(n_h, 0.0001)
    base = np.interp(np.arange(n_m), np.arange(n_h) * 60, h_c)
    m_c = base + rng.normal(0, 0.0002, n_m)
    m_h = m_c + rng.uniform(0.0001, 0.0003, n_m)
    m_l = m_c - rng.uniform(0.0001, 0.0003, n_m)
    m_s = np.full(n_m, 0.0001)
    map_start = (np.arange(n_h) * 60).astype(np.int64)
    map_end = ((np.arange(n_h) + 1) * 60).astype(np.int64)
    idx = pd.date_range("2020-01-01", periods=n_h, freq="1h", tz="UTC")
    df = pd.DataFrame({"high": h_h, "low": h_l, "close": h_c}, index=idx)
    sl._ARRAYS_CACHE.clear()
    sl._ATR_CACHE.clear()
    ss = sl.ema_cross(df, fast=5, slow=20, atr_period=14, pip_value=0.0001)
    n_sig = ss.bar_index.size
    return dict(
        h_h=h_h, h_l=h_l, h_c=h_c, h_s=h_s,
        m_h=m_h, m_l=m_l, m_c=m_c, m_s=m_s,
        map_start=map_start, map_end=map_end,
        bar_index=ss.bar_index.astype(np.int64),
        direction=ss.direction.astype(np.int64),
        entry_price=ss.entry_price.astype(np.float64),
        hour=ss.hour.astype(np.int64),
        day=ss.day.astype(np.int64),
        atr_pips=ss.atr_pips.astype(np.float64),
        swing_sl=np.zeros(n_sig, dtype=np.float64),
        filter_value=np.zeros(n_sig, dtype=np.float64),
        variant=np.zeros(n_sig, dtype=np.int64),
        sig_filters=np.full((bc.NUM_SIGNAL_PARAMS, n_sig), -1, dtype=np.int64),
        n_sig=n_sig,
    )


def _row(be_enabled, trigger, offset):
    r = np.zeros(bc.NUM_PL, dtype=np.float64)
    r[bc.PL_SIGNAL_VARIANT] = 0
    r[bc.PL_SL_MODE] = 0       # fixed pips
    r[bc.PL_SL_FIXED_PIPS] = 30.0
    r[bc.PL_TP_MODE] = 2       # fixed pips
    r[bc.PL_TP_FIXED_PIPS] = 60.0
    r[bc.PL_HOURS_END] = 23
    r[bc.PL_DAYS_BITMASK] = 127
    r[bc.PL_BUY_FILTER_MAX] = -1
    r[bc.PL_SELL_FILTER_MIN] = -1
    r[bc.PL_BREAKEVEN_ENABLED] = be_enabled
    r[bc.PL_BREAKEVEN_TRIGGER] = trigger
    r[bc.PL_BREAKEVEN_OFFSET] = offset
    return r


def main():
    d = _build_data()
    configs = [
        ("A: BE off",                _row(0, 0.0, 0.0)),
        ("B: BE trig=5 offset=2",    _row(1, 5.0, 2.0)),
        ("C: BE trig=5 offset=10",   _row(1, 5.0, 10.0)),
    ]
    pm = np.stack([c[1] for c in configs])
    n_trials = pm.shape[0]
    metrics = np.zeros((n_trials, bc.NUM_METRICS), dtype=np.float64)
    pnl = np.empty((n_trials, d["n_sig"]), dtype=np.float64)
    param_layout = np.arange(bc.NUM_PL, dtype=np.int64)

    bc.batch_evaluate(
        d["h_h"], d["h_l"], d["h_c"], d["h_s"], 0.0001, 0.0,
        d["bar_index"], d["direction"], d["entry_price"],
        d["hour"], d["day"], d["atr_pips"],
        d["swing_sl"], d["filter_value"], d["variant"],
        d["sig_filters"],
        pm, param_layout,
        metrics,
        d["n_sig"], 365.0 * 24.0,
        0.0, 999.0,
        d["m_h"], d["m_l"], d["m_c"], d["m_s"],
        d["map_start"], d["map_end"],
        pnl,
    )

    print(f"{'config':32} {'trades':>8} {'wins':>6} {'win%':>6} {'total_pips':>12}")
    for i, (name, _) in enumerate(configs):
        n_trades = int(metrics[i, 0])
        row_pnl = pnl[i, :n_trades]
        wins = int((row_pnl > 0).sum())
        win_pct = 100.0 * wins / n_trades if n_trades else 0.0
        total = float(row_pnl.sum())
        print(f"{name:32} {n_trades:>8d} {wins:>6d} {win_pct:>5.1f}% {total:>12.1f}")


if __name__ == "__main__":
    main()
