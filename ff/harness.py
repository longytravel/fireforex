"""End-to-end run harness.

Takes an EA config (dict) + run settings and does everything in one place:

1. Resolve data paths from ``EA.data`` (any pair, any timeframe).
2. Auto-derive pip_value from pair if not given.
3. Load main & sub timeframe parquet files.
4. Build the signal library (pooled variants) per ``EA.signals``.
5. Run the sampler → list of trial dicts (``EA.engine_schema``).
6. Encode trials → (N, NUM_PL) float64 via ``EA.engine_mapping``.
7. Call ``ff_core.batch_evaluate``.
8. Extract the 8 numbers from the best trial, render a per-run summary.
9. Save ``artifacts/runs/{layer}_{stamp}.npz`` (quality, pnl, equity, params).
10. Append a row to ``artifacts/history.csv`` (migrating old rows with
    ``strategy="baseline"`` as needed).
11. Regenerate ``artifacts/comparison.html`` with every run side-by-side.

Nothing about pair / timeframe / pip_value / commission is hardcoded. Every
such setting comes from the EA config.
"""
from __future__ import annotations

import json
import logging
import os
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

import ff_core as bc

from . import signal_lib as sl
from . import sampler as spl
from . import encoding as enc


# ── Timeframe / pair tables (constants of the market, not assumptions) ──

# How many bars per year each timeframe produces. Used for Sharpe annualisation.
# 252 trading days/year × bars/day. Weekend trading is rare for forex, so this is
# the standard convention.
BARS_PER_YEAR: dict[str, int] = {
    "M1":  60 * 24 * 252,
    "M5":  12 * 24 * 252,
    "M15": 4 * 24 * 252,
    "M30": 2 * 24 * 252,
    "H1":  24 * 252,
    "H4":  6 * 252,
    "D":   252,
    "W":   52,
}

# Bar duration in minutes — used to build the main→sub bar mapping.
TF_MINUTES: dict[str, int] = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D": 1440, "W": 10080,
}

# Pip value per pair. Override by passing ``pip_value`` explicitly in
# ``EA.execution``. JPY quote pairs use 0.01; other majors use 0.0001.
_JPY_QUOTE_PAIRS = {
    "USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY",
    "CHF_JPY", "CAD_JPY", "NZD_JPY",
}


def pip_value_for(pair: str) -> float:
    return 0.01 if pair.upper() in _JPY_QUOTE_PAIRS else 0.0001


# Data root — overridable with FF_DATA_ROOT environment variable.
DATA_ROOT = Path(os.environ.get("FF_DATA_ROOT", r"G:\My Drive\BackTestData"))


# ── Metric column registry (mirrors core/src/constants.rs M_* indices) ──
# Single source of truth for the API + frontend. (key, label, group).
# Order MUST match M_* indices in constants.rs.
METRIC_COLUMNS: list[tuple[str, str, str]] = [
    ("trades",            "Trades",                     "Activity"),
    ("win_rate",          "Win rate",                   "Activity"),
    ("profit_factor",     "Profit factor",              "Return"),
    ("sharpe",            "Sharpe",                     "Risk-Adjusted"),
    ("sortino",           "Sortino",                    "Risk-Adjusted"),
    ("max_dd_pct",        "Max DD %",                   "Risk"),
    ("return_pct",        "Return %",                   "Return"),
    ("r_squared",         "R² (equity linearity)",      "Risk-Adjusted"),
    ("ulcer",             "Ulcer Index",                "Risk"),
    ("quality",           "Quality",                    "Composite"),
    # New columns — Rust indices 10..24
    ("expectancy_r",      "Expectancy (R)",             "Return"),
    ("expectancy_pips",   "Expectancy (pips)",          "Return"),
    ("sqn",               "SQN (Van Tharp)",            "Risk-Adjusted"),
    ("calmar",            "Calmar",                     "Risk-Adjusted"),
    ("recovery",          "Recovery Factor",            "Risk-Adjusted"),
    ("upi",               "UPI / Martin Ratio",         "Risk-Adjusted"),
    ("k_ratio",           "K-Ratio (Kestner)",          "Risk-Adjusted"),
    ("tail_ratio",        "Tail Ratio (P95/|P5|)",      "Risk"),
    ("omega",             "Omega (τ=0)",                "Return"),
    ("max_consec_loss",   "Max Consecutive Losses",     "Risk"),
    ("psr",               "Probabilistic Sharpe (PSR)", "Overfit-Aware"),
    ("dsr",               "Deflated Sharpe (DSR)",      "Overfit-Aware"),
    ("quality_v2",        "Quality (alias)",            "_hidden"),
    ("avg_hold_bars",     "Avg Hold Bars",              "Forex"),
    ("trades_per_day",    "Trades Per Day",             "Forex"),
]
assert len(METRIC_COLUMNS) == bc.NUM_METRICS, \
    f"METRIC_COLUMNS ({len(METRIC_COLUMNS)}) out of sync with bc.NUM_METRICS ({bc.NUM_METRICS})"

METRIC_INDEX: dict[str, int] = {k: i for i, (k, _, _) in enumerate(METRIC_COLUMNS)}

# Metrics where lower values are better — pick_best argmin instead of argmax.
LOWER_IS_BETTER: frozenset[str] = frozenset({"max_dd_pct", "ulcer", "max_consec_loss"})


def _norm_ppf(p: np.ndarray | float) -> np.ndarray | float:
    """Inverse standard-normal CDF via Beasley-Springer-Moro approximation.

    Max absolute error ~1e-9 over p ∈ (1e-12, 1-1e-12). Vectorised over
    numpy arrays. Self-contained to avoid a scipy dependency for DSR.
    """
    a = [-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00]
    p_low, p_high = 0.02425, 1.0 - 0.02425
    p_arr = np.asarray(p, dtype=np.float64)
    out = np.zeros_like(p_arr, dtype=np.float64)
    # Lower tail
    lo = p_arr < p_low
    if lo.any():
        q = np.sqrt(-2.0 * np.log(p_arr[lo]))
        out[lo] = (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                  ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
    # Upper tail
    hi = p_arr > p_high
    if hi.any():
        q = np.sqrt(-2.0 * np.log(1.0 - p_arr[hi]))
        out[hi] = -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                   ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
    # Central region
    mid = ~(lo | hi)
    if mid.any():
        q = p_arr[mid] - 0.5
        r = q * q
        out[mid] = (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q / \
                   (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1.0)
    return out if p_arr.ndim else float(out)


def _norm_cdf_vec(x: np.ndarray) -> np.ndarray:
    """Standard-normal CDF via numpy — vectorised wrapper for ``math.erf``."""
    from math import erf, sqrt
    inv_sqrt2 = 1.0 / sqrt(2.0)
    flat = np.asarray(x, dtype=np.float64).ravel()
    out = np.fromiter((0.5 * (1.0 + erf(v * inv_sqrt2)) for v in flat),
                      dtype=np.float64, count=flat.size)
    return out.reshape(np.asarray(x).shape)


def _finalise_dsr(metrics_out: np.ndarray, n_trials: int) -> None:
    """Fill the DSR column in-place using PSR + sweep-wide ``n_trials``.

    Lopez de Prado (2014): DSR deflates PSR by the expected maximum of
    ``n_trials`` independent standard-normal draws::

        E[max_N] ≈ (1-γ) Φ⁻¹(1 - 1/N) + γ Φ⁻¹(1 - 1/(N·e))

    γ ≈ 0.5772 (Euler–Mascheroni). PSR is stored as a probability Φ(z);
    we recover z, subtract E[max_N], and re-apply Φ.
    """
    psr_idx = METRIC_INDEX["psr"]
    dsr_idx = METRIC_INDEX["dsr"]
    if n_trials < 2:
        metrics_out[:, dsr_idx] = metrics_out[:, psr_idx]
        return

    euler_mascheroni = 0.5772156649015329
    n = float(n_trials)
    e_max = (1.0 - euler_mascheroni) * _norm_ppf(1.0 - 1.0 / n) \
            + euler_mascheroni * _norm_ppf(1.0 - 1.0 / (n * np.e))

    psr_col = metrics_out[:, psr_idx]
    p_clamped = np.clip(psr_col, 1e-12, 1.0 - 1e-12)
    z = _norm_ppf(p_clamped)
    metrics_out[:, dsr_idx] = _norm_cdf_vec(z - e_max)


def pick_best(
    metrics_out: np.ndarray,
    objective: str = "quality",
    constraints: dict | None = None,
    tie_break: tuple[str, ...] = ("return_pct", "trades"),
    require_profitable: bool = True,
) -> int:
    """Select the best trial by ``objective`` among rows passing ``constraints``.

    ``constraints`` shape: ``{"trades": {">=": 100}, "max_dd_pct": {"<=": 30}}``.
    Falls back to unconstrained argmax if no row passes (preserves behaviour
    for runs that would otherwise return nothing). Tie-break keys resolve
    equal-objective ties by higher value of the named metric.

    Direction is inferred from ``LOWER_IS_BETTER``: argmin for MaxDD, Ulcer,
    MaxConsecLoss; argmax for everything else.

    When ``require_profitable`` is True (default), rows with Return % ≤ 0
    (or Profit Factor ≤ 1 as fallback) are filtered out — so that metrics
    that can be maximised by losing trials (R², K-Ratio, Tail Ratio) don't
    promote a loser to "best". Falls back to unfiltered best if no
    profitable row exists.
    """
    obj_idx = METRIC_INDEX[objective]
    obj_col = metrics_out[:, obj_idx]
    direction = -1.0 if objective in LOWER_IS_BETTER else 1.0

    mask = np.ones(metrics_out.shape[0], dtype=bool)
    if constraints:
        for key, rules in constraints.items():
            col = metrics_out[:, METRIC_INDEX[key]]
            for op, val in rules.items():
                if op == ">=":
                    mask &= col >= val
                elif op == "<=":
                    mask &= col <= val
                elif op == ">":
                    mask &= col > val
                elif op == "<":
                    mask &= col < val
                elif op == "==":
                    mask &= col == val
        if not mask.any():
            mask = np.ones(metrics_out.shape[0], dtype=bool)

    if require_profitable:
        ret = metrics_out[:, METRIC_INDEX["return_pct"]]
        pf = metrics_out[:, METRIC_INDEX["profit_factor"]]
        prof_mask = (ret > 0) | (pf > 1.0)
        if prof_mask.any():
            mask &= prof_mask
        # else: leave mask as-is — no profitable trials, fall back to best loser.

    # NaN in the objective column can't be argmaxed meaningfully — treat as -inf.
    safe_obj = np.where(np.isfinite(obj_col), direction * obj_col, -np.inf)
    masked_obj = np.where(mask, safe_obj, -np.inf)
    best_val = masked_obj.max()
    candidates = np.where(masked_obj == best_val)[0]
    if candidates.size > 1:
        for tb_key in tie_break:
            tb_idx = METRIC_INDEX.get(tb_key)
            if tb_idx is None:
                continue
            tb_vals = metrics_out[candidates, tb_idx]
            tb_finite = np.isfinite(tb_vals)
            if not tb_finite.any():
                continue  # all NaN on this tie-break key — skip
            tb_safe = np.where(tb_finite, tb_vals, -np.inf)
            tb_max = tb_safe.max()
            candidates = candidates[tb_safe == tb_max]
            if candidates.size == 1:
                break
    return int(candidates[0])


# ── Data loading + mapping (TF-agnostic) ───────────────────────────────

# Parquet cache. Long-lived processes (web server) reload the same 5.7M M1
# rows on every run otherwise. Keyed on (resolved path, mtime_ns, size) so a
# replaced file invalidates cleanly. Safe in short-lived CLI runs too —
# the dict simply stays empty through the single run.
_PARQUET_CACHE: dict = {}


def load_parquet(path: Path) -> pd.DataFrame:
    try:
        st = Path(path).stat()
        key = (str(Path(path).resolve()), int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        key = None
    if key is not None:
        hit = _PARQUET_CACHE.get(key)
        if hit is not None:
            return hit
    df = pd.read_parquet(path)
    df.columns = [c.lower() for c in df.columns]
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    df = df.sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    if key is not None:
        _PARQUET_CACHE[key] = df
    return df


def build_main_to_sub_mapping(main_index: pd.DatetimeIndex,
                              sub_index: pd.DatetimeIndex,
                              main_tf_minutes: int) -> tuple[np.ndarray, np.ndarray]:
    """For each main-TF bar, find the sub-TF bar index range it covers.

    Generalisation of the H1→M1 mapping — works for any (main, sub) pair where
    main_tf_minutes cleanly divides into sub_tf bars.

    Indexes may be datetime64[ns, UTC] or datetime64[us, UTC]; `asi8` returns
    whatever scalar unit the array was stored in. Normalise both sides to
    nanoseconds before arithmetic so a ``datetime64[us]`` index doesn't make
    the bar width three orders of magnitude too wide.
    """
    main_ns = main_index.astype("datetime64[ns, UTC]").asi8
    sub_ns = sub_index.astype("datetime64[ns, UTC]").asi8
    bar_ns = int(main_tf_minutes * 60 * 1_000_000_000)
    start = np.searchsorted(sub_ns, main_ns, side="left").astype(np.int64)
    end = np.searchsorted(sub_ns, main_ns + bar_ns, side="left").astype(np.int64)
    return start, end


# ── Main run ───────────────────────────────────────────────────────────

ART_ROOT = Path(__file__).resolve().parent.parent / "artifacts"
HISTORY_CSV = ART_ROOT / "history.csv"
COMPARISON_HTML = ART_ROOT / "comparison.html"
RUNS_DIR = ART_ROOT / "runs"


def _resolve_execution(ea: dict) -> dict:
    """Fill defaults + auto-derive pip_value from pair if not set."""
    exe = dict(ea.get("execution", {}))
    pair = ea["data"]["pair"]
    if exe.get("pip_value") is None:
        exe["pip_value"] = pip_value_for(pair)
    exe.setdefault("commission_pips", 0.3)
    exe.setdefault("max_spread_pips", 10.0)
    exe.setdefault("slippage_pips", 0.0)
    exe.setdefault("atr_period", 14)
    return exe


def _summarise_trial(trial: dict) -> str:
    """Flatten a trial dict into a one-line human-readable string."""
    def flatten(node, prefix=""):
        out = []
        if isinstance(node, dict):
            for k, v in node.items():
                key = f"{prefix}.{k}" if prefix else k
                out.extend(flatten(v, key))
        else:
            pretty = f"{node:.3f}" if isinstance(node, float) else str(node)
            out.append(f"{prefix}={pretty}")
        return out
    parts = flatten(trial)
    return " · ".join(parts)


# Column order matches ff_core NUM_TRADE_FIELDS rows emitted by lib.rs.
TRADE_FIELD_NAMES = (
    "pnl_pips",
    "exit_reason",
    "direction",
    "entry_bar_index",
    "entry_sub_bar_index",
    "entry_price",
    "exit_bar_index",
    "exit_sub_bar_index",
    "exit_price",
)


def _build_best_trade_log(
    flat_trade_row: np.ndarray,
    n_trades: int,
    main_index: "pd.DatetimeIndex",
    sub_index: "pd.DatetimeIndex",
) -> np.ndarray:
    """Turn the best trial's flat trade-record slice into a structured array.

    Adds entry_ts / exit_ts columns resolved from the main/sub DatetimeIndexes
    so the reconciler can join against MT5 deals without any engine re-run.
    `exit_sub_bar_index == -1` flags H1-level exits (max_bars, stale, EOD):
    exit_ts falls back to the main bar close in that case.
    """
    num_fields = len(TRADE_FIELD_NAMES)
    dtype = np.dtype(
        [(name, np.float64) for name in TRADE_FIELD_NAMES]
        + [("entry_ts", "datetime64[ns]"), ("exit_ts", "datetime64[ns]")]
    )
    if n_trades == 0:
        return np.empty(0, dtype=dtype)

    rows = flat_trade_row[: n_trades * num_fields].reshape(n_trades, num_fields)
    out = np.empty(n_trades, dtype=dtype)
    for i, name in enumerate(TRADE_FIELD_NAMES):
        out[name] = rows[:, i]

    entry_main_idx = rows[:, 3].astype(np.int64)
    entry_sub_idx = rows[:, 4].astype(np.int64)
    exit_main_idx = rows[:, 6].astype(np.int64)
    exit_sub_idx = rows[:, 7].astype(np.int64)

    main_ts = main_index.values.astype("datetime64[ns]")
    sub_ts = sub_index.values.astype("datetime64[ns]")

    # Entry: always resolved from sub_tf (first M1 bar after the signal close).
    # Cap indices so a malformed row cannot IndexError.
    sub_n = sub_ts.shape[0]
    main_n = main_ts.shape[0]
    entry_sub_idx_clipped = np.clip(entry_sub_idx, 0, sub_n - 1)
    out["entry_ts"] = sub_ts[entry_sub_idx_clipped]

    # Exit: prefer sub_tf timestamp; fall back to main_tf close on H1-only exits
    # (exit_sub_bar_index == -1 for EXIT_MAX_BARS / EXIT_STALE / EXIT_NONE).
    exit_has_sub = exit_sub_idx >= 0
    exit_sub_idx_clipped = np.clip(exit_sub_idx, 0, sub_n - 1)
    exit_main_idx_clipped = np.clip(exit_main_idx, 0, main_n - 1)
    exit_ts = np.where(
        exit_has_sub,
        sub_ts[exit_sub_idx_clipped],
        main_ts[exit_main_idx_clipped],
    )
    out["exit_ts"] = exit_ts
    return out


_progress_logger = logging.getLogger(__name__ + ".progress")


def _safe_progress(cb: Callable[[float, str], None] | None,
                   fraction: float, message: str) -> None:
    """Call ``cb(fraction, message)`` swallowing any exception.

    Progress hooks from callers (e.g. a FastAPI job runner) are strictly
    informational — a crashing callback must NOT break the run. We log the
    failure to a module-local debug logger so it's diagnosable but never
    raise, and never write to stderr.
    """
    if cb is None:
        return
    try:
        cb(float(fraction), str(message))
    except Exception:  # pragma: no cover — pure safety net.
        try:
            _progress_logger.debug("progress_cb raised", exc_info=True)
        except Exception:
            pass


def run(ea: dict, *, layer_name: str, optimizer: str = "random", seed: int = 42,
        n_trials: int = 2000, open_browser: bool = True,
        progress_cb: Callable[[float, str], None] | None = None) -> dict:
    """Execute an EA end-to-end.

    Returns a dict with the key numbers — useful for programmatic parity checks.

    ``progress_cb`` (optional) is called with ``(fraction_done, message)`` at
    meaningful checkpoints: signal-library build, trial sampling, encoding,
    backtest batch, metrics, completion. The fraction is monotonically
    non-decreasing and ends at ``1.0``. Exceptions in the callback are
    swallowed so a faulty hook cannot break the run. When ``None`` (default),
    behaviour is bit-identical to before this hook existed.
    """
    t_total = time.perf_counter()
    _safe_progress(progress_cb, 0.0, "starting")

    data_cfg = ea["data"]
    exe_cfg = _resolve_execution(ea)
    pair = data_cfg["pair"]
    main_tf = data_cfg["main_tf"]
    sub_tf = data_cfg["sub_tf"]
    if main_tf not in BARS_PER_YEAR or sub_tf not in BARS_PER_YEAR:
        raise ValueError(
            f"Unknown timeframe: main={main_tf!r} sub={sub_tf!r}. "
            f"Known: {sorted(BARS_PER_YEAR)}"
        )
    if TF_MINUTES[sub_tf] >= TF_MINUTES[main_tf]:
        raise ValueError(f"sub_tf must be finer than main_tf, got main={main_tf} sub={sub_tf}")

    print(f"Fire Forex · {ea['name']} · layer={layer_name} · opt={optimizer} · seed={seed}")

    # 1. Load data.
    path_main = DATA_ROOT / f"{pair}_{main_tf}.parquet"
    path_sub = DATA_ROOT / f"{pair}_{sub_tf}.parquet"
    _safe_progress(progress_cb, 0.02, f"loading data ({pair} {main_tf}/{sub_tf})")
    print(f"[load] main TF: {path_main.name}")
    t = time.perf_counter()
    main_df = load_parquet(path_main)
    print(f"       {len(main_df):,} bars  {main_df.index.min()} → {main_df.index.max()}"
          f"  in {time.perf_counter()-t:.2f}s")
    print(f"[load] sub TF:  {path_sub.name}")
    t = time.perf_counter()
    sub_df = load_parquet(path_sub)
    print(f"       {len(sub_df):,} bars  {sub_df.index.min()} → {sub_df.index.max()}"
          f"  in {time.perf_counter()-t:.2f}s")

    # 1b. Optional user date window (Data tab / Parameters tab).
    from .data.date_slice import clip as _clip
    user_start = data_cfg.get("start_date")
    user_end = data_cfg.get("end_date")
    if user_start or user_end:
        main_df = _clip(main_df, user_start, user_end)
        sub_df = _clip(sub_df, user_start, user_end)
        print(f"[window] user-requested {user_start or '…'} → {user_end or '…'}  "
              f"main={len(main_df):,} · sub={len(sub_df):,}")
        if len(main_df) == 0 or len(sub_df) == 0:
            raise ValueError(
                f"user date window {user_start}..{user_end} yielded zero bars "
                f"for {pair} {main_tf}/{sub_tf}"
            )

    # 2. Align windows.
    start = max(main_df.index.min(), sub_df.index.min())
    stop = min(main_df.index.max(), sub_df.index.max())
    main_df = main_df.loc[start:stop]
    sub_df = sub_df.loc[start:stop]
    print(f"[align] shared window {start} → {stop}  "
          f"main={len(main_df):,} · sub={len(sub_df):,}")

    # 3. Main → sub mapping.
    t = time.perf_counter()
    map_start, map_end = build_main_to_sub_mapping(main_df.index, sub_df.index,
                                                   TF_MINUTES[main_tf])
    print(f"[map]  main→sub built in {time.perf_counter()-t:.2f}s")

    # 4. OHLCS arrays.
    h_h = main_df["high"].to_numpy(dtype=np.float64, copy=True)
    h_l = main_df["low"].to_numpy(dtype=np.float64, copy=True)
    h_c = main_df["close"].to_numpy(dtype=np.float64, copy=True)
    h_s = (main_df["spread"].to_numpy(dtype=np.float64, copy=True)
           if "spread" in main_df.columns
           else np.full(len(main_df), exe_cfg["pip_value"], dtype=np.float64))
    m_h = sub_df["high"].to_numpy(dtype=np.float64, copy=True)
    m_l = sub_df["low"].to_numpy(dtype=np.float64, copy=True)
    m_c = sub_df["close"].to_numpy(dtype=np.float64, copy=True)
    m_s = (sub_df["spread"].to_numpy(dtype=np.float64, copy=True)
           if "spread" in sub_df.columns
           else np.full(len(sub_df), exe_cfg["pip_value"], dtype=np.float64))

    # 5. Signal library.
    _safe_progress(progress_cb, 0.10, "building signal library")
    print(f"[signals] building library")
    t = time.perf_counter()

    def _lib_cb(sub_frac: float, msg: str) -> None:
        # Scope 0..1 sub-progress into the 0.10 → 0.30 band for the overall run.
        _safe_progress(progress_cb, 0.10 + 0.20 * sub_frac, msg)

    lib = sl.build_signal_library(
        ea["signals"], main_df,
        pip_value=exe_cfg["pip_value"],
        atr_period=exe_cfg["atr_period"],
        progress_cb=_lib_cb if progress_cb is not None else None,
        data_path=path_main,
    )
    print(f"          {lib.n_variants} variants · {lib.n_signals:,} pooled signals "
          f"in {time.perf_counter()-t:.2f}s")
    _safe_progress(progress_cb, 0.30,
                   f"signal library ready ({lib.n_variants} variants, "
                   f"{lib.n_signals:,} signals)")

    # 6. Sample trials.
    if optimizer != "random":
        raise NotImplementedError(f"optimizer={optimizer!r} not yet supported (random only)")
    _safe_progress(progress_cb, 0.35, f"sampling {n_trials} trials")
    sampler = spl.RandomSampler(ea["engine_schema"], n_variants=lib.n_variants, seed=seed)
    trials = sampler.sample(n_trials)
    print(f"[sample] {len(trials)} trials")

    # 7. Encode.
    _safe_progress(progress_cb, 0.40, "encoding trials")
    param_matrix = enc.encode(trials, ea["engine_mapping"])
    param_layout = np.arange(bc.NUM_PL, dtype=np.int64)

    # 8. batch_evaluate.
    # max_trades per trial = max signals per variant (engine skips non-matching signals).
    max_trades = max(v["n_signals"] for v in lib.variant_map)
    metrics_out = np.zeros((n_trials, bc.NUM_METRICS), dtype=np.float64)
    pnl_buffers = np.empty((n_trials, max_trades), dtype=np.float64)
    # Per-trade records for the live-parity validator. Flat layout
    # (n_trials, max_trades * NUM_TRADE_FIELDS) mirrors pnl_buffers' chunking.
    # Column order per trade: pnl_pips, exit_reason, direction,
    # entry_bar_index, entry_sub_bar_index, entry_price,
    # exit_bar_index, exit_sub_bar_index, exit_price.
    trade_records = np.empty((n_trials, max_trades * bc.NUM_TRADE_FIELDS),
                             dtype=np.float64)

    # Per-signal filter matrix, shape (NUM_SIGNAL_PARAMS, n_signals).
    # -1 means "no filter" — we don't use this feature yet.
    sig_filters = np.full((bc.NUM_SIGNAL_PARAMS, lib.bar_index.size), -1, dtype=np.int64)

    # Warm-up call (pays any one-time cost for a fair sweep timing).
    bc.batch_evaluate(
        h_h, h_l, h_c, h_s,
        exe_cfg["pip_value"], exe_cfg["slippage_pips"],
        lib.bar_index[:1], lib.direction[:1], lib.entry_price[:1],
        lib.hour[:1], lib.day[:1], lib.atr_pips[:1],
        lib.swing_sl[:1], lib.filter_value[:1], lib.variant[:1],
        np.ascontiguousarray(sig_filters[:, :1]),
        param_matrix[:1], param_layout,
        np.zeros((1, bc.NUM_METRICS), dtype=np.float64),
        1, BARS_PER_YEAR[main_tf], exe_cfg["commission_pips"], exe_cfg["max_spread_pips"],
        m_h, m_l, m_c, m_s,
        map_start, map_end,
        np.empty((1, 1), dtype=np.float64),
        np.empty((1, 1 * bc.NUM_TRADE_FIELDS), dtype=np.float64),
    )

    # Heartbeat: batch_evaluate is one blocking Rust call that can't yield
    # progress. We spawn a daemon thread that linearly advances the bar from
    # 0.45 → 0.84 based on expected wall time (tuned from recent runs) so the
    # UI doesn't look frozen. Stops the moment the call returns.
    import threading as _threading
    est_sweep_s = max(5.0, (n_trials * lib.n_signals) / 60_000_000.0)
    _stop_heartbeat = _threading.Event()

    def _heartbeat_loop() -> None:
        hb_start = time.perf_counter()
        while not _stop_heartbeat.wait(0.5):
            hb_elapsed = time.perf_counter() - hb_start
            # Cap at 98% of the sweep band so we never claim it's done.
            frac = min(0.98, hb_elapsed / est_sweep_s)
            interp = 0.45 + (0.85 - 0.45) * frac
            _safe_progress(progress_cb, interp,
                           f"running {n_trials:,} backtests · {hb_elapsed:.0f}s elapsed"
                           f" (est {est_sweep_s:.0f}s)")

    _safe_progress(progress_cb, 0.45,
                   f"running backtest ({n_trials} trials × {lib.n_signals:,} signals)")
    print(f"[sweep] calling batch_evaluate({n_trials} × {lib.n_signals:,} signals)…")
    _hb_thread = _threading.Thread(target=_heartbeat_loop, daemon=True)
    _hb_thread.start()
    t = time.perf_counter()
    try:
        bc.batch_evaluate(
            h_h, h_l, h_c, h_s,
            exe_cfg["pip_value"], exe_cfg["slippage_pips"],
            lib.bar_index, lib.direction, lib.entry_price,
            lib.hour, lib.day, lib.atr_pips,
            lib.swing_sl, lib.filter_value, lib.variant,
            sig_filters,
            param_matrix, param_layout,
            metrics_out,
            max_trades, BARS_PER_YEAR[main_tf], exe_cfg["commission_pips"], exe_cfg["max_spread_pips"],
            m_h, m_l, m_c, m_s,
            map_start, map_end,
            pnl_buffers,
            trade_records,
        )
    finally:
        _stop_heartbeat.set()
    elapsed = time.perf_counter() - t
    rate = n_trials / elapsed
    print(f"        done in {elapsed:.2f}s  →  {rate:,.0f} evals/sec")
    _safe_progress(progress_cb, 0.85, "backtest complete, computing metrics")

    # 9. Extract best + 8 numbers.
    # DSR needs sweep-wide n_trials to deflate PSR. Finalise in Python (cheap).
    _finalise_dsr(metrics_out, n_trials)

    quality = metrics_out[:, METRIC_INDEX["quality"]]
    # Default objective stays "quality" (v1) for baseline back-compat; the
    # frontend can post-facto re-rank by any other column via pick_best().
    best = pick_best(metrics_out, objective="quality", tie_break=("return_pct", "trades"))
    running_best = np.maximum.accumulate(quality)
    n_trades_best = int(metrics_out[best, 0])
    pnl_best = pnl_buffers[best, :n_trades_best].copy()
    total_pips = float(pnl_best.sum())

    # Per-trade log for the live-parity validator. Slice the best trial's
    # trade_records, reshape to (n_trades_best, NUM_TRADE_FIELDS), and turn
    # into a structured array with named columns + resolved timestamps.
    trades_best = _build_best_trade_log(
        trade_records[best], n_trades_best,
        main_df.index, sub_df.index,
    )
    # Parity invariant: the trade-log pnl sum must match the aggregate
    # total_pips down to float arithmetic. Guards against drift between
    # pnl_buffer and trade_records writes if the engine is refactored.
    trades_pnl_sum = float(trades_best["pnl_pips"].sum()) if n_trades_best else 0.0
    if abs(trades_pnl_sum - total_pips) > 1e-9:
        raise RuntimeError(
            f"trade-log parity broken: trades.pnl_pips.sum()={trades_pnl_sum!r} "
            f"vs aggregate total_pips={total_pips!r}"
        )
    expectancy_pips = total_pips / n_trades_best if n_trades_best else 0.0
    equity_curve = np.cumsum(pnl_best)
    total_runtime = time.perf_counter() - t_total

    best_trial = trials[best]
    variant_id = best_trial["signal_variant"]
    variant_info = lib.variant_map[variant_id] if variant_id < len(lib.variant_map) else {}

    # ── Print the 8 numbers ─────────────────────────────────────────
    wr = metrics_out[best, 1]
    win_rate_pct = wr * 100 if wr <= 1 else wr
    print(f"\n┌── {ea['name']} · {layer_name} · {optimizer} · N={n_trials} ─────")
    print( "│ SPEED")
    print(f"│   backtests/sec  : {rate:>12,.0f}")
    print(f"│   total runtime  : {total_runtime:>12.2f} s")
    print( "│ ACTIVITY")
    print(f"│   trades (best)  : {n_trades_best:>12,}")
    print(f"│   win rate       : {win_rate_pct:>12.2f} %")
    print( "│ MONEY")
    print(f"│   total pips     : {total_pips:>+12,.0f}")
    print(f"│   expectancy     : {expectancy_pips:>+12.2f} pips/trade")
    print( "│ RISK")
    print(f"│   max drawdown   : {metrics_out[best, 5]:>12.2f} %")
    print(f"│   profit factor  : {metrics_out[best, 2]:>12.3f}")
    print( "│ best variant")
    print(f"│   id={variant_id}  family={variant_info.get('family','?')}  "
          f"params={variant_info.get('params',{})}")
    print( "│ best trial (engine)")
    print(f"│   {_summarise_trial(best_trial['engine'])}")
    print( "└──────────────────────────────────────────────────────────\n")

    # 10. Save npz.
    _safe_progress(progress_cb, 0.92, "saving run artifacts")
    ART_ROOT.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_file = RUNS_DIR / f"{layer_name}_{stamp}.npz"
    # Per-trial payload for the interactive scatter plot. Full metrics +
    # packed pnl buffer + per-trial trade count so the UI can slice any
    # trial's equity curve on demand without re-running the engine.
    per_trial_metrics = metrics_out.astype(np.float32, copy=True)
    per_trial_pnl = pnl_buffers.astype(np.float32, copy=True)
    per_trial_n_trades = metrics_out[:, 0].astype(np.int32, copy=True)

    np.savez_compressed(
        run_file,
        quality=quality,
        running_best=running_best,
        pnl=pnl_best,
        equity=equity_curve,
        best_trial_json=np.array(json.dumps(best_trial, default=_json_default)),
        variant_map_json=np.array(json.dumps(lib.variant_map, default=_json_default)),
        param_matrix=param_matrix,
        per_trial_metrics=per_trial_metrics,
        per_trial_pnl=per_trial_pnl,
        per_trial_n_trades=per_trial_n_trades,
        trades=trades_best,
    )
    print(f"[save] run data → {run_file.name}")

    # 11. Append to history.csv.
    row = {
        "datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": ea["name"],
        "layer": layer_name,
        "optimizer": optimizer,
        "seed": seed,
        "n_trials": n_trials,
        "n_variants": lib.n_variants,
        "n_signals": lib.n_signals,
        "pair": pair,
        "main_tf": main_tf,
        "sub_tf": sub_tf,
        "bt_per_sec": round(rate, 0),
        "runtime_s": round(total_runtime, 2),
        "trades": n_trades_best,
        "win_rate_pct": round(win_rate_pct, 3),
        "total_pips": round(total_pips, 1),
        "expectancy_pips": round(expectancy_pips, 3),
        "max_dd_pct": round(float(metrics_out[best, 5]), 3),
        "profit_factor": round(float(metrics_out[best, 2]), 4),
        "sharpe": round(float(metrics_out[best, 3]), 4),
        "return_pct": round(float(metrics_out[best, 6]), 2),
        "quality": round(float(metrics_out[best, 9]), 4),
        "best_variant_id": variant_id,
        "best_variant_family": variant_info.get("family", ""),
        "run_file": run_file.name,
    }
    if HISTORY_CSV.exists():
        hist = pd.read_csv(HISTORY_CSV)
        # Migrate older rows missing 'strategy' column.
        if "strategy" not in hist.columns:
            hist["strategy"] = "baseline"
        hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    else:
        hist = pd.DataFrame([row])
    hist.to_csv(HISTORY_CSV, index=False)
    print(f"[save] appended row → {HISTORY_CSV.name}  (now {len(hist)} runs)")

    # 12. Comparison HTML.
    build_comparison_html(hist)
    print(f"[viz]  wrote {COMPARISON_HTML.name}")
    if open_browser:
        webbrowser.open(COMPARISON_HTML.as_uri())

    print(f"\n[total] {total_runtime:.2f}s end-to-end")
    _safe_progress(progress_cb, 1.0, "done")
    return {
        "rate_bt_per_sec": rate,
        "runtime_s": total_runtime,
        "trades": n_trades_best,
        "win_rate_pct": win_rate_pct,
        "total_pips": total_pips,
        "expectancy_pips": expectancy_pips,
        "max_dd_pct": float(metrics_out[best, 5]),
        "profit_factor": float(metrics_out[best, 2]),
        "quality_best": float(metrics_out[best, 9]),
        "best_trial": best_trial,
        "run_file": str(run_file),
    }


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.ndarray,)):
        return o.tolist()
    if isinstance(o, bool):
        return bool(o)
    raise TypeError(f"Not JSON-serializable: {type(o).__name__}")


# ── Comparison renderer (migrated + generalised from demo_speed.py) ────

def build_comparison_html(hist: pd.DataFrame) -> None:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    # Load each layer's saved run — if missing file, skip the curves but keep the row.
    runs: dict[str, dict] = {}
    for _, r in hist.iterrows():
        p = RUNS_DIR / r["run_file"]
        if p.exists():
            with np.load(p, allow_pickle=False) as z:
                runs[r["layer"]] = {k: z[k].copy() for k in z.files
                                    if z[k].dtype != object and k != "param_matrix"}

    fig = make_subplots(
        rows=4, cols=1,
        row_heights=[0.30, 0.24, 0.24, 0.22],
        subplot_titles=(
            "All runs — layered comparison",
            "Running best quality (climb rate = optimiser smartness)",
            "Equity curve of each layer's best variant (cumulative pips)",
            "Speed — backtests per second",
        ),
        specs=[[{"type": "table"}], [{"type": "scatter"}],
               [{"type": "scatter"}], [{"type": "bar"}]],
        vertical_spacing=0.07,
    )

    table_cols = [c for c in ["strategy", "layer", "optimizer", "pair", "main_tf",
                              "bt_per_sec", "runtime_s", "trades", "win_rate_pct",
                              "total_pips", "expectancy_pips", "max_dd_pct",
                              "profit_factor", "best_variant_family"]
                  if c in hist.columns]
    fig.add_trace(go.Table(
        header=dict(values=table_cols, fill_color="#222", font=dict(color="white")),
        cells=dict(values=[hist[c].tolist() for c in table_cols], align="right")
    ), row=1, col=1)

    palette = ["#e63946", "#2a9d8f", "#e9c46a", "#264653", "#f4a261",
               "#9b5de5", "#06a77d", "#d62828", "#457b9d", "#6d597a"]
    for i, (layer, data) in enumerate(runs.items()):
        color = palette[i % len(palette)]
        if "running_best" in data:
            fig.add_trace(go.Scatter(
                x=np.arange(len(data["running_best"])),
                y=data["running_best"], mode="lines", name=layer,
                legendgroup=layer, line=dict(color=color, width=2)
            ), row=2, col=1)
        if "equity" in data:
            fig.add_trace(go.Scatter(
                x=np.arange(len(data["equity"])),
                y=data["equity"], mode="lines", name=layer,
                legendgroup=layer, showlegend=False,
                line=dict(color=color, width=1.5)
            ), row=3, col=1)

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
