"""Complex01 — JSON-backed EA.

The schema (``signals`` + ``engine_schema``) lives in ``complex01.json`` so the
web UI can read and save it as plain data. The engine mapping (slot constants
and encoder callables) stays here because it is Python-only.

Effective tunable dimensions: up to ~22 when every group is ON.
"""
from __future__ import annotations

import json
from pathlib import Path

import ff_core as bc

from ff import encoding as enc
from ff.schema_json import dict_to_ea


ENGINE_MAPPING = [
    (bc.PL_SIGNAL_VARIANT, enc.slot_int(("signal_variant",))),

    (bc.PL_SL_MODE, enc.slot_categorical(
        ("engine", "stop_loss", "selector"),
        {"fixed": 0, "atr": 1},
    )),
    (bc.PL_SL_FIXED_PIPS, enc.slot_branch_field(
        ("engine", "stop_loss", "selector"), "fixed",
        ("engine", "stop_loss", "fixed", "pips"),
    )),
    (bc.PL_SL_ATR_MULT, enc.slot_branch_field(
        ("engine", "stop_loss", "selector"), "atr",
        ("engine", "stop_loss", "atr", "mult"),
    )),

    (bc.PL_TP_MODE, enc.slot_categorical(
        ("engine", "take_profit", "selector"),
        {"rr": 0, "atr": 1, "fixed": 2},
    )),
    (bc.PL_TP_RR_RATIO, enc.slot_branch_field(
        ("engine", "take_profit", "selector"), "rr",
        ("engine", "take_profit", "rr", "ratio"),
    )),
    (bc.PL_TP_ATR_MULT, enc.slot_branch_field(
        ("engine", "take_profit", "selector"), "atr",
        ("engine", "take_profit", "atr", "mult"),
    )),
    (bc.PL_TP_FIXED_PIPS, enc.slot_branch_field(
        ("engine", "take_profit", "selector"), "fixed",
        ("engine", "take_profit", "fixed", "pips"),
    )),

    (bc.PL_HOURS_START, enc.slot_if_on(
        ("engine", "session", "test"),
        ("engine", "session", "when_on", "hours_start"),
        default=0,
    )),
    (bc.PL_HOURS_END, enc.slot_if_on(
        ("engine", "session", "test"),
        ("engine", "session", "when_on", "hours_end"),
        default=23,
    )),
    (bc.PL_DAYS_BITMASK, enc.slot_int(("engine", "days"))),

    (bc.PL_TRAILING_MODE, enc.slot_mode_or_off(
        test_path=("engine", "trailing", "test"),
        mode_path=("engine", "trailing", "when_on", "mode", "selector"),
        mode_map={"fixed": 1, "atr": 2},
    )),
    (bc.PL_TRAIL_ACTIVATE, enc.slot_if_on(
        ("engine", "trailing", "test"),
        ("engine", "trailing", "when_on", "activate"),
    )),
    (bc.PL_TRAIL_DISTANCE, enc.slot_branch_field(
        ("engine", "trailing", "when_on", "mode", "selector"), "fixed",
        ("engine", "trailing", "when_on", "mode", "fixed", "distance"),
    )),
    (bc.PL_TRAIL_ATR_MULT, enc.slot_branch_field(
        ("engine", "trailing", "when_on", "mode", "selector"), "atr",
        ("engine", "trailing", "when_on", "mode", "atr", "mult"),
    )),

    (bc.PL_BREAKEVEN_ENABLED, enc.slot_bool_to_int(("engine", "breakeven", "test"))),
    (bc.PL_BREAKEVEN_TRIGGER, enc.slot_if_on(
        ("engine", "breakeven", "test"),
        ("engine", "breakeven", "when_on", "trigger"),
    )),
    (bc.PL_BREAKEVEN_OFFSET, enc.slot_if_on(
        ("engine", "breakeven", "test"),
        ("engine", "breakeven", "when_on", "offset"),
    )),

    (bc.PL_PARTIAL_ENABLED, enc.slot_bool_to_int(("engine", "partial", "test"))),
    (bc.PL_PARTIAL_PCT, enc.slot_if_on(
        ("engine", "partial", "test"),
        ("engine", "partial", "when_on", "pct"),
    )),
    (bc.PL_PARTIAL_TRIGGER, enc.slot_if_on(
        ("engine", "partial", "test"),
        ("engine", "partial", "when_on", "trigger"),
    )),

    (bc.PL_MAX_BARS, enc.slot_if_on(
        ("engine", "max_bars", "test"),
        ("engine", "max_bars", "when_on", "bars"),
        default=0,
    )),

    (bc.PL_STALE_ENABLED, enc.slot_bool_to_int(("engine", "stale", "test"))),
    (bc.PL_STALE_BARS, enc.slot_if_on(
        ("engine", "stale", "test"),
        ("engine", "stale", "when_on", "bars"),
    )),
    (bc.PL_STALE_ATR_THRESH, enc.slot_if_on(
        ("engine", "stale", "test"),
        ("engine", "stale", "when_on", "atr_thresh"),
    )),

    (bc.PL_CHANDELIER_ENABLED, enc.slot_bool_to_int(("engine", "chandelier", "test"))),
    (bc.PL_CHANDELIER_ACTIVATE, enc.slot_if_on(
        ("engine", "chandelier", "test"),
        ("engine", "chandelier", "when_on", "activate"),
        default=-1.0,
    )),
    (bc.PL_CHANDELIER_ATR_MULT, enc.slot_if_on(
        ("engine", "chandelier", "test"),
        ("engine", "chandelier", "when_on", "atr_mult"),
        default=-1.0,
    )),
]


_JSON_PATH = Path(__file__).with_suffix(".json")
EA = dict_to_ea(
    json.loads(_JSON_PATH.read_text(encoding="utf-8")),
    engine_mapping=ENGINE_MAPPING,
)
