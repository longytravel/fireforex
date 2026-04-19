"""Encode a sampled trial dict into a ``(NUM_PL,)`` float64 row for the Rust engine.

The Rust engine expects a flat ``(N_trials, NUM_PL)`` float64 matrix. NUM_PL is
27 at the time of writing. Each slot has a fixed meaning (see audit in the
plan file). Slot values are plain floats; categorical modes are encoded as
integer-valued floats.

An EA declares its mapping as a list of ``(slot_index, encoder_fn)`` pairs.
``encoder_fn`` takes a trial dict and returns a single float64. Helpers in this
module produce common encoder patterns so an EA's mapping stays declarative.

**Rust slot foot-guns encoded as defaults (see audit):**

- ``PL_SIGNAL_VARIANT = -1``: disables variant filtering. Set per-trial.
- ``PL_BUY_FILTER_MAX / PL_SELL_FILTER_MIN = -1``: disables filter (exact-match
  only, no range).
- ``PL_DAYS_BITMASK``: bit 0 = Monday. Value 31 = Mon–Fri.
- ``PL_*_ENABLED``: 0 = off; ``> 0`` = on (use 1.0).
- ``SL_MODE``: 0=FIXED, 1=ATR, 2=SWING (SWING needs swing_sl data).
- ``TP_MODE``: 0=RR, 1=ATR, 2=FIXED.
- ``TRAILING_MODE``: 0=off, 1=fixed_pips, 2=atr_chandelier.
"""
from __future__ import annotations

from typing import Any, Callable

import numpy as np
import ff_core as bc


# ── Slot defaults ──────────────────────────────────────────────────────

ENGINE_DEFAULTS: dict[int, float] = {
    bc.PL_SIGNAL_VARIANT: -1.0,
    bc.PL_BUY_FILTER_MAX: -1.0,
    bc.PL_SELL_FILTER_MIN: -1.0,
    # Pk slots default to -1 (off). Omitting these would leave them at 0.0,
    # which the Rust engine reads as "filter active for value 0" — a silent
    # aggressive-filter trap. See docs/validation/2026-04-19-signal-filters/.
    **{bc.PL_SIGNAL_P0 + i: -1.0 for i in range(bc.NUM_SIGNAL_PARAMS)},
}


# ── Path access ────────────────────────────────────────────────────────

_MISSING = object()


def _get(trial: dict, path: tuple[str, ...], default: Any = _MISSING) -> Any:
    """Walk a dict by path. Return ``default`` if any key missing; ``MISSING`` → KeyError."""
    node: Any = trial
    for key in path:
        if not isinstance(node, dict) or key not in node:
            if default is _MISSING:
                raise KeyError(f"trial path not found: {'.'.join(path)}")
            return default
        node = node[key]
    return node


# ── Encoder helpers (used by EA mapping tables) ────────────────────────

def slot_const(value: float) -> Callable[[dict], float]:
    """Always write a constant value (e.g. a slot you never vary)."""
    v = float(value)
    def f(_trial: dict) -> float:
        return v
    return f


def slot_float(path: tuple[str, ...], default: float = 0.0) -> Callable[[dict], float]:
    """Write the float value at ``path``, or ``default`` if absent."""
    def f(trial: dict) -> float:
        v = _get(trial, path, default)
        return float(v) if v is not None else float(default)
    return f


def slot_int(path: tuple[str, ...], default: int = 0) -> Callable[[dict], float]:
    """Write the int value at ``path`` as float, or ``default`` if absent."""
    def f(trial: dict) -> float:
        v = _get(trial, path, default)
        return float(int(v)) if v is not None else float(default)
    return f


def slot_categorical(path: tuple[str, ...], mapping: dict, default: float = 0.0) -> Callable[[dict], float]:
    """Look up value at ``path``, translate via ``mapping`` to a number."""
    m = {k: float(v) for k, v in mapping.items()}
    def f(trial: dict) -> float:
        v = _get(trial, path, None)
        if v is None or v not in m:
            return float(default)
        return m[v]
    return f


def slot_bool_to_int(path: tuple[str, ...], on_value: Any = True) -> Callable[[dict], float]:
    """Map a boolean-like value at ``path`` to 1.0 if == ``on_value``, else 0.0."""
    def f(trial: dict) -> float:
        v = _get(trial, path, None)
        return 1.0 if v == on_value else 0.0
    return f


def slot_mode_or_off(test_path: tuple[str, ...], mode_path: tuple[str, ...] | None = None,
                     mode_map: dict | None = None, off_value: float = 0.0,
                     on_value: Any = True, default_on: float = 1.0) -> Callable[[dict], float]:
    """If test value equals ``on_value``: return mapped mode (or ``default_on``).
    Otherwise return ``off_value``.

    Useful for TRAILING_MODE where the group's on/off switch and the mode
    selector together determine the engine slot value.
    """
    m = {k: float(v) for k, v in (mode_map or {}).items()}
    def f(trial: dict) -> float:
        if _get(trial, test_path, None) != on_value:
            return float(off_value)
        if mode_path is None or not m:
            return float(default_on)
        mode = _get(trial, mode_path, None)
        return m.get(mode, float(off_value))
    return f


def slot_if_on(test_path: tuple[str, ...], value_path: tuple[str, ...], default: float = 0.0,
               on_value: Any = True) -> Callable[[dict], float]:
    """If test value equals ``on_value``: return float at ``value_path``.
    Otherwise return ``default``."""
    def f(trial: dict) -> float:
        if _get(trial, test_path, None) != on_value:
            return float(default)
        v = _get(trial, value_path, None)
        return float(v) if v is not None else float(default)
    return f


def slot_branch_field(sel_path: tuple[str, ...], arm: Any, value_path: tuple[str, ...],
                      default: float = 0.0) -> Callable[[dict], float]:
    """If selector at ``sel_path`` equals ``arm``: return float at ``value_path``.
    Otherwise return ``default``."""
    def f(trial: dict) -> float:
        if _get(trial, sel_path, None) != arm:
            return float(default)
        v = _get(trial, value_path, None)
        return float(v) if v is not None else float(default)
    return f


# ── Encoder ────────────────────────────────────────────────────────────

def encode(trials: list[dict], engine_mapping: list[tuple[int, Callable[[dict], float]]]) -> np.ndarray:
    """Build the ``(N, NUM_PL)`` float64 matrix the Rust engine expects.

    Unmapped slots default to 0.0 unless listed in :data:`ENGINE_DEFAULTS`.
    Mapped slots override defaults.
    """
    n = len(trials)
    pm = np.zeros((n, bc.NUM_PL), dtype=np.float64)
    for slot_idx, default in ENGINE_DEFAULTS.items():
        pm[:, slot_idx] = default
    mapped_slots = {slot for slot, _ in engine_mapping}
    # Sanity check: warn in test mode if a slot is mapped twice (bug in EA config).
    if len(mapped_slots) != len(engine_mapping):
        seen: set[int] = set()
        for slot, _ in engine_mapping:
            if slot in seen:
                raise ValueError(f"encode(): slot {slot} mapped more than once")
            seen.add(slot)
    for i, trial in enumerate(trials):
        for slot_idx, fn in engine_mapping:
            pm[i, slot_idx] = fn(trial)
    return pm


# ── Self-test ──────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    # Fake mini-trial to exercise each helper.
    trials = [
        {
            "signal_variant": 0,
            "engine": {
                "stop_loss": {"selector": "atr", "atr": {"mult": 1.5}, "fixed_pips": {"pips": 20.0}},
                "take_profit": {"selector": "rr", "rr": {"ratio": 2.0}},
                "trailing": {"test": False, "when_on": {}},
                "breakeven": {"test": True, "when_on": {"trigger": 15.0, "offset": 2.0}},
                "days": 31,
            },
        },
        {
            "signal_variant": 2,
            "engine": {
                "stop_loss": {"selector": "fixed", "fixed": {"pips": 30.0}, "atr": {"mult": 0.0}},
                "take_profit": {"selector": "atr", "atr": {"mult": 3.0}},
                "trailing": {"test": True, "when_on": {"mode": {"selector": "atr"},
                                                       "activate": 25.0,
                                                       "fixed": {"distance": 10.0},
                                                       "atr": {"mult": 1.2}}},
                "breakeven": {"test": False, "when_on": {}},
                "days": 127,
            },
        },
    ]
    engine_mapping = [
        (bc.PL_SIGNAL_VARIANT, slot_int(("signal_variant",))),
        (bc.PL_SL_MODE, slot_categorical(("engine", "stop_loss", "selector"),
                                         {"fixed": 0, "atr": 1})),
        (bc.PL_SL_FIXED_PIPS, slot_branch_field(("engine", "stop_loss", "selector"),
                                                "fixed", ("engine", "stop_loss", "fixed", "pips"))),
        (bc.PL_SL_ATR_MULT, slot_branch_field(("engine", "stop_loss", "selector"),
                                              "atr", ("engine", "stop_loss", "atr", "mult"))),
        (bc.PL_TP_MODE, slot_categorical(("engine", "take_profit", "selector"),
                                         {"rr": 0, "atr": 1, "fixed": 2})),
        (bc.PL_TP_RR_RATIO, slot_branch_field(("engine", "take_profit", "selector"),
                                              "rr", ("engine", "take_profit", "rr", "ratio"))),
        (bc.PL_TP_ATR_MULT, slot_branch_field(("engine", "take_profit", "selector"),
                                              "atr", ("engine", "take_profit", "atr", "mult"))),
        (bc.PL_TRAILING_MODE, slot_mode_or_off(
            test_path=("engine", "trailing", "test"),
            mode_path=("engine", "trailing", "when_on", "mode", "selector"),
            mode_map={"fixed": 1, "atr": 2},
        )),
        (bc.PL_TRAIL_ACTIVATE, slot_if_on(("engine", "trailing", "test"),
                                          ("engine", "trailing", "when_on", "activate"))),
        (bc.PL_BREAKEVEN_ENABLED, slot_bool_to_int(("engine", "breakeven", "test"))),
        (bc.PL_BREAKEVEN_TRIGGER, slot_if_on(("engine", "breakeven", "test"),
                                             ("engine", "breakeven", "when_on", "trigger"))),
        (bc.PL_BREAKEVEN_OFFSET, slot_if_on(("engine", "breakeven", "test"),
                                            ("engine", "breakeven", "when_on", "offset"))),
        (bc.PL_DAYS_BITMASK, slot_int(("engine", "days"))),
    ]
    pm = encode(trials, engine_mapping)
    print(f"matrix shape: {pm.shape}")
    for i, row in enumerate(pm):
        print(f"trial {i}:")
        print(f"  variant={row[bc.PL_SIGNAL_VARIANT]:.0f}  "
              f"sl_mode={row[bc.PL_SL_MODE]:.0f}  sl_fixed={row[bc.PL_SL_FIXED_PIPS]:.1f}  "
              f"sl_atr={row[bc.PL_SL_ATR_MULT]:.2f}")
        print(f"  tp_mode={row[bc.PL_TP_MODE]:.0f}  tp_rr={row[bc.PL_TP_RR_RATIO]:.2f}  "
              f"tp_atr={row[bc.PL_TP_ATR_MULT]:.2f}")
        print(f"  trail_mode={row[bc.PL_TRAILING_MODE]:.0f}  trail_activate={row[bc.PL_TRAIL_ACTIVATE]:.1f}")
        print(f"  be_enabled={row[bc.PL_BREAKEVEN_ENABLED]:.0f}  "
              f"be_trigger={row[bc.PL_BREAKEVEN_TRIGGER]:.1f}  be_offset={row[bc.PL_BREAKEVEN_OFFSET]:.1f}")
        print(f"  days={row[bc.PL_DAYS_BITMASK]:.0f}  "
              f"buy_filter={row[bc.PL_BUY_FILTER_MAX]:.1f}  sell_filter={row[bc.PL_SELL_FILTER_MIN]:.1f}")
    # Expected:
    #   trial 0: variant=0, sl_mode=1 sl_atr=1.5, tp_rr=2.0, trail_mode=0, be_enabled=1 be_trigger=15
    #   trial 1: variant=2, sl_mode=0 sl_fixed=30, tp_mode=1 tp_atr=3.0, trail_mode=2 activate=25, be=0
    print("encoding.py: OK")
