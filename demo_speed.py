"""Fire Forex — multi-timeframe speed demo (H1 entries, M1 sub-bar fills).

20 years of EUR/USD. Signals generated on H1 (slower TF → fewer whipsaws).
SL/TP/trail management walks M1 sub-bars inside each H1 bar (realistic fills).

Same Rust engine (`ff_core` from `core/`), same 500-variant sweep.

Run:
    .\\.venv\\Scripts\\python.exe demo_speed.py
"""
from __future__ import annotations

import sys
import time
import webbrowser
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd

import ff_core as bc

# ── Layer identity (change these when swapping optimisers) ───────────
LAYER_NAME = "baseline_random"       # unique label for this run in history.csv
OPTIMIZER = "random"                 # "random" | "optuna" | "catcma"
SEED = 42                            # fixed for reproducibility (same test, same data)

# ── Configuration ────────────────────────────────────────────────────
DATA_H1 = Path(r"G:\My Drive\BackTestData\EUR_USD_H1.parquet")
DATA_M1 = Path(r"G:\My Drive\BackTestData\EUR_USD_M1.parquet")
ART = Path(__file__).resolve().parent / "artifacts"
OUT = ART / "demo_speed.html"
HISTORY_CSV = ART / "history.csv"
COMPARISON_HTML = ART / "comparison.html"
RUNS_DIR = ART / "runs"

# Entry signal (H1 timeframe) — DO NOT change between layers
EMA_FAST = 12
EMA_SLOW = 50
ATR_PERIOD = 14

# Sweep — DO NOT change between layers (same test)
N_TRIALS = 500
SL_ATR_RANGE = (0.8, 4.0)
TP_RR_RANGE = (0.8, 4.0)

# Execution
PIP_VALUE_EURUSD = 0.0001
COMMISSION_PIPS = 0.3
MAX_SPREAD_PIPS = 10.0
SLIPPAGE_PIPS = 0.0
BARS_PER_YEAR_H1 = 24 * 252          # trading hours per year (for Sharpe)


# ── Helpers ──────────────────────────────────────────────────────────
def ewm(arr: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, arr.size):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def atr_ema(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    return ewm(tr, period)


def load_parquet(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df.columns = [c.lower() for c in df.columns]
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    df = df.sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def build_h1_to_m1_mapping(h1_index: pd.DatetimeIndex,
                           m1_index: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """For each H1 bar, find the M1 bar index range it covers.

    Returns two int64 arrays of length n_h1:
        start[i] = M1 index of first bar at or after H1 bar i's start
        end[i]   = M1 index of first bar at or after H1 bar i+1's start
    """
    h1_ns = h1_index.asi8  # int64 ns timestamps
    m1_ns = m1_index.asi8
    one_hour_ns = int(3600 * 1_000_000_000)

    start = np.searchsorted(m1_ns, h1_ns, side="left").astype(np.int64)
    end = np.searchsorted(m1_ns, h1_ns + one_hour_ns, side="left").astype(np.int64)
    return start, end


# ── Signal generation (on H1) ────────────────────────────────────────
def generate_h1_signals(h1: pd.DataFrame) -> dict[str, np.ndarray]:
    h = h1["high"].to_numpy(dtype=np.float64, copy=True)
    l = h1["low"].to_numpy(dtype=np.float64, copy=True)
    c = h1["close"].to_numpy(dtype=np.float64, copy=True)

    ef = ewm(c, EMA_FAST)
    es = ewm(c, EMA_SLOW)

    up = (ef[1:] > es[1:]) & (ef[:-1] <= es[:-1])
    dn = (ef[1:] < es[1:]) & (ef[:-1] >= es[:-1])

    bars_up = np.where(up)[0] + 1
    bars_dn = np.where(dn)[0] + 1

    bars = np.concatenate([bars_up, bars_dn])
    dirs = np.concatenate([
        np.ones(bars_up.size, dtype=np.int64),
        -np.ones(bars_dn.size, dtype=np.int64),
    ])
    order = np.argsort(bars)
    bars = bars[order].astype(np.int64)
    dirs = dirs[order]

    atr_arr = atr_ema(h, l, c, ATR_PERIOD)
    atr_pips = (atr_arr[bars] / PIP_VALUE_EURUSD).astype(np.float64)

    return {
        "bar_index": bars,
        "direction": dirs,
        "entry_price": c[bars].astype(np.float64),
        "hour": h1.index.hour.to_numpy()[bars].astype(np.int64),
        "day": h1.index.dayofweek.to_numpy()[bars].astype(np.int64),
        "atr_pips": atr_pips,
        "swing_sl": np.zeros(bars.size, dtype=np.float64),
        "filter_value": np.zeros(bars.size, dtype=np.float64),
        "variant": np.zeros(bars.size, dtype=np.int64),
    }


# ── Param matrix ─────────────────────────────────────────────────────
def build_param_matrix(n_trials: int, seed: int = SEED) -> tuple[np.ndarray, np.ndarray]:
    """Full-width (N, NUM_PL) float64 matrix.

    Fixed: SL_MODE=ATR, TP_MODE=RR, DAYS=Mon-Fri, HOURS=24/7.
    Tuned: SL_ATR_MULT, TP_RR_RATIO.
    """
    rng = np.random.default_rng(seed)
    pm = np.zeros((n_trials, bc.NUM_PL), dtype=np.float64)
    pm[:, bc.PL_SL_MODE] = 1.0                                     # ATR
    pm[:, bc.PL_SL_ATR_MULT] = rng.uniform(*SL_ATR_RANGE, n_trials)
    pm[:, bc.PL_TP_MODE] = 0.0                                     # RR
    pm[:, bc.PL_TP_RR_RATIO] = rng.uniform(*TP_RR_RANGE, n_trials)
    pm[:, bc.PL_DAYS_BITMASK] = 31.0
    pm[:, bc.PL_HOURS_START] = 0.0
    pm[:, bc.PL_HOURS_END] = 23.0
    layout = np.arange(bc.NUM_PL, dtype=np.int64)
    return pm, layout


# ── Main ──────────────────────────────────────────────────────────────
def main() -> None:
    t_total = time.perf_counter()

    # 1. Load both timeframes
    print(f"[load] H1 from {DATA_H1}")
    t = time.perf_counter()
    h1 = load_parquet(DATA_H1)
    print(f"       {len(h1):,} bars  {h1.index.min()} → {h1.index.max()}  "
          f"in {time.perf_counter()-t:.2f}s")

    print(f"[load] M1 from {DATA_M1}")
    t = time.perf_counter()
    m1 = load_parquet(DATA_M1)
    print(f"       {len(m1):,} bars  {m1.index.min()} → {m1.index.max()}  "
          f"in {time.perf_counter()-t:.2f}s")

    # 2. Align windows (both should cover ~2007-2026; intersect to be safe)
    common_start = max(h1.index.min(), m1.index.min())
    common_end = min(h1.index.max(), m1.index.max())
    h1 = h1.loc[common_start:common_end]
    m1 = m1.loc[common_start:common_end]
    print(f"[align] shared window {common_start} → {common_end}  "
          f"H1={len(h1):,} · M1={len(m1):,}")

    # 3. H1 → M1 mapping
    t = time.perf_counter()
    h1_to_sub_start, h1_to_sub_end = build_h1_to_m1_mapping(h1.index, m1.index)
    print(f"[map]  H1→M1 built in {time.perf_counter()-t:.2f}s")

    # 4. H1 arrays (main bars for Rust engine)
    h_h = h1["high"].to_numpy(dtype=np.float64, copy=True)
    h_l = h1["low"].to_numpy(dtype=np.float64, copy=True)
    h_c = h1["close"].to_numpy(dtype=np.float64, copy=True)
    h_s = (h1["spread"].to_numpy(dtype=np.float64, copy=True)
           if "spread" in h1.columns
           else np.full(len(h1), PIP_VALUE_EURUSD, dtype=np.float64))

    # M1 arrays (sub-bars for SL/TP/trail checks)
    m_h = m1["high"].to_numpy(dtype=np.float64, copy=True)
    m_l = m1["low"].to_numpy(dtype=np.float64, copy=True)
    m_c = m1["close"].to_numpy(dtype=np.float64, copy=True)
    m_s = (m1["spread"].to_numpy(dtype=np.float64, copy=True)
           if "spread" in m1.columns
           else np.full(len(m1), PIP_VALUE_EURUSD, dtype=np.float64))

    # 5. Signals on H1
    t = time.perf_counter()
    sig = generate_h1_signals(h1)
    n_signals = sig["bar_index"].size
    print(f"[signals] {n_signals:,} H1 EMA({EMA_FAST},{EMA_SLOW}) crosses  "
          f"in {time.perf_counter()-t:.2f}s")

    # 6. Params + buffers
    param_matrix, param_layout = build_param_matrix(N_TRIALS)
    print(f"[params] {N_TRIALS} (sl_atr, tp_rr) combos")

    metrics_out = np.zeros((N_TRIALS, bc.NUM_METRICS), dtype=np.float64)
    max_trades = int(n_signals)
    pnl_buffers = np.empty((N_TRIALS, max_trades), dtype=np.float64)

    # 7. Warm call (1 trial to pay any one-time cost)
    bc.batch_evaluate(
        h_h, h_l, h_c, h_s,
        PIP_VALUE_EURUSD, SLIPPAGE_PIPS,
        sig["bar_index"][:1], sig["direction"][:1], sig["entry_price"][:1],
        sig["hour"][:1], sig["day"][:1], sig["atr_pips"][:1],
        sig["swing_sl"][:1], sig["filter_value"][:1], sig["variant"][:1],
        param_matrix[:1], param_layout, bc.EXEC_BASIC,
        np.zeros((1, bc.NUM_METRICS), dtype=np.float64),
        1, BARS_PER_YEAR_H1, COMMISSION_PIPS, MAX_SPREAD_PIPS,
        m_h, m_l, m_c, m_s,
        h1_to_sub_start, h1_to_sub_end,
        np.empty((1, 1), dtype=np.float64),
    )

    # 8. Real sweep — multi-TF: H1 main + M1 sub-bar fills
    print(f"[sweep] Rust batch_evaluate — {N_TRIALS} × {n_signals:,} signals  "
          f"(H1 entries, M1 sub-bar SL/TP)…")
    t = time.perf_counter()
    bc.batch_evaluate(
        h_h, h_l, h_c, h_s,
        PIP_VALUE_EURUSD, SLIPPAGE_PIPS,
        sig["bar_index"], sig["direction"], sig["entry_price"],
        sig["hour"], sig["day"], sig["atr_pips"],
        sig["swing_sl"], sig["filter_value"], sig["variant"],
        param_matrix, param_layout, bc.EXEC_BASIC,
        metrics_out,
        max_trades, BARS_PER_YEAR_H1, COMMISSION_PIPS, MAX_SPREAD_PIPS,
        m_h, m_l, m_c, m_s,
        h1_to_sub_start, h1_to_sub_end,
        pnl_buffers,
    )
    elapsed = time.perf_counter() - t
    rate = N_TRIALS / elapsed
    print(f"        done in {elapsed:.3f}s  →  {rate:,.0f} evals/sec")

    # 9. Rank best variant & compute the 8 numbers
    quality = metrics_out[:, 9]
    best = int(np.argmax(quality))
    running_best = np.maximum.accumulate(quality)

    n_trades_best = int(metrics_out[best, 0])
    pnl_best = pnl_buffers[best, :n_trades_best].copy()
    total_pips = float(pnl_best.sum())
    expectancy_pips = total_pips / n_trades_best if n_trades_best else 0.0
    equity_curve = np.cumsum(pnl_best)

    total_runtime_s = time.perf_counter() - t_total

    # ── The 8 numbers (human-readable summary) ───────────────────────
    print(f"\n┌── Fire Forex · {LAYER_NAME} · {OPTIMIZER} · N={N_TRIALS} ──────")
    print( "│ SPEED")
    print(f"│   backtests/sec  : {rate:>12,.0f}")
    print(f"│   total runtime  : {total_runtime_s:>12.2f} s")
    print( "│ ACTIVITY")
    print(f"│   trades (best)  : {n_trades_best:>12,}")
    print(f"│   win rate       : {metrics_out[best, 1]*100 if metrics_out[best, 1] <= 1 else metrics_out[best, 1]:>12.2f} %")
    print( "│ MONEY")
    print(f"│   total pips     : {total_pips:>+12,.0f}")
    print(f"│   expectancy     : {expectancy_pips:>+12.2f} pips/trade")
    print( "│ RISK")
    print(f"│   max drawdown   : {metrics_out[best, 5]:>12.2f} %")
    print(f"│   profit factor  : {metrics_out[best, 2]:>12.3f}")
    print( "│ best params")
    print(f"│   sl_atr_mult    : {param_matrix[best, bc.PL_SL_ATR_MULT]:>12.3f}")
    print(f"│   tp_rr_ratio    : {param_matrix[best, bc.PL_TP_RR_RATIO]:>12.3f}")
    print( "└──────────────────────────────────────────────────────────\n")

    # ── Persist this run ─────────────────────────────────────────────
    ART.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_file = RUNS_DIR / f"{LAYER_NAME}_{stamp}.npz"
    np.savez_compressed(
        run_file,
        quality=quality,
        running_best=running_best,
        pnl=pnl_best,
        equity=equity_curve,
        sl_atr=param_matrix[:, bc.PL_SL_ATR_MULT],
        tp_rr=param_matrix[:, bc.PL_TP_RR_RATIO],
    )
    print(f"[save] run data → {run_file.name}")

    # Append to history.csv (create with header if needed)
    row = {
        "datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
        "layer": LAYER_NAME,
        "optimizer": OPTIMIZER,
        "seed": SEED,
        "n_trials": N_TRIALS,
        "n_signals": n_signals,
        "bt_per_sec": round(rate, 0),
        "runtime_s": round(total_runtime_s, 2),
        "trades": n_trades_best,
        "win_rate_pct": round(float(metrics_out[best, 1]) * (100 if metrics_out[best, 1] <= 1 else 1), 3),
        "total_pips": round(total_pips, 1),
        "expectancy_pips": round(expectancy_pips, 3),
        "max_dd_pct": round(float(metrics_out[best, 5]), 3),
        "profit_factor": round(float(metrics_out[best, 2]), 4),
        "sharpe": round(float(metrics_out[best, 3]), 4),
        "return_pct": round(float(metrics_out[best, 6]), 2),
        "quality": round(float(metrics_out[best, 9]), 4),
        "sl_atr_mult": round(float(param_matrix[best, bc.PL_SL_ATR_MULT]), 3),
        "tp_rr_ratio": round(float(param_matrix[best, bc.PL_TP_RR_RATIO]), 3),
        "run_file": run_file.name,
    }
    if HISTORY_CSV.exists():
        hist = pd.read_csv(HISTORY_CSV)
        hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    else:
        hist = pd.DataFrame([row])
    hist.to_csv(HISTORY_CSV, index=False)
    print(f"[save] appended row → {HISTORY_CSV.name}  (now {len(hist)} runs)")

    # ── Per-run HTML (this run only) ─────────────────────────────────
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=2, cols=1,
        subplot_titles=("Quality per trial (red = best-so-far)",
                        "SL_ATR × TP_RR coloured by quality"),
        vertical_spacing=0.15,
    )
    trial_idx = np.arange(N_TRIALS)
    fig.add_trace(
        go.Scatter(x=trial_idx, y=quality, mode="markers", name="trial quality",
                   marker=dict(size=5, color=quality, colorscale="Viridis", showscale=True)),
        row=1, col=1)
    fig.add_trace(
        go.Scatter(x=trial_idx, y=running_best, mode="lines",
                   name="best so far", line=dict(color="red", width=2)),
        row=1, col=1)
    fig.add_trace(
        go.Scatter(x=param_matrix[:, bc.PL_SL_ATR_MULT],
                   y=param_matrix[:, bc.PL_TP_RR_RATIO],
                   mode="markers",
                   marker=dict(size=8, color=quality, colorscale="Viridis"),
                   text=[f"q={q:.3f} sh={s:.2f} ret={r:.1f}% tr={int(t)}"
                         for q, s, r, t in zip(quality, metrics_out[:, 3],
                                               metrics_out[:, 6], metrics_out[:, 0])],
                   name="params"),
        row=2, col=1)
    fig.update_xaxes(title_text="SL_ATR_MULT", row=2, col=1)
    fig.update_yaxes(title_text="TP_RR_RATIO", row=2, col=1)
    fig.update_layout(
        height=800,
        title=f"Fire Forex · {LAYER_NAME} · {OPTIMIZER} · "
              f"{N_TRIALS} variants in {elapsed:.2f}s ({rate:,.0f} evals/sec)",
    )
    fig.write_html(str(OUT), include_plotlyjs="cdn")
    print(f"[viz] wrote {OUT.name}")

    # ── Comparison HTML (all runs, layer-by-layer) ───────────────────
    build_comparison_html(hist)
    print(f"[viz] wrote {COMPARISON_HTML.name}")

    webbrowser.open(COMPARISON_HTML.as_uri())
    print(f"\n[total] {total_runtime_s:.2f}s end-to-end")


def build_comparison_html(hist: pd.DataFrame) -> None:
    """Single HTML dashboard comparing every layer in history.csv.

    Shows:
      1. Summary table (all 8 numbers, newest last, deltas vs baseline)
      2. Bars per metric across layers
      3. Equity curves overlaid (one line per layer)
      4. Running-best quality overlaid (shows optimiser smartness)
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    # Load each layer's saved run
    runs: dict[str, dict] = {}
    for _, r in hist.iterrows():
        p = RUNS_DIR / r["run_file"]
        if p.exists():
            with np.load(p) as z:
                runs[r["layer"]] = {k: z[k].copy() for k in z.files}

    fig = make_subplots(
        rows=4, cols=1,
        row_heights=[0.28, 0.24, 0.24, 0.24],
        subplot_titles=(
            "All runs — the 8 numbers (newest at bottom)",
            "Running best quality (x = trial number · lines that climb = smart optimiser)",
            "Equity curve of each layer's best variant (pips, cumulative)",
            "Speed — backtests per second",
        ),
        specs=[[{"type": "table"}], [{"type": "scatter"}],
               [{"type": "scatter"}], [{"type": "bar"}]],
        vertical_spacing=0.07,
    )

    table_cols = ["layer", "optimizer", "bt_per_sec", "runtime_s",
                  "trades", "win_rate_pct", "total_pips", "expectancy_pips",
                  "max_dd_pct", "profit_factor"]
    fig.add_trace(go.Table(
        header=dict(values=table_cols, fill_color="#222", font=dict(color="white")),
        cells=dict(values=[hist[c].tolist() for c in table_cols],
                   align="right")),
        row=1, col=1)

    palette = ["#e63946", "#2a9d8f", "#e9c46a", "#264653", "#f4a261", "#9b5de5"]
    for i, (layer, data) in enumerate(runs.items()):
        color = palette[i % len(palette)]
        fig.add_trace(go.Scatter(
            x=np.arange(len(data["running_best"])),
            y=data["running_best"], mode="lines", name=layer,
            legendgroup=layer, line=dict(color=color, width=2)),
            row=2, col=1)
        fig.add_trace(go.Scatter(
            x=np.arange(len(data["equity"])),
            y=data["equity"], mode="lines", name=layer,
            legendgroup=layer, showlegend=False,
            line=dict(color=color, width=1.5)),
            row=3, col=1)

    fig.add_trace(go.Bar(x=hist["layer"], y=hist["bt_per_sec"],
                         marker_color="#2a9d8f", showlegend=False),
                  row=4, col=1)

    fig.update_xaxes(title_text="trial #", row=2, col=1)
    fig.update_yaxes(title_text="best quality so far", row=2, col=1)
    fig.update_xaxes(title_text="trade #", row=3, col=1)
    fig.update_yaxes(title_text="cumulative pips", row=3, col=1)
    fig.update_yaxes(title_text="bt/sec", row=4, col=1)
    fig.update_layout(height=1400, title="Fire Forex — layer comparison")
    fig.write_html(str(COMPARISON_HTML), include_plotlyjs="cdn")


if __name__ == "__main__":
    main()
