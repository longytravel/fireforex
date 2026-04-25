"""JSON (de)serialisation for EA schema primitives.

The web UI needs to save/load EAs as plain data — Python dataclasses like
``FloatRange`` and ``Branch`` don't survive ``json.dumps``. This module bridges
them.

**Scope:** the user-visible parts of an EA — ``name``, ``data``, ``execution``,
``signals``, ``engine_schema``. The ``engine_mapping`` list references
``ff_core`` slot constants and encoder callables; it is NOT serialised
here. Callers supply it separately via ``dict_to_ea(..., engine_mapping=...)``.

Node JSON shape:

- ``FloatRange`` → ``{"type": "FloatRange", "min": .., "max": .., "scale": "linear"|"log", "step": ..?}``
- ``IntRange``   → ``{"type": "IntRange",   "min": .., "max": .., "step": ..}``
- ``Choice``     → ``{"type": "Choice",     "values": [..]}``
- ``Group``      → ``{"type": "Group",      "test": <node>, "on_value": .., "when_on": <subtree>}``
- ``Branch``     → ``{"type": "Branch",     "selector": <node>, "arms": {name: <subtree>}}``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schema import Branch, Choice, FloatRange, Group, IntRange

_NODE_TYPES = (FloatRange, IntRange, Choice, Group, Branch)
_NODE_TYPE_NAMES = {"FloatRange", "IntRange", "Choice", "Group", "Branch"}
_SERIALISABLE_TOP_KEYS = ("name", "data", "execution", "signals", "engine_schema")


# ── Node ↔ dict ────────────────────────────────────────────────────────


def schema_node_to_dict(node: Any) -> dict:
    if isinstance(node, FloatRange):
        d: dict = {"type": "FloatRange", "min": node.min, "max": node.max, "scale": node.scale}
        if node.step is not None:
            d["step"] = node.step
        return d
    if isinstance(node, IntRange):
        return {"type": "IntRange", "min": node.min, "max": node.max, "step": node.step}
    if isinstance(node, Choice):
        return {"type": "Choice", "values": list(node.values)}
    if isinstance(node, Group):
        return {
            "type": "Group",
            "test": schema_node_to_dict(node.test),
            "on_value": node.on_value,
            "when_on": _subtree_to_dict(node.when_on),
        }
    if isinstance(node, Branch):
        return {
            "type": "Branch",
            "selector": schema_node_to_dict(node.selector),
            "arms": {k: _subtree_to_dict(v) for k, v in node.arms.items()},
        }
    raise TypeError(f"schema_node_to_dict: unsupported type {type(node).__name__}")


def dict_to_schema_node(d: dict) -> Any:
    t = d.get("type")
    if t == "FloatRange":
        return FloatRange(
            min=d["min"],
            max=d["max"],
            scale=d.get("scale", "linear"),
            step=d.get("step"),
        )
    if t == "IntRange":
        return IntRange(min=d["min"], max=d["max"], step=d.get("step", 1))
    if t == "Choice":
        return Choice(d["values"])
    if t == "Group":
        return Group(
            test=dict_to_schema_node(d["test"]),
            when_on=_subtree_from_dict(d.get("when_on", {})),
            on_value=d.get("on_value", True),
        )
    if t == "Branch":
        return Branch(
            selector=dict_to_schema_node(d["selector"]),
            arms={k: _subtree_from_dict(v) for k, v in d.get("arms", {}).items()},
        )
    raise ValueError(f"dict_to_schema_node: unknown type {t!r}")


# ── Sub-tree (nested dict of nodes) ────────────────────────────────────


def _subtree_to_dict(tree: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in tree.items():
        if isinstance(v, _NODE_TYPES):
            out[k] = schema_node_to_dict(v)
        elif isinstance(v, dict):
            out[k] = _subtree_to_dict(v)
        else:
            out[k] = v
    return out


def _subtree_from_dict(tree: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in tree.items():
        if isinstance(v, dict) and v.get("type") in _NODE_TYPE_NAMES:
            out[k] = dict_to_schema_node(v)
        elif isinstance(v, dict):
            out[k] = _subtree_from_dict(v)
        else:
            out[k] = v
    return out


# ── EA top-level ───────────────────────────────────────────────────────


def ea_to_dict(ea: dict) -> dict:
    """Serialise the user-visible parts of an EA. ``engine_mapping`` is skipped."""
    out: dict[str, Any] = {}
    for k in _SERIALISABLE_TOP_KEYS:
        if k not in ea:
            continue
        v = ea[k]
        if k in ("signals", "engine_schema"):
            out[k] = _subtree_to_dict(v)
        else:
            out[k] = v
    return out


def dict_to_ea(d: dict, *, engine_mapping: list | None = None) -> dict:
    """Reconstruct an EA dict from its JSON form.

    ``engine_mapping``, if supplied, is attached verbatim. If ``None``, the
    returned EA has no ``engine_mapping`` entry and must be completed before
    being passed to :func:`ff.harness.run`.
    """
    out: dict[str, Any] = {}
    for k in _SERIALISABLE_TOP_KEYS:
        if k not in d:
            continue
        v = d[k]
        if k in ("signals", "engine_schema"):
            out[k] = _subtree_from_dict(v)
        else:
            out[k] = v
    if engine_mapping is not None:
        out["engine_mapping"] = engine_mapping
    return out


# ── File helpers ───────────────────────────────────────────────────────


def save_ea(ea: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(ea_to_dict(ea), indent=2), encoding="utf-8")


def load_ea_json(path: str | Path, *, engine_mapping: list | None = None) -> dict:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return dict_to_ea(d, engine_mapping=engine_mapping)


# ── Self-test ──────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    from ff.schema import Branch, Choice, FloatRange, Group, IntRange

    sample = {
        "name": "rt",
        "data": {"pair": "EUR_USD", "main_tf": "H1", "sub_tf": "M1"},
        "execution": {"pip_value": None, "commission_pips": 0.3},
        "signals": {
            "ema_cross": {"fast": IntRange(5, 32, step=5), "slow": IntRange(21, 180, step=30)},
        },
        "engine_schema": {
            "stop_loss": Branch(
                selector=Choice(["fixed", "atr"]),
                arms={
                    "fixed": {"pips": FloatRange(5, 100, scale="log")},
                    "atr": {"mult": FloatRange(0.5, 4.0)},
                },
            ),
            "trailing": Group(
                test=Choice([True, False]),
                on_value=True,
                when_on={"activate": FloatRange(5, 80, scale="log")},
            ),
            "days": Choice([31, 63, 127]),
        },
    }
    round_tripped = dict_to_ea(ea_to_dict(sample))
    assert round_tripped["signals"] == sample["signals"], "signals mismatch"
    assert round_tripped["engine_schema"] == sample["engine_schema"], "engine_schema mismatch"
    print("schema_json round-trip: OK")
