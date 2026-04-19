"""Baseline EA — JSON-backed.

Schema in ``baseline.json``; engine mapping stays here (Python-only).
"""
from __future__ import annotations

import json
from pathlib import Path

import ff_core as bc

from ff import encoding as enc
from ff.schema_json import dict_to_ea


ENGINE_MAPPING = [
    (bc.PL_SIGNAL_VARIANT, enc.slot_int(("signal_variant",))),
    (bc.PL_SL_MODE,        enc.slot_const(1)),     # ATR
    (bc.PL_SL_ATR_MULT,    enc.slot_float(("engine", "sl_atr_mult"))),
    (bc.PL_TP_MODE,        enc.slot_const(0)),     # RR
    (bc.PL_TP_RR_RATIO,    enc.slot_float(("engine", "tp_rr_ratio"))),
    (bc.PL_DAYS_BITMASK,   enc.slot_const(31)),    # Mon-Fri
    (bc.PL_HOURS_START,    enc.slot_const(0)),
    (bc.PL_HOURS_END,      enc.slot_const(23)),
]


_JSON_PATH = Path(__file__).with_suffix(".json")
EA = dict_to_ea(
    json.loads(_JSON_PATH.read_text(encoding="utf-8")),
    engine_mapping=ENGINE_MAPPING,
)
