"""Per-knob overrides on top of a generated EA.

The web UI lets a user tweak any knob that the complexity preset produced:
change its min / max / step, disable it (freeze at a single value), or turn a
Group on/off. This module applies those edits to an already-built EA in place.

Override shape (all keys optional)::

    {
      "groups": { "trailing": false, "breakeven": true },
      "knobs":  { "stop_loss.atr.mult": {"min": 1.0, "max": 3.0, "step": 0.1,
                                          "enabled": true, "frozen": 1.5} },
      "global": { "step_multiplier": 2.0 }
    }

Paths are dotted routes through ``engine_schema`` and ``signals``. Inside a
Branch, the path segment is the arm name (``stop_loss.atr.mult``). Inside a
Group, use ``when_on`` as a segment (``session.when_on.hours_start``).
"""

from __future__ import annotations

from dataclasses import fields, replace
from typing import Any

from ff.schema import Branch, Choice, FloatRange, Group, IntRange

_LEAF_TYPES = (FloatRange, IntRange, Choice)


# ── Public entry ───────────────────────────────────────────────────────


def apply_overrides(ea: dict, overrides: dict | None) -> dict:
    """Return a new EA with overrides applied. Original is left untouched."""
    if not overrides:
        return ea
    out = _clone_ea(ea)
    g_mult = float((overrides.get("global") or {}).get("step_multiplier") or 1.0)
    if g_mult and abs(g_mult - 1.0) > 1e-9:
        _scale_steps(out.get("engine_schema", {}), g_mult)
        _scale_steps(out.get("signals", {}), g_mult)
    # Drop excluded signal families first so subsequent knob overrides
    # targeting them are harmlessly no-op.
    fam_flags = overrides.get("signal_families") or {}
    if fam_flags:
        signals = out.get("signals", {})
        for fam_name, keep in fam_flags.items():
            if keep is False and fam_name in signals:
                del signals[fam_name]
        if not signals:
            # Never let the library end up empty — keep one family as a fallback.
            src = ea.get("signals", {})
            if src:
                first = next(iter(src))
                signals[first] = src[first]
    for grp_path, enabled in (overrides.get("groups") or {}).items():
        _set_group_enabled(out.get("engine_schema", {}), grp_path.split("."), bool(enabled))
    for knob_path, spec in (overrides.get("knobs") or {}).items():
        if not isinstance(spec, dict):
            continue
        _apply_knob(out, knob_path.split("."), spec)
    return out


# ── Clone ──────────────────────────────────────────────────────────────


def _clone_ea(ea: dict) -> dict:
    """Deep-copy the mutable parts; the engine_mapping list is left by reference."""
    out = dict(ea)
    out["signals"] = _clone_tree(ea.get("signals", {}))
    out["engine_schema"] = _clone_tree(ea.get("engine_schema", {}))
    return out


def _clone_tree(tree: Any) -> Any:
    if isinstance(tree, dict):
        return {k: _clone_tree(v) for k, v in tree.items()}
    if isinstance(tree, (FloatRange, IntRange, Choice)):
        return _shallow_dataclass(tree)
    if isinstance(tree, Group):
        return Group(test=_clone_tree(tree.test), when_on=_clone_tree(tree.when_on), on_value=tree.on_value)
    if isinstance(tree, Branch):
        return Branch(
            selector=_clone_tree(tree.selector),
            arms={k: _clone_tree(v) for k, v in tree.arms.items()},
        )
    return tree


def _shallow_dataclass(obj: Any) -> Any:
    kwargs = {f.name: getattr(obj, f.name) for f in fields(obj)}
    return type(obj)(**kwargs)


# ── Global step multiplier ─────────────────────────────────────────────


def _scale_steps(tree: Any, mult: float) -> None:
    if isinstance(tree, dict):
        for k, v in list(tree.items()):
            if isinstance(v, FloatRange):
                if v.step is not None:
                    tree[k] = replace(v, step=_snap(v.step * mult))
            elif isinstance(v, IntRange):
                new_step = max(1, int(round(v.step * mult)))
                if new_step != v.step:
                    tree[k] = replace(v, step=new_step)
            else:
                _scale_steps(v, mult)
    elif isinstance(tree, Group):
        _scale_steps(tree.when_on, mult)
    elif isinstance(tree, Branch):
        for arm in tree.arms.values():
            _scale_steps(arm, mult)


def _snap(x: float) -> float:
    if x == 0:
        return x
    return round(x, 6)


# ── Group on/off ───────────────────────────────────────────────────────


def _set_group_enabled(tree: dict, path: list[str], enabled: bool) -> None:
    """Force a Group on or off by rewriting its ``test.values``.

    Group is an unfrozen dataclass, so we mutate ``test`` in place — this
    skips ``__post_init__``'s ``len(test.values) >= 2`` check, which we need
    because an "always-off" group has a single-value test.
    """
    node = _walk_get(tree, path)
    if not isinstance(node, Group):
        return
    existing = list(node.test.values)
    off_candidates = [v for v in existing if v != node.on_value]
    off_val = off_candidates[0] if off_candidates else ((not node.on_value) if isinstance(node.on_value, bool) else False)
    if enabled:
        if node.on_value not in existing:
            existing.append(node.on_value)
        if off_val not in existing:
            existing.append(off_val)
        node.test = Choice(tuple(existing))
    else:
        node.test = Choice((off_val,))


# ── Per-knob override ──────────────────────────────────────────────────


def _apply_knob(ea: dict, path: list[str], spec: dict) -> None:
    # search both engine_schema and signals
    root_candidates: list[tuple[list[str], dict]] = []
    root_candidates.append((path, ea.get("engine_schema", {})))
    root_candidates.append((path, ea.get("signals", {})))
    for p, root in root_candidates:
        node = _walk_get(root, p)
        if node is None:
            continue
        new_node = _mutate_leaf(node, spec)
        if new_node is not None:
            _walk_set(root, p, new_node)
            return


def _mutate_leaf(node: Any, spec: dict) -> Any:
    if "frozen" in spec:
        return Choice((spec["frozen"],))
    enabled = spec.get("enabled")
    if enabled is False:
        # Freeze at the current min (or mid) as a single Choice.
        if isinstance(node, (FloatRange, IntRange)):
            pick = float(node.min)
            if isinstance(node, IntRange):
                pick = int(node.min)
            return Choice((pick,))
        if isinstance(node, Choice):
            return Choice((node.values[0],))
    if isinstance(node, FloatRange):
        return replace(
            node,
            min=float(spec.get("min", node.min)),
            max=float(spec.get("max", node.max)),
            step=_opt_float(spec.get("step", node.step)),
        )
    if isinstance(node, IntRange):
        new_step = spec.get("step", node.step)
        new_step = max(1, int(new_step)) if new_step else node.step
        return replace(
            node,
            min=int(spec.get("min", node.min)),
            max=int(spec.get("max", node.max)),
            step=new_step,
        )
    if isinstance(node, Choice):
        if "values" in spec:
            return Choice(tuple(spec["values"]))
        return node
    return None


def _opt_float(v: Any) -> float | None:
    if v is None or v == "" or (isinstance(v, float) and v != v):
        return None
    return float(v)


# ── Path walking through Group/Branch/dict ─────────────────────────────


def _walk_get(tree: Any, path: list[str]) -> Any:
    node: Any = tree
    for key in path:
        node = _child(node, key)
        if node is None:
            return None
    return node


def _walk_set(tree: Any, path: list[str], new_node: Any) -> None:
    assert path, "empty path"
    parent = tree
    for key in path[:-1]:
        parent = _child(parent, key)
        if parent is None:
            return
    last = path[-1]
    _set_child(parent, last, new_node)


def _child(parent: Any, key: str) -> Any:
    if isinstance(parent, dict):
        return parent.get(key)
    if isinstance(parent, Group):
        if key == "when_on":
            return parent.when_on
        if key == "test":
            return parent.test
        return parent.when_on.get(key) if isinstance(parent.when_on, dict) else None
    if isinstance(parent, Branch):
        if key == "selector":
            return parent.selector
        return parent.arms.get(key)
    return None


def _set_child(parent: Any, key: str, value: Any) -> None:
    if isinstance(parent, dict):
        parent[key] = value
        return
    if isinstance(parent, Group):
        if key == "when_on":
            parent.when_on = value if isinstance(value, dict) else parent.when_on
        elif key == "test":
            # replace test (Group is a dataclass with default eq); dataclass is
            # mutable since we didn't freeze it.
            parent.test = value
        elif isinstance(parent.when_on, dict):
            parent.when_on[key] = value
        return
    if isinstance(parent, Branch):
        if key == "selector":
            parent.selector = value
        elif key in parent.arms:
            # ``parent.arms[key]`` is a dict subtree.
            parent.arms[key] = value
        return


# ── Schema flattener for the UI ────────────────────────────────────────


def flatten_schema(tree: dict, prefix: str = "") -> list[dict]:
    """Return a flat list of dicts describing every knob & group for the UI.

    Each entry::

        {"path": "stop_loss.atr.mult",
         "kind": "float"|"int"|"choice"|"group"|"branch"|"branch_selector",
         "min": .., "max": .., "step": .., "scale": "linear"|"log",
         "values": [...], "test_values": [...], "on_value": .., "enabled": bool}
    """
    out: list[dict] = []
    if isinstance(tree, dict):
        for k, v in tree.items():
            path = f"{prefix}.{k}" if prefix else k
            out.extend(_flatten_node(path, v))
    return out


def _flatten_node(path: str, node: Any) -> list[dict]:
    out: list[dict] = []
    if isinstance(node, FloatRange):
        out.append(
            {
                "path": path,
                "kind": "float",
                "min": node.min,
                "max": node.max,
                "step": node.step,
                "scale": node.scale,
            }
        )
    elif isinstance(node, IntRange):
        out.append({"path": path, "kind": "int", "min": node.min, "max": node.max, "step": node.step})
    elif isinstance(node, Choice):
        out.append({"path": path, "kind": "choice", "values": list(node.values)})
    elif isinstance(node, Group):
        test_values = list(node.test.values)
        enabled = node.on_value in test_values and len(test_values) > 1
        out.append(
            {
                "path": path,
                "kind": "group",
                "test_values": test_values,
                "on_value": node.on_value,
                "enabled": enabled,
            }
        )
        out.extend(flatten_schema(node.when_on, f"{path}.when_on"))
    elif isinstance(node, Branch):
        out.append({"path": path, "kind": "branch", "selector_values": list(node.selector.values)})
        for arm_name, arm in node.arms.items():
            out.extend(flatten_schema(arm, f"{path}.{arm_name}"))
    elif isinstance(node, dict):
        out.extend(flatten_schema(node, path))
    return out
