"""Data-driven per-(pair, TF) default ranges.

For any (pair, main_tf) combo we can reach, compute the **median 14-bar ATR
in pips** from the actual parquet file, then derive stop / target / trailing
ranges as ATR multiples. Cached to ``artifacts/volatility_cache.json`` so
subsequent lookups are instant.

**How to add a new pair-aware knob:** append one entry to ``ATR_RULES``
below — ``key → (lo_mult, hi_mult)``. That is the only maintenance needed
for new pair-aware features.

Knobs that are inherently scale-free (risk:reward ratio, ATR multipliers,
EMA periods, hour-of-day) do NOT need an entry here — they live in the
scale-free / TF-based blocks at the bottom of ``derive_ranges``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ── Data discovery ────────────────────────────────────────────────────

_DATA_ROOTS: tuple[Path, ...] = (
    Path(r"G:\My Drive\BackTestData"),
    Path(r"C:\Users\ROG\Projects\ForexPipeline\data"),
)

_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "artifacts" / "volatility_cache.json"


def _data_file(pair: str, tf: str) -> Path | None:
    for root in _DATA_ROOTS:
        p = root / f"{pair}_{tf}.parquet"
        if p.exists():
            return p
    return None


# ── Cache ─────────────────────────────────────────────────────────────

_cache: dict[str, float] | None = None


def _load_cache() -> dict[str, float]:
    global _cache
    if _cache is not None:
        return _cache
    if _CACHE_PATH.exists():
        try:
            _cache = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            return _cache
        except Exception:
            pass
    _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is None:
        return
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(_cache, indent=2, sort_keys=True), encoding="utf-8")


# ── ATR in pips ───────────────────────────────────────────────────────


def _pip_value(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def _compute_atr_pips(pair: str, tf: str, window: int = 14) -> float | None:
    """Median 14-bar ATR in pips for this (pair, tf), straight off the parquet."""
    p = _data_file(pair, tf)
    if p is None:
        return None
    try:
        df = pd.read_parquet(p, columns=["high", "low", "close"])
    except Exception:
        try:
            df = pd.read_parquet(p)
        except Exception:
            return None
        need = {"high", "low", "close"}
        if not need.issubset(df.columns):
            return None
        df = df[["high", "low", "close"]]
    if len(df) < window + 5:
        return None
    high = df["high"].to_numpy(dtype=np.float64)
    low = df["low"].to_numpy(dtype=np.float64)
    close = df["close"].to_numpy(dtype=np.float64)
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = np.convolve(tr, np.ones(window) / window, mode="valid")
    median_atr_price = float(np.median(atr))
    if not math.isfinite(median_atr_price) or median_atr_price <= 0:
        return None
    return median_atr_price / _pip_value(pair)


def get_atr_pips(pair: str, tf: str, *, force: bool = False) -> float | None:
    """Cached ATR-in-pips for (pair, tf). Returns None if the data isn't available."""
    cache = _load_cache()
    key = f"{pair}:{tf}"
    if force or key not in cache:
        v = _compute_atr_pips(pair, tf)
        if v is None:
            return None
        cache[key] = v
        _save_cache()
    return cache[key]


# ── Tidy number helpers ───────────────────────────────────────────────


def _nice_round(x: float) -> float:
    """Round to a tidy 1/2/5 × 10^k."""
    if x <= 0:
        return 0.1
    k = math.floor(math.log10(x))
    base = x / (10**k)
    if base < 1.5:
        nice = 1
    elif base < 3.5:
        nice = 2
    elif base < 7.5:
        nice = 5
    else:
        nice = 10
    return nice * (10**k)


# ── Rules ── one entry per pair-aware knob ────────────────────────────

# Each entry: (lo_multiplier, hi_multiplier) applied to median-ATR-in-pips.
# Adding a new pair-aware knob is one line here.
ATR_RULES: dict[str, tuple[float, float]] = {
    "fixed_sl_pips": (0.3, 6.0),
    "fixed_tp_pips": (0.5, 10.0),
    "trail_activation_pips": (0.3, 8.0),
}


# ── Scale-free blocks ─────────────────────────────────────────────────


def _tf_ema_fast(main_tf: str) -> dict[str, int]:
    bands = {
        "M1": (3, 50),
        "M5": (3, 40),
        "M15": (3, 35),
        "M30": (3, 30),
        "H1": (5, 30),
        "H4": (3, 25),
        "D": (3, 20),
    }
    lo, hi = bands.get(main_tf, (5, 30))
    return {"min": lo, "max": hi}


def _tf_ema_slow(main_tf: str) -> dict[str, int]:
    bands = {
        "M1": (20, 300),
        "M5": (20, 250),
        "M15": (20, 220),
        "M30": (20, 200),
        "H1": (20, 200),
        "H4": (15, 150),
        "D": (10, 120),
    }
    lo, hi = bands.get(main_tf, (20, 200))
    return {"min": lo, "max": hi}


def _sub_tf_default(main_tf: str) -> str:
    return "M1" if main_tf in ("M1", "M5", "M15", "M30", "H1", "H4") else "M5"


# ── Main entry ────────────────────────────────────────────────────────


def derive_ranges(pair: str, main_tf: str) -> dict[str, Any] | None:
    """Data-driven default ranges for (pair, main_tf). None if data missing."""
    atr = get_atr_pips(pair, main_tf)
    if atr is None:
        return None

    out: dict[str, Any] = {}
    for key, (lo_mult, hi_mult) in ATR_RULES.items():
        lo = max(0.1, _nice_round(atr * lo_mult))
        hi = _nice_round(atr * hi_mult)
        if hi <= lo:
            hi = _nice_round(lo * 2)
        out[key] = {"min": lo, "max": hi}

    # Scale-free (same ranges regardless of pair / TF)
    out["rr_ratio"] = {"min": 0.5, "max": 5.0}
    out["atr_mult_sl"] = {"min": 0.5, "max": 6.0}
    out["atr_mult_tp"] = {"min": 0.5, "max": 10.0}
    out["trail_atr_mult"] = {"min": 0.3, "max": 3.0}

    # TF-aware
    out["ema_fast"] = _tf_ema_fast(main_tf)
    out["ema_slow"] = _tf_ema_slow(main_tf)
    out["sub_tf"] = _sub_tf_default(main_tf)

    out["__atr_pips"] = float(atr)
    out["__source"] = "data"
    return out


if __name__ == "__main__":  # pragma: no cover
    import sys

    pair = sys.argv[1] if len(sys.argv) > 1 else "EUR_USD"
    tf = sys.argv[2] if len(sys.argv) > 2 else "H1"
    r = derive_ranges(pair, tf)
    if r is None:
        print(f"no data for {pair}/{tf}")
    else:
        print(f"{pair}/{tf}  median ATR = {r['__atr_pips']:.2f} pips")
        for k, v in r.items():
            if k.startswith("__"):
                continue
            print(f"  {k}: {v}")
