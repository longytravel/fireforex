"""Signal-family registry and library builder.

Each *family* is a Python function that takes an H1 DataFrame and a dict of
periods, and returns a :class:`SignalSet`. Families are registered by name via
the :func:`register` decorator.

An EA declares the families it uses in its ``signals`` section, with per-family
parameter grids expressed as schema leaves (usually :class:`IntRange`). The
:func:`build_signal_library` function expands each grid into the Cartesian
product of its parameters, calls each family once per valid combo, and returns
a single pooled set of arrays (sorted by bar index, tagged by variant id) ready
to feed the Rust engine.

The engine filters signals by equality: ``signal.variant == PL_SIGNAL_VARIANT``
(see audit). So a per-trial call to ``batch_evaluate`` sees all variants in the
pooled array but only acts on the one the trial picked.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import time as _time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
import pandas as pd

from . import schema as sc

# Disk cache for build_signal_library. The key binds everything that could
# change the signals: data file identity, the ACTUAL sliced DataFrame window
# passed in (not just the source file — the harness aligns main/sub), the
# source code of ff/signal_lib.py itself (so editing a signal formula
# invalidates every stale cache automatically), the canonical grid, and
# numeric knobs. Bump _CACHE_VERSION only for key-format changes.
_CACHE_VERSION = "v2"
_CACHE_DIR = Path(__file__).resolve().parent.parent / "artifacts" / "signal_cache"


def _source_hash() -> str:
    """Short hash of this module's bytes. Any edit to ewm/atr_ema/rsi or any
    registered signal family invalidates every on-disk cache entry on the
    next call — no manual _CACHE_VERSION bumps required."""
    try:
        b = Path(__file__).read_bytes()
    except OSError:
        return "nosrc"
    return hashlib.sha256(b).hexdigest()[:16]


def _df_fingerprint(df: pd.DataFrame) -> str:
    """Cheap fingerprint of a DataFrame's effective content.

    Includes length, first/last timestamp, and first/last close — enough to
    notice any window shift (harness aligns to the shared main/sub window,
    which changes when either parquet changes).
    """
    n = len(df)
    if n == 0:
        return "empty"
    first_ts = int(df.index[0].value)
    last_ts = int(df.index[-1].value)
    close_col = "close" if "close" in df.columns else df.columns[0]
    first_c = float(df[close_col].iloc[0])
    last_c = float(df[close_col].iloc[-1])
    return f"{n}|{first_ts}|{last_ts}|{first_c:.12g}|{last_c:.12g}"


# ``pip_value`` is not constant across pairs (JPY pairs use 0.01). To avoid
# silent bugs on non-EUR/USD data, every family REQUIRES an explicit
# ``pip_value`` keyword — no default. The harness passes it through after
# looking it up from the EA's declared pair.


# ── Types ──────────────────────────────────────────────────────────────


@dataclass
class SignalSet:
    """One family's output for one parameter combo.

    All arrays are of equal length N = number of signal events. Array dtypes
    match what ``ff_core.batch_evaluate`` expects.
    """

    bar_index: np.ndarray  # int64
    direction: np.ndarray  # int64, +1 long / -1 short
    entry_price: np.ndarray  # float64
    atr_pips: np.ndarray  # float64
    hour: np.ndarray  # int64
    day: np.ndarray  # int64
    filter_value: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    swing_sl: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))

    def __post_init__(self) -> None:
        n = self.bar_index.size
        for name in ("direction", "entry_price", "atr_pips", "hour", "day"):
            a = getattr(self, name)
            if a.size != n:
                raise ValueError(f"SignalSet: {name} has length {a.size}, expected {n}")
        # Allow filter_value / swing_sl to be zero-length → default to zeros of length n.
        if self.filter_value.size == 0 and n > 0:
            self.filter_value = np.zeros(n, dtype=np.float64)
        if self.swing_sl.size == 0 and n > 0:
            self.swing_sl = np.zeros(n, dtype=np.float64)


class InvalidCombo(Exception):
    """Raised by a family when the requested parameter combo is nonsensical
    (e.g. ema_cross with fast >= slow). The library builder skips it silently."""


_REGISTRY: dict[str, Callable] = {}


def register(name: str) -> Callable:
    """Decorator: register a signal family under ``name``."""

    def _wrap(fn: Callable) -> Callable:
        if name in _REGISTRY:
            raise KeyError(f"signal family {name!r} already registered")
        _REGISTRY[name] = fn
        return fn

    return _wrap


def get_family(name: str) -> Callable:
    if name not in _REGISTRY:
        raise KeyError(f"signal family {name!r} not registered. Known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def list_families() -> list[str]:
    return sorted(_REGISTRY)


# ── Indicator helpers (migrated from demo_speed.py) ────────────────────


def ewm(arr: np.ndarray, span: int) -> np.ndarray:
    """Pandas-style exponentially-weighted moving average (span-based alpha)."""
    return pd.Series(arr).ewm(span=span, adjust=False).mean().to_numpy(dtype=np.float64, copy=True)


def atr_ema(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """ATR smoothed with the span-based EMA above. Cached per (array-triple, period)
    so we don't recompute the same ATR 3,556 times when only signal params vary."""
    key = (id(high), id(low), id(close), int(period))
    hit = _ATR_CACHE.get(key)
    if hit is not None:
        return hit
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    out = ewm(tr, period)
    _ATR_CACHE[key] = out
    return out


# ── Session tagging (for session-based filtering via PL_BUY/SELL_FILTER) ──

# Integer IDs by design: engine filter slots are exact-match on integers.
SESSION_ASIA = 0
SESSION_LONDON = 1
SESSION_NY = 2
SESSION_OVERLAP = 3  # London-NY overlap (13-16 UTC)

# Rough UTC session hours. Real-world session boundaries drift with DST; good-enough
# for strategy gating and refinable per-EA.
_SESSION_OF_HOUR = {
    **{h: SESSION_ASIA for h in range(0, 7)},
    **{h: SESSION_LONDON for h in range(7, 13)},
    **{h: SESSION_OVERLAP for h in range(13, 16)},
    **{h: SESSION_NY for h in range(16, 22)},
    **{h: SESSION_ASIA for h in range(22, 24)},  # late-NY / Asia rollover
}


def session_of_hour(hour_arr: np.ndarray) -> np.ndarray:
    """Map a per-signal UTC hour array to a session ID array.

    Returns an int64 array the same shape as ``hour_arr``. Intended to be
    written into :attr:`SignalSet.filter_value` so an EA can gate trades by
    session via ``PL_BUY_FILTER_MAX`` / ``PL_SELL_FILTER_MIN`` (which are
    exact-match filters in the Rust engine).
    """
    out = np.empty(hour_arr.size, dtype=np.int64)
    for h, sid in _SESSION_OF_HOUR.items():
        out[hour_arr == h] = sid
    return out


def rsi(close: np.ndarray, period: int) -> np.ndarray:
    """Wilder's RSI."""
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = ewm(gain, period)
    avg_loss = ewm(loss, period)
    rs = np.where(avg_loss > 0, avg_gain / avg_loss, np.inf)
    return 100.0 - (100.0 / (1.0 + rs))


# ── Shared context (hour, day, atr, price) ─────────────────────────────

# Per-build caches. Cleared at the top of ``build_signal_library`` so the same
# DataFrame + ATR period is computed once per run, not once per variant.
_ARRAYS_CACHE: dict = {}
_ATR_CACHE: dict = {}


def _main_tf_arrays(main_tf: pd.DataFrame) -> dict:
    """Cache-friendly extraction of the fields every family needs from the main-TF frame."""
    key = id(main_tf)
    hit = _ARRAYS_CACHE.get(key)
    if hit is not None:
        return hit
    out = {
        "high": main_tf["high"].to_numpy(dtype=np.float64, copy=True),
        "low": main_tf["low"].to_numpy(dtype=np.float64, copy=True),
        "close": main_tf["close"].to_numpy(dtype=np.float64, copy=True),
        "hour": main_tf.index.hour.to_numpy().astype(np.int64),
        "day": main_tf.index.dayofweek.to_numpy().astype(np.int64),
    }
    _ARRAYS_CACHE[key] = out
    return out


def _signals_from_crosses(
    *,
    main_tf_arr: dict,
    fast_line: np.ndarray,
    slow_line: np.ndarray,
    atr_period: int,
    pip_value: float,
) -> SignalSet:
    """Build a SignalSet from fast/slow line crosses. Shared by EMA and MACD."""
    up = (fast_line[1:] > slow_line[1:]) & (fast_line[:-1] <= slow_line[:-1])
    dn = (fast_line[1:] < slow_line[1:]) & (fast_line[:-1] >= slow_line[:-1])
    bars_up = np.where(up)[0] + 1
    bars_dn = np.where(dn)[0] + 1
    bars = np.concatenate([bars_up, bars_dn])
    dirs = np.concatenate(
        [
            np.ones(bars_up.size, dtype=np.int64),
            -np.ones(bars_dn.size, dtype=np.int64),
        ]
    )
    order = np.argsort(bars)
    bars = bars[order].astype(np.int64)
    dirs = dirs[order]
    atr = atr_ema(main_tf_arr["high"], main_tf_arr["low"], main_tf_arr["close"], atr_period)
    return SignalSet(
        bar_index=bars,
        direction=dirs,
        entry_price=main_tf_arr["close"][bars].astype(np.float64),
        atr_pips=(atr[bars] / pip_value).astype(np.float64),
        hour=main_tf_arr["hour"][bars],
        day=main_tf_arr["day"][bars],
    )


# ── Families ───────────────────────────────────────────────────────────


@register("ema_cross")
def ema_cross(main_tf: pd.DataFrame, *, fast: int, slow: int, atr_period: int, pip_value: float) -> SignalSet:
    if fast >= slow:
        raise InvalidCombo(f"ema_cross needs fast < slow, got fast={fast} slow={slow}")
    h = _main_tf_arrays(main_tf)
    return _signals_from_crosses(
        main_tf_arr=h,
        fast_line=ewm(h["close"], fast),
        slow_line=ewm(h["close"], slow),
        atr_period=atr_period,
        pip_value=pip_value,
    )


@register("macd_cross")
def macd_cross(main_tf: pd.DataFrame, *, fast: int, slow: int, signal: int, atr_period: int, pip_value: float) -> SignalSet:
    if fast >= slow:
        raise InvalidCombo(f"macd_cross needs fast < slow, got fast={fast} slow={slow}")
    if signal >= slow:
        raise InvalidCombo(f"macd_cross needs signal < slow, got signal={signal} slow={slow}")
    h = _main_tf_arrays(main_tf)
    macd_line = ewm(h["close"], fast) - ewm(h["close"], slow)
    signal_line = ewm(macd_line, signal)
    return _signals_from_crosses(
        main_tf_arr=h,
        fast_line=macd_line,
        slow_line=signal_line,
        atr_period=atr_period,
        pip_value=pip_value,
    )


@register("donchian")
def donchian(main_tf: pd.DataFrame, *, lookback: int, atr_period: int, pip_value: float) -> SignalSet:
    """Donchian breakout: close above the prior N-bar high → long; below prior N-bar low → short."""
    if lookback < 2:
        raise InvalidCombo(f"donchian needs lookback >= 2, got {lookback}")
    h = _main_tf_arrays(main_tf)
    # Rolling prior max/min over ``lookback`` bars, strictly before current bar.
    highs = pd.Series(h["high"]).shift(1).rolling(lookback).max().to_numpy()
    lows = pd.Series(h["low"]).shift(1).rolling(lookback).min().to_numpy()
    long_breaks = (h["close"] > highs) & ~np.isnan(highs)
    short_breaks = (h["close"] < lows) & ~np.isnan(lows)
    # One signal per fresh breakout edge.
    up = long_breaks & ~np.concatenate([[False], long_breaks[:-1]])
    dn = short_breaks & ~np.concatenate([[False], short_breaks[:-1]])
    bars_up = np.where(up)[0]
    bars_dn = np.where(dn)[0]
    bars = np.concatenate([bars_up, bars_dn])
    dirs = np.concatenate(
        [
            np.ones(bars_up.size, dtype=np.int64),
            -np.ones(bars_dn.size, dtype=np.int64),
        ]
    )
    order = np.argsort(bars)
    bars = bars[order].astype(np.int64)
    dirs = dirs[order]
    atr = atr_ema(h["high"], h["low"], h["close"], atr_period)
    return SignalSet(
        bar_index=bars,
        direction=dirs,
        entry_price=h["close"][bars].astype(np.float64),
        atr_pips=(atr[bars] / pip_value).astype(np.float64),
        hour=h["hour"][bars],
        day=h["day"][bars],
    )


@register("rsi_reversal")
def rsi_reversal(main_tf: pd.DataFrame, *, period: int, lower: int, upper: int, atr_period: int, pip_value: float) -> SignalSet:
    """RSI reversal: crosses back above ``lower`` → long; back below ``upper`` → short."""
    if not (0 < lower < upper < 100):
        raise InvalidCombo(f"rsi_reversal needs 0<lower<upper<100, got lower={lower} upper={upper}")
    if period < 2:
        raise InvalidCombo(f"rsi_reversal needs period >= 2, got {period}")
    h = _main_tf_arrays(main_tf)
    r = rsi(h["close"], period)
    up = (r[1:] > lower) & (r[:-1] <= lower)
    dn = (r[1:] < upper) & (r[:-1] >= upper)
    bars_up = np.where(up)[0] + 1
    bars_dn = np.where(dn)[0] + 1
    bars = np.concatenate([bars_up, bars_dn])
    dirs = np.concatenate(
        [
            np.ones(bars_up.size, dtype=np.int64),
            -np.ones(bars_dn.size, dtype=np.int64),
        ]
    )
    order = np.argsort(bars)
    bars = bars[order].astype(np.int64)
    dirs = dirs[order]
    atr = atr_ema(h["high"], h["low"], h["close"], atr_period)
    return SignalSet(
        bar_index=bars,
        direction=dirs,
        entry_price=h["close"][bars].astype(np.float64),
        atr_pips=(atr[bars] / pip_value).astype(np.float64),
        hour=h["hour"][bars],
        day=h["day"][bars],
    )


# ── Grid iteration and library build ───────────────────────────────────


def _iter_grid(param_spec: dict) -> Iterator[dict]:
    """Cartesian product over {name: Leaf} → iterator of {name: value} dicts.

    Supports IntRange, FloatRange with step, and Choice. Raises if a leaf
    cannot be enumerated (continuous FloatRange).
    """
    keys = list(param_spec.keys())
    value_lists = []
    for k in keys:
        leaf = param_spec[k]
        if not isinstance(leaf, (sc.IntRange, sc.FloatRange, sc.Choice)):
            raise TypeError(f"signal grid entry {k!r} must be a Leaf (IntRange/FloatRange/Choice), got {type(leaf).__name__}")
        value_lists.append(sc.expand(leaf))
    for combo in itertools.product(*value_lists):
        yield dict(zip(keys, combo))


@dataclass
class SignalLibrary:
    """Pooled signal arrays produced by building an EA's signal section."""

    bar_index: np.ndarray
    direction: np.ndarray
    entry_price: np.ndarray
    atr_pips: np.ndarray
    hour: np.ndarray
    day: np.ndarray
    filter_value: np.ndarray
    swing_sl: np.ndarray
    variant: np.ndarray  # int64: per-signal variant id
    variant_map: list[dict]  # variant_id → {family, params, n_signals}

    @property
    def n_signals(self) -> int:
        return self.bar_index.size

    @property
    def n_variants(self) -> int:
        return len(self.variant_map)


# ── Parallel build workers ─────────────────────────────────────────────

# Per-worker state populated by _pool_init. Each worker process holds one
# reference to the main-TF frame so it isn't re-pickled per task.
_WORKER_STATE: dict = {}


def _pool_init(df: pd.DataFrame, pip_value: float, atr_period: int) -> None:
    _WORKER_STATE["df"] = df
    _WORKER_STATE["pip_value"] = float(pip_value)
    _WORKER_STATE["atr_period"] = int(atr_period)


def _pool_worker(family_name: str, combo: dict):
    """Build one variant. Returns a SignalSet or None (InvalidCombo only).

    Families are registered at module import time via ``@register`` decorators,
    so the registry is populated in every spawned worker. 0-signal variants
    are KEPT (empty SignalSet) so variant-id → (family, params) mapping stays
    stable across buffer sizes — the live runner rebuilds the library from a
    trailing window that may not yet contain enough bars to fire every
    long-lookback variant, and dropping those here would make the fingerprint
    resolver in `frozen_trial` / live `_evaluate_and_fire` bail out on every
    tick until the buffer warms up.
    """
    fn = get_family(family_name)
    try:
        ss = fn(
            _WORKER_STATE["df"],
            pip_value=_WORKER_STATE["pip_value"],
            atr_period=_WORKER_STATE["atr_period"],
            **combo,
        )
    except InvalidCombo:
        return None
    return ss


def _parallel_worker_count(total_tasks: int) -> int:
    cpu = os.cpu_count() or 1
    # More workers than tasks wastes startup cost.
    return max(1, min(cpu, total_tasks))


def _should_parallelize(total_est: int) -> bool:
    """Serial path wins on small grids because pool startup eats ~1s on Windows.

    Override with FF_PARALLEL=0 (force off) / FF_PARALLEL=1 (force on), and
    tune the crossover with FF_PARALLEL_MIN (default 500).
    """
    forced = os.environ.get("FF_PARALLEL")
    if forced == "0":
        return False
    if forced == "1":
        return True
    threshold = int(os.environ.get("FF_PARALLEL_MIN", "500"))
    return total_est >= threshold


def _canonical_signals_cfg(signals_cfg: dict) -> str:
    """Deterministic JSON for a signals_cfg dict.

    Uses ``sc.expand`` so the cache key reflects the *actual set of values* the
    grid will enumerate — not just the leaf's textual form.
    """
    out: dict = {}
    for fam in sorted(signals_cfg):
        spec = signals_cfg[fam]
        out[fam] = {}
        for k in sorted(spec):
            leaf = spec[k]
            vals = [round(v, 10) if isinstance(v, float) else v for v in sc.expand(leaf)]
            out[fam][k] = vals
    return json.dumps(out, sort_keys=True, default=str)


def _signal_cache_key(data_path: Path, df_fp: str, signals_cfg: dict, pip_value: float, atr_period: int) -> str:
    try:
        mtime = int(data_path.stat().st_mtime_ns)
        size = int(data_path.stat().st_size)
    except OSError:
        mtime, size = 0, 0
    payload = "|".join(
        [
            _CACHE_VERSION,
            _source_hash(),
            str(data_path.resolve()),
            str(mtime),
            str(size),
            df_fp,
            _canonical_signals_cfg(signals_cfg),
            f"pip={pip_value:.12g}",
            f"atr={int(atr_period)}",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_cached_library(path: Path) -> SignalLibrary | None:
    try:
        with np.load(path, allow_pickle=False) as z:
            variant_map = json.loads(str(z["variant_map_json"]))
            return SignalLibrary(
                bar_index=z["bar_index"].astype(np.int64, copy=False),
                direction=z["direction"].astype(np.int64, copy=False),
                entry_price=z["entry_price"].astype(np.float64, copy=False),
                atr_pips=z["atr_pips"].astype(np.float64, copy=False),
                hour=z["hour"].astype(np.int64, copy=False),
                day=z["day"].astype(np.int64, copy=False),
                filter_value=z["filter_value"].astype(np.float64, copy=False),
                swing_sl=z["swing_sl"].astype(np.float64, copy=False),
                variant=z["variant"].astype(np.int64, copy=False),
                variant_map=variant_map,
            )
    except (OSError, KeyError, ValueError):
        return None


def _save_cached_library(path: Path, lib: SignalLibrary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # np.savez auto-appends '.npz' unless passed a file object, so use open().
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        np.savez(
            f,
            bar_index=lib.bar_index,
            direction=lib.direction,
            entry_price=lib.entry_price,
            atr_pips=lib.atr_pips,
            hour=lib.hour,
            day=lib.day,
            filter_value=lib.filter_value,
            swing_sl=lib.swing_sl,
            variant=lib.variant,
            variant_map_json=np.asarray(json.dumps(lib.variant_map, default=str)),
        )
    os.replace(tmp, path)


def _run_variants_serial(
    tasks: list[tuple[str, dict]],
    main_tf_df: pd.DataFrame,
    pip_value: float,
    atr_period: int,
    total_est: int,
    progress_cb,
) -> list:
    """Serial path: build each variant in order. Returns outcomes list aligned
    with ``tasks`` — SignalSet for kept variants, None for invalid/empty.
    """
    outcomes: list = []
    last_tick = 0.0
    kept = 0
    for i, (family_name, combo) in enumerate(tasks, start=1):
        fn = get_family(family_name)
        try:
            ss = fn(main_tf_df, pip_value=pip_value, atr_period=atr_period, **combo)
        except InvalidCombo:
            outcomes.append(None)
        else:
            # Keep even 0-signal variants so the variant_map covers every
            # (family, params) in the grid. See `_pool_worker` docstring.
            outcomes.append(ss)
            kept += 1
        if progress_cb is not None:
            now = _time.perf_counter()
            if now - last_tick > 0.2:
                last_tick = now
                try:
                    progress_cb(
                        min(0.999, i / max(total_est, 1)),
                        f"building signals {i}/{total_est} ({family_name}: {kept} kept)",
                    )
                except Exception:
                    pass
    return outcomes


def _run_variants_parallel(
    tasks: list[tuple[str, dict]],
    main_tf_df: pd.DataFrame,
    pip_value: float,
    atr_period: int,
    total_est: int,
    progress_cb,
) -> list:
    """Parallel path: fan tasks across worker processes. Order-preserving
    (results slotted by submission index) so variant IDs match serial.
    """
    outcomes: list = [None] * len(tasks)
    workers = _parallel_worker_count(len(tasks))
    last_tick = 0.0
    done = 0
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_pool_init,
        initargs=(main_tf_df, float(pip_value), int(atr_period)),
    ) as pool:
        fut_to_idx = {pool.submit(_pool_worker, family_name, combo): i for i, (family_name, combo) in enumerate(tasks)}
        for fut in as_completed(fut_to_idx):
            i = fut_to_idx[fut]
            outcomes[i] = fut.result()
            done += 1
            if progress_cb is not None:
                now = _time.perf_counter()
                if now - last_tick > 0.2:
                    last_tick = now
                    kept = sum(1 for x in outcomes if x is not None)
                    try:
                        progress_cb(
                            min(0.999, done / max(total_est, 1)),
                            f"building signals {done}/{total_est} (parallel x{workers}: {kept} kept)",
                        )
                    except Exception:
                        pass
    return outcomes


def build_signal_library(
    signals_cfg: dict,
    main_tf_df: pd.DataFrame,
    *,
    pip_value: float,
    atr_period: int,
    progress_cb=None,
    data_path: Path | str | None = None,
    use_cache: bool = True,
) -> SignalLibrary:
    """Expand an EA's ``signals`` config into one pooled library.

    ``signals_cfg`` maps family name → {param_name: Leaf}. Every Cartesian combo
    per family is tried; combos rejected via :class:`InvalidCombo` are skipped
    silently. ``pip_value`` and ``atr_period`` come from the EA's ``execution``
    section and are passed through to every family call.

    ``progress_cb(fraction, message)`` — optional. Called per attempted combo
    as the library builds, so slow runs can show a live bar instead of a
    silent multi-minute pause. Exceptions inside the callback are swallowed.

    ``data_path`` — when given and ``use_cache`` is true (default), results are
    cached on disk at ``artifacts/signal_cache/{key}.npz``. The key binds the
    data file (path + mtime + size), the grid, pip_value, and atr_period, so
    any change invalidates cleanly. Set ``FF_NO_CACHE=1`` to disable globally.
    """
    # Disk cache: check before doing any work. Honour env override for bench runs.
    cache_path: Path | None = None
    if use_cache and data_path is not None and os.environ.get("FF_NO_CACHE") != "1":
        dp = Path(data_path)
        df_fp = _df_fingerprint(main_tf_df)
        key = _signal_cache_key(dp, df_fp, signals_cfg, pip_value, atr_period)
        cache_path = _CACHE_DIR / f"{key}.npz"
        if cache_path.exists():
            hit = _load_cached_library(cache_path)
            if hit is not None:
                if progress_cb is not None:
                    try:
                        progress_cb(
                            0.999,
                            f"signals cached ({hit.n_variants} variants, {hit.n_signals:,} signals)",
                        )
                    except Exception:
                        pass
                return hit

    # Reset per-build caches. See _main_tf_arrays / atr_ema.
    _ARRAYS_CACHE.clear()
    _ATR_CACHE.clear()
    # Estimate up-front so fraction is meaningful.
    try:
        size_est = estimate_library_size(signals_cfg)
        total_est = max(1, int(size_est.get("_total", 1)))
    except Exception:
        total_est = 1

    # Enumerate tasks in the same order the serial loop would — this keeps
    # variant IDs identical between serial and parallel paths.
    tasks: list[tuple[str, dict]] = []
    for family_name, param_spec in signals_cfg.items():
        for combo in _iter_grid(param_spec):
            tasks.append((family_name, combo))

    if _should_parallelize(total_est):
        outcomes = _run_variants_parallel(
            tasks,
            main_tf_df,
            pip_value,
            atr_period,
            total_est,
            progress_cb,
        )
    else:
        outcomes = _run_variants_serial(
            tasks,
            main_tf_df,
            pip_value,
            atr_period,
            total_est,
            progress_cb,
        )

    bar_index_parts: list[np.ndarray] = []
    direction_parts: list[np.ndarray] = []
    entry_price_parts: list[np.ndarray] = []
    atr_pips_parts: list[np.ndarray] = []
    hour_parts: list[np.ndarray] = []
    day_parts: list[np.ndarray] = []
    filter_parts: list[np.ndarray] = []
    swing_parts: list[np.ndarray] = []
    variant_parts: list[np.ndarray] = []
    variant_map: list[dict] = []
    next_id = 0

    for (family_name, combo), ss in zip(tasks, outcomes):
        if ss is None:
            continue
        n = ss.bar_index.size
        bar_index_parts.append(ss.bar_index)
        direction_parts.append(ss.direction)
        entry_price_parts.append(ss.entry_price)
        atr_pips_parts.append(ss.atr_pips)
        hour_parts.append(ss.hour)
        day_parts.append(ss.day)
        filter_parts.append(ss.filter_value)
        swing_parts.append(ss.swing_sl)
        variant_parts.append(np.full(n, next_id, dtype=np.int64))
        variant_map.append({"family": family_name, "params": combo, "n_signals": n})
        next_id += 1

    if next_id == 0:
        raise RuntimeError("build_signal_library: no valid variants produced")

    bar_index = np.concatenate(bar_index_parts)
    # Engine expects signals sorted by bar_index globally; co-sort everything.
    order = np.argsort(bar_index, kind="stable")
    lib = SignalLibrary(
        bar_index=bar_index[order].astype(np.int64, copy=False),
        direction=np.concatenate(direction_parts)[order].astype(np.int64, copy=False),
        entry_price=np.concatenate(entry_price_parts)[order].astype(np.float64, copy=False),
        atr_pips=np.concatenate(atr_pips_parts)[order].astype(np.float64, copy=False),
        hour=np.concatenate(hour_parts)[order].astype(np.int64, copy=False),
        day=np.concatenate(day_parts)[order].astype(np.int64, copy=False),
        filter_value=np.concatenate(filter_parts)[order].astype(np.float64, copy=False),
        swing_sl=np.concatenate(swing_parts)[order].astype(np.float64, copy=False),
        variant=np.concatenate(variant_parts)[order].astype(np.int64, copy=False),
        variant_map=variant_map,
    )
    if cache_path is not None:
        try:
            _save_cached_library(cache_path, lib)
        except OSError:
            pass
    return lib


def estimate_library_size(signals_cfg: dict) -> dict:
    """Estimate the number of combos per family and total, WITHOUT running any
    family function. Does not account for InvalidCombo prunes that depend on
    data (empty variant prunes) or post-expansion rules — but it does count the
    built-in ``fast < slow`` style prunes we can know from the grid alone.

    Returns a dict: {family_name: {combos, raw}, _total: int}.
    """
    out = {}
    total = 0
    for family_name, param_spec in signals_cfg.items():
        raw = 1
        for leaf in param_spec.values():
            raw *= len(sc.expand(leaf))
        # Known a-priori prunes: ema_cross, macd_cross require fast<slow.
        # Conservative estimate: count all valid pairs from the grid.
        combos = 0
        for combo in _iter_grid(param_spec):
            if family_name in ("ema_cross", "macd_cross") and "fast" in combo and "slow" in combo:
                if combo["fast"] >= combo["slow"]:
                    continue
                if family_name == "macd_cross" and "signal" in combo and combo["signal"] >= combo["slow"]:
                    continue
            if family_name == "rsi_reversal" and "lower" in combo and "upper" in combo:
                if not (0 < combo["lower"] < combo["upper"] < 100):
                    continue
            combos += 1
        out[family_name] = {"combos": combos, "raw": raw}
        total += combos
    out["_total"] = total
    return out


# ── Self-test ──────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    cfg = {
        "ema_cross": {
            "fast": sc.IntRange(5, 20, step=5),
            "slow": sc.IntRange(21, 60, step=10),
        },
        "macd_cross": {
            "fast": sc.IntRange(8, 12, step=4),
            "slow": sc.IntRange(20, 30, step=10),
            "signal": sc.IntRange(5, 9, step=4),
        },
        "donchian": {"lookback": sc.IntRange(10, 40, step=10)},
    }
    print("families registered:", list_families())
    est = estimate_library_size(cfg)
    print("estimated grid:", est)
