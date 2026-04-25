"""complexity_to_ea — build a Fire Forex EA dict from a 1..10 "complexity" level.

Reads ``pair_tf.yaml`` (a hand-written table of per-(pair, main_tf) knob ranges)
and produces an EA dict of the exact shape consumed by ``ff.harness.run`` —
i.e. a subset of the ``eas/complex01`` schema, with optional groups present
only when the requested complexity level enables them.

The resulting ``engine_mapping`` is generated from the ``engine_schema`` via
``build_standard_mapping`` so that any (pair, tf, level) combination produces
a structurally-valid EA whose mapping is a strict subset of complex01's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import ff_core as bc
import yaml

from ff import encoding as enc
from ff.schema import Branch, Choice, FloatRange, Group, IntRange

# ── YAML loader (cached) ───────────────────────────────────────────────

_YAML_PATH = Path(__file__).with_name("pair_tf.yaml")
_YAML_CACHE: dict[str, Any] | None = None


def _load_yaml() -> dict[str, Any]:
    global _YAML_CACHE
    if _YAML_CACHE is None:
        _YAML_CACHE = yaml.safe_load(_YAML_PATH.read_text(encoding="utf-8"))
    return _YAML_CACHE


def _resolve_ranges(pair: str, main_tf: str) -> dict[str, Any]:
    """Return per-knob ranges for (pair, main_tf).

    First tries the data-driven derivation in ``volatility.derive_ranges``
    (uses median 14-bar ATR in pips from the parquet file, cached). Falls
    back to the hand-written YAML if the data isn't available.
    """
    try:
        from . import volatility

        derived = volatility.derive_ranges(pair, main_tf)
    except Exception:
        derived = None
    if derived is not None:
        return derived

    y = _load_yaml()
    if pair in y:
        pair_block = y[pair]
        if main_tf in pair_block:
            return pair_block[main_tf]
        first_tf = next(iter(pair_block))
        return pair_block[first_tf]
    if "_default" in y:
        return y["_default"]
    raise ValueError(f"no volatility data and no pair_tf.yaml entry for {pair!r}/{main_tf!r}")


# ── Leaf-builder helpers (step granularity per complexity level) ───────


def _float_step(level: int, lo: float, hi: float) -> float:
    """Return a step size for numeric knobs.

    We always emit a concrete step so the UI shows a number (never "cont."),
    and the UI's step-granularity slider can scale it visibly. The size targets
    roughly 10–40 grid points across the range depending on level.
    """
    span = max(1e-9, float(hi) - float(lo))
    # grid points targeted
    if level <= 2:
        n = 8
    elif level <= 4:
        n = 12
    elif level <= 6:
        n = 20
    elif level <= 8:
        n = 30
    else:
        n = 50
    step = span / n
    # round to a tidy 1/2/5 × 10^k
    import math

    if step <= 0:
        return 1.0
    k = math.floor(math.log10(step))
    base = step / (10**k)
    if base < 1.5:
        nice = 1
    elif base < 3.5:
        nice = 2
    elif base < 7.5:
        nice = 5
    else:
        nice = 10
    return nice * (10**k)


def _int_step(level: int, span: int) -> int:
    """Step granularity for IntRange knobs: coarser at low levels."""
    if level <= 2:
        return max(1, span // 6)  # ~6 grid points
    if level <= 4:
        return max(1, span // 10)  # ~10
    if level <= 6:
        return max(1, span // 15)  # ~15
    if level <= 8:
        return max(1, span // 20)  # ~20
    return max(1, span // 30)  # fine


def _fr(ranges_entry: dict, scale: str = "linear", level: int = 6) -> FloatRange:
    lo = float(ranges_entry["min"])
    hi = float(ranges_entry["max"])
    return FloatRange(min=lo, max=hi, scale=scale, step=_float_step(level, lo, hi))


def _ir(lo: int, hi: int, level: int) -> IntRange:
    return IntRange(min=int(lo), max=int(hi), step=_int_step(level, int(hi) - int(lo)))


# ── Engine-schema builders (one per group) ─────────────────────────────


def _build_stop_loss(level: int, r: dict) -> Any:
    """Level 1–2: Choice[fixed] only. Level 3+: Branch fixed/atr."""
    if level <= 2:
        return Branch(
            selector=Choice(["fixed"]),
            arms={
                "fixed": {"pips": _fr(r["fixed_sl_pips"], scale="log")},
            },
        )
    return Branch(
        selector=Choice(["fixed", "atr"]),
        arms={
            "fixed": {"pips": _fr(r["fixed_sl_pips"], scale="log")},
            "atr": {"mult": _fr(r["atr_mult_sl"], scale="linear")},
        },
    )


def _build_take_profit(level: int, r: dict) -> Any:
    """Level 1: fixed only. Level 2: rr only. Level 3+: rr+atr+fixed."""
    if level == 1:
        return Branch(
            selector=Choice(["fixed"]),
            arms={
                "fixed": {"pips": _fr(r["fixed_tp_pips"], scale="log")},
            },
        )
    if level == 2:
        return Branch(
            selector=Choice(["rr"]),
            arms={
                "rr": {"ratio": _fr(r["rr_ratio"], scale="linear")},
            },
        )
    return Branch(
        selector=Choice(["rr", "atr", "fixed"]),
        arms={
            "rr": {"ratio": _fr(r["rr_ratio"], scale="linear")},
            "atr": {"mult": _fr(r["atr_mult_tp"], scale="linear")},
            "fixed": {"pips": _fr(r["fixed_tp_pips"], scale="log")},
        },
    )


def _build_trailing(level: int, r: dict) -> Group:
    dist_lo = max(5.0, float(r["fixed_sl_pips"]["min"]))
    dist_hi = max(dist_lo + 1.0, float(r["fixed_sl_pips"]["max"]) * 0.6)
    return Group(
        test=Choice([True, False]),
        on_value=True,
        when_on={
            "mode": Branch(
                selector=Choice(["fixed", "atr"]),
                arms={
                    "fixed": {"distance": FloatRange(dist_lo, dist_hi, scale="log", step=_float_step(level, dist_lo, dist_hi))},
                    "atr": {"mult": _fr(r["trail_atr_mult"], scale="linear")},
                },
            ),
            "activate": _fr(r["trail_activation_pips"], scale="log"),
        },
    )


def _build_breakeven(level: int, r: dict) -> Group:
    trig_hi = max(10.0, float(r["fixed_sl_pips"]["max"]) * 0.5)
    return Group(
        test=Choice([True, False]),
        on_value=True,
        when_on={
            "trigger": FloatRange(5.0, trig_hi, scale="log", step=_float_step(level, 5.0, trig_hi)),
            "offset": FloatRange(-2.0, 10.0, scale="linear", step=_float_step(level, -2.0, 10.0)),
        },
    )


def _build_partial(level: int, r: dict) -> Group:
    trig_hi = max(15.0, float(r["fixed_sl_pips"]["max"]) * 0.8)
    return Group(
        test=Choice([True, False]),
        on_value=True,
        when_on={
            "pct": FloatRange(20.0, 75.0, scale="linear", step=_float_step(level, 20.0, 75.0)),
            "trigger": FloatRange(5.0, trig_hi, scale="log", step=_float_step(level, 5.0, trig_hi)),
        },
    )


def _build_chandelier(level: int, r: dict) -> Group:
    return Group(
        test=Choice([True, False]),
        on_value=True,
        when_on={
            "activate": FloatRange(5.0, 25.0, scale="log", step=_float_step(level, 5.0, 25.0)),
            "atr_mult": FloatRange(2.0, 4.0, scale="linear", step=_float_step(level, 2.0, 4.0)),
        },
    )


def _build_stale(level: int, r: dict) -> Group:
    step = _int_step(level, 180)
    return Group(
        test=Choice([True, False]),
        on_value=True,
        when_on={
            "bars": IntRange(20, 200, step=step),
            "atr_thresh": FloatRange(0.3, 2.5, scale="linear", step=_float_step(level, 0.3, 2.5)),
        },
    )


def _build_session(level: int, r: dict) -> Group:
    return Group(
        test=Choice([True, False]),
        on_value=True,
        when_on={
            "hours_start": IntRange(0, 23, step=1),
            "hours_end": IntRange(0, 23, step=1),
        },
    )


def _build_max_bars(level: int, r: dict) -> Group:
    step = _int_step(level, 450)
    return Group(
        test=Choice([True, False]),
        on_value=True,
        when_on={
            "bars": IntRange(48, 500, step=step),
        },
    )


def _build_days(level: int) -> Choice:
    """Level 1–3: fixed Mon-Fri (31). Level 4+: Choice over common bitmasks."""
    if level <= 3:
        return Choice([31])  # Mon-Fri only; const
    return Choice([31, 63, 127])  # Mon-Fri, Mon-Sat, Mon-Sun


# ── Signals (ema_cross / macd_cross / donchian) ────────────────────────


def _build_signals(level: int, r: dict) -> dict:
    ema_fast = r["ema_fast"]
    ema_slow = r["ema_slow"]

    signals: dict[str, Any] = {
        "ema_cross": {
            "fast": _ir(ema_fast["min"], ema_fast["max"], level),
            "slow": _ir(ema_slow["min"], ema_slow["max"], level),
        },
    }
    if level >= 3:
        signals["macd_cross"] = {
            "fast": _ir(8, 16, level),
            "slow": _ir(24, 48, level),
            "signal": _ir(5, 9, level),
        }
    if level >= 7:
        signals["donchian"] = {
            "lookback": _ir(20, 120, level),
        }
    return signals


# ── Optional-group gating per complexity level ─────────────────────────


def _optional_keys_for_level(level: int) -> set[str]:
    """Which optional groups are present in engine_schema at this level."""
    keys: set[str] = set()
    if level >= 4:
        keys.add("trailing")
        keys.add("max_bars")
    if level >= 5:
        keys.add("breakeven")
    if level >= 6:
        keys.add("partial")
    if level >= 7:
        keys.add("stale")
        keys.add("session")
    if level >= 5:
        keys.add("chandelier")
    return keys


def _build_engine_schema(level: int, r: dict) -> dict:
    schema: dict[str, Any] = {
        "stop_loss": _build_stop_loss(level, r),
        "take_profit": _build_take_profit(level, r),
    }
    opt = _optional_keys_for_level(level)
    # Stop-management cluster — ordered so the UI renders them together.
    if "trailing" in opt:
        schema["trailing"] = _build_trailing(level, r)
    if "chandelier" in opt:
        schema["chandelier"] = _build_chandelier(level, r)
    if "breakeven" in opt:
        schema["breakeven"] = _build_breakeven(level, r)
    # Trade-lifecycle cluster.
    if "partial" in opt:
        schema["partial"] = _build_partial(level, r)
    if "stale" in opt:
        schema["stale"] = _build_stale(level, r)
    if "session" in opt:
        schema["session"] = _build_session(level, r)
    if "max_bars" in opt:
        schema["max_bars"] = _build_max_bars(level, r)
    schema["days"] = _build_days(level)
    return schema


# ── Mapping builder (data-driven, matches complex01 exactly) ───────────


def build_standard_mapping(engine_schema: dict) -> list:
    """Emit the (PL_*, encoder) tuples for every knob present in schema.

    The output is a strict subset of ``eas.complex01.ENGINE_MAPPING`` shaped
    to whatever engine_schema is supplied. Always includes PL_SIGNAL_VARIANT
    (the harness needs it) and PL_DAYS_BITMASK (always present in schema).
    """
    mapping: list = [
        (bc.PL_SIGNAL_VARIANT, enc.slot_int(("signal_variant",))),
    ]

    # Stop-loss: Branch (full) or Choice["fixed"] (level 1–2).
    sl = engine_schema["stop_loss"]
    if isinstance(sl, Branch):
        arms = set(sl.arms.keys())
        if arms == {"fixed"}:
            mapping.append(
                (
                    bc.PL_SL_MODE,
                    enc.slot_categorical(("engine", "stop_loss", "selector"), {"fixed": 0}),
                )
            )
            mapping.append(
                (
                    bc.PL_SL_FIXED_PIPS,
                    enc.slot_branch_field(
                        ("engine", "stop_loss", "selector"),
                        "fixed",
                        ("engine", "stop_loss", "fixed", "pips"),
                    ),
                )
            )
        else:
            mapping.append(
                (
                    bc.PL_SL_MODE,
                    enc.slot_categorical(("engine", "stop_loss", "selector"), {"fixed": 0, "atr": 1}),
                )
            )
            mapping.append(
                (
                    bc.PL_SL_FIXED_PIPS,
                    enc.slot_branch_field(
                        ("engine", "stop_loss", "selector"),
                        "fixed",
                        ("engine", "stop_loss", "fixed", "pips"),
                    ),
                )
            )
            mapping.append(
                (
                    bc.PL_SL_ATR_MULT,
                    enc.slot_branch_field(
                        ("engine", "stop_loss", "selector"),
                        "atr",
                        ("engine", "stop_loss", "atr", "mult"),
                    ),
                )
            )

    # Take-profit: Branch with arms rr/atr/fixed, rr only, or fixed only.
    tp = engine_schema["take_profit"]
    if isinstance(tp, Branch):
        arms = set(tp.arms.keys())
        if arms == {"rr"}:
            mapping.append(
                (
                    bc.PL_TP_MODE,
                    enc.slot_categorical(("engine", "take_profit", "selector"), {"rr": 0}),
                )
            )
            mapping.append(
                (
                    bc.PL_TP_RR_RATIO,
                    enc.slot_branch_field(
                        ("engine", "take_profit", "selector"),
                        "rr",
                        ("engine", "take_profit", "rr", "ratio"),
                    ),
                )
            )
        elif arms == {"fixed"}:
            mapping.append(
                (
                    bc.PL_TP_MODE,
                    enc.slot_categorical(("engine", "take_profit", "selector"), {"fixed": 2}),
                )
            )
            mapping.append(
                (
                    bc.PL_TP_FIXED_PIPS,
                    enc.slot_branch_field(
                        ("engine", "take_profit", "selector"),
                        "fixed",
                        ("engine", "take_profit", "fixed", "pips"),
                    ),
                )
            )
        else:
            mapping.append(
                (
                    bc.PL_TP_MODE,
                    enc.slot_categorical(
                        ("engine", "take_profit", "selector"),
                        {"rr": 0, "atr": 1, "fixed": 2},
                    ),
                )
            )
            mapping.append(
                (
                    bc.PL_TP_RR_RATIO,
                    enc.slot_branch_field(
                        ("engine", "take_profit", "selector"),
                        "rr",
                        ("engine", "take_profit", "rr", "ratio"),
                    ),
                )
            )
            mapping.append(
                (
                    bc.PL_TP_ATR_MULT,
                    enc.slot_branch_field(
                        ("engine", "take_profit", "selector"),
                        "atr",
                        ("engine", "take_profit", "atr", "mult"),
                    ),
                )
            )
            mapping.append(
                (
                    bc.PL_TP_FIXED_PIPS,
                    enc.slot_branch_field(
                        ("engine", "take_profit", "selector"),
                        "fixed",
                        ("engine", "take_profit", "fixed", "pips"),
                    ),
                )
            )

    # Session (optional) — when absent, engine gets hours_start=0, hours_end=23.
    if "session" in engine_schema:
        mapping.append(
            (
                bc.PL_HOURS_START,
                enc.slot_if_on(
                    ("engine", "session", "test"),
                    ("engine", "session", "when_on", "hours_start"),
                    default=0,
                ),
            )
        )
        mapping.append(
            (
                bc.PL_HOURS_END,
                enc.slot_if_on(
                    ("engine", "session", "test"),
                    ("engine", "session", "when_on", "hours_end"),
                    default=23,
                ),
            )
        )

    # Days bitmask — always present.
    mapping.append((bc.PL_DAYS_BITMASK, enc.slot_int(("engine", "days"))))

    # Trailing (optional).
    if "trailing" in engine_schema:
        mapping.append(
            (
                bc.PL_TRAILING_MODE,
                enc.slot_mode_or_off(
                    test_path=("engine", "trailing", "test"),
                    mode_path=("engine", "trailing", "when_on", "mode", "selector"),
                    mode_map={"fixed": 1, "atr": 2},
                ),
            )
        )
        mapping.append(
            (
                bc.PL_TRAIL_ACTIVATE,
                enc.slot_if_on(
                    ("engine", "trailing", "test"),
                    ("engine", "trailing", "when_on", "activate"),
                ),
            )
        )
        mapping.append(
            (
                bc.PL_TRAIL_DISTANCE,
                enc.slot_branch_field(
                    ("engine", "trailing", "when_on", "mode", "selector"),
                    "fixed",
                    ("engine", "trailing", "when_on", "mode", "fixed", "distance"),
                ),
            )
        )
        mapping.append(
            (
                bc.PL_TRAIL_ATR_MULT,
                enc.slot_branch_field(
                    ("engine", "trailing", "when_on", "mode", "selector"),
                    "atr",
                    ("engine", "trailing", "when_on", "mode", "atr", "mult"),
                ),
            )
        )

    # Breakeven (optional).
    if "breakeven" in engine_schema:
        mapping.append((bc.PL_BREAKEVEN_ENABLED, enc.slot_bool_to_int(("engine", "breakeven", "test"))))
        mapping.append(
            (
                bc.PL_BREAKEVEN_TRIGGER,
                enc.slot_if_on(
                    ("engine", "breakeven", "test"),
                    ("engine", "breakeven", "when_on", "trigger"),
                ),
            )
        )
        mapping.append(
            (
                bc.PL_BREAKEVEN_OFFSET,
                enc.slot_if_on(
                    ("engine", "breakeven", "test"),
                    ("engine", "breakeven", "when_on", "offset"),
                ),
            )
        )

    # Partial (optional).
    if "partial" in engine_schema:
        mapping.append((bc.PL_PARTIAL_ENABLED, enc.slot_bool_to_int(("engine", "partial", "test"))))
        mapping.append(
            (
                bc.PL_PARTIAL_PCT,
                enc.slot_if_on(
                    ("engine", "partial", "test"),
                    ("engine", "partial", "when_on", "pct"),
                ),
            )
        )
        mapping.append(
            (
                bc.PL_PARTIAL_TRIGGER,
                enc.slot_if_on(
                    ("engine", "partial", "test"),
                    ("engine", "partial", "when_on", "trigger"),
                ),
            )
        )

    # Max bars (optional).
    if "max_bars" in engine_schema:
        mapping.append(
            (
                bc.PL_MAX_BARS,
                enc.slot_if_on(
                    ("engine", "max_bars", "test"),
                    ("engine", "max_bars", "when_on", "bars"),
                    default=0,
                ),
            )
        )

    # Stale (optional).
    if "stale" in engine_schema:
        mapping.append((bc.PL_STALE_ENABLED, enc.slot_bool_to_int(("engine", "stale", "test"))))
        mapping.append(
            (
                bc.PL_STALE_BARS,
                enc.slot_if_on(
                    ("engine", "stale", "test"),
                    ("engine", "stale", "when_on", "bars"),
                ),
            )
        )
        mapping.append(
            (
                bc.PL_STALE_ATR_THRESH,
                enc.slot_if_on(
                    ("engine", "stale", "test"),
                    ("engine", "stale", "when_on", "atr_thresh"),
                ),
            )
        )

    # Chandelier (optional).
    if "chandelier" in engine_schema:
        mapping.append((bc.PL_CHANDELIER_ENABLED, enc.slot_bool_to_int(("engine", "chandelier", "test"))))
        mapping.append(
            (
                bc.PL_CHANDELIER_ACTIVATE,
                enc.slot_if_on(
                    ("engine", "chandelier", "test"),
                    ("engine", "chandelier", "when_on", "activate"),
                    default=-1.0,
                ),
            )
        )
        mapping.append(
            (
                bc.PL_CHANDELIER_ATR_MULT,
                enc.slot_if_on(
                    ("engine", "chandelier", "test"),
                    ("engine", "chandelier", "when_on", "atr_mult"),
                    default=-1.0,
                ),
            )
        )

    return mapping


# ── Public API ─────────────────────────────────────────────────────────


def complexity_to_ea(
    level: int,
    pair: str,
    main_tf: str,
    sub_tf: str | None = None,
    name: str | None = None,
) -> dict:
    """Build a complete Fire Forex EA dict for the given complexity level.

    Parameters
    ----------
    level : int
        Complexity 1..10. Higher levels enable more optional groups and finer
        sampling granularity.
    pair : str
        e.g. ``"EUR_USD"``.
    main_tf : str
        e.g. ``"H1"``.
    sub_tf : str | None
        Sub-timeframe for intrabar replay. Defaults to the YAML value.
    name : str | None
        EA name. Defaults to ``f"complexity_L{level}_{pair}_{main_tf}"``.

    Returns
    -------
    dict
        An EA dict ready for ``ff.harness.run()`` / ``ff.preflight.preflight_report()``.
    """
    if not 1 <= level <= 10:
        raise ValueError(f"level must be in 1..10, got {level}")

    r = _resolve_ranges(pair, main_tf)
    effective_sub_tf = sub_tf if sub_tf is not None else r.get("sub_tf", "M1")
    effective_name = name if name is not None else f"complexity_L{level}_{pair}_{main_tf}"

    engine_schema = _build_engine_schema(level, r)
    engine_mapping = build_standard_mapping(engine_schema)
    signals = _build_signals(level, r)

    return {
        "name": effective_name,
        "data": {
            "pair": pair,
            "main_tf": main_tf,
            "sub_tf": effective_sub_tf,
        },
        "execution": {
            "pip_value": None,
            "commission_pips": 0.3,
            "max_spread_pips": 10.0,
            "slippage_pips": 0.0,
            "atr_period": 14,
        },
        "signals": signals,
        "engine_schema": engine_schema,
        "engine_mapping": engine_mapping,
    }
