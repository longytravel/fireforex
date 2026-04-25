"""Inspection report — print every knob in an EA, clearly.

The whole point of this module: a non-coder should be able to run
``python run.py eas/XX.py --inspect`` and see every parameter, every
timeframe choice, and every step size — without reading Python.

No side effects. No file edits. Just reads an EA dict and prints.

This module exposes two entry points:

- :func:`inspect_dict` — structured tree suitable for JSON rendering (used by
  the web UI).
- :func:`inspect_report` — human-readable text formatter, built on top of
  :func:`inspect_dict` plus a few extra text-only sections (data-file
  sizes, sensitivity tables, etc.).
"""

from __future__ import annotations

from . import harness as hn
from . import schema as sc
from . import signal_lib as sl

# ── Helpers ────────────────────────────────────────────────────────────


def _fmt_values(values: list) -> str:
    if len(values) <= 8:
        return str(values)
    return f"[{values[0]}, {values[1]}, {values[2]}, ..., {values[-2]}, {values[-1]}]"


def _leaf_desc(leaf) -> str:
    if isinstance(leaf, sc.FloatRange):
        step = f" step={leaf.step}" if leaf.step is not None else ""
        return f"FloatRange  {leaf.min} to {leaf.max}   scale={leaf.scale}{step}"
    if isinstance(leaf, sc.IntRange):
        return f"IntRange    {leaf.min} to {leaf.max}   step={leaf.step}"
    if isinstance(leaf, sc.Choice):
        return f"Choice      {list(leaf.values)}"
    return f"{type(leaf).__name__}"


def _describe_node(node, indent: int = 2) -> list[str]:
    """Recursively describe a schema tree node."""
    lines: list[str] = []
    pad = " " * indent
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (sc.FloatRange, sc.IntRange, sc.Choice)):
                lines.append(f"{pad}{k:<22} {_leaf_desc(v)}")
            elif isinstance(v, sc.Group):
                test_values = list(v.test.values)
                lines.append(f"{pad}{k}  (on/off group — test={test_values}, on={v.on_value!r})")
                lines.append(f"{pad}  when ON:")
                lines.extend(_describe_node(v.when_on, indent + 4))
            elif isinstance(v, sc.Branch):
                sel_values = list(v.selector.values)
                lines.append(f"{pad}{k}  (Branch — pick one of {sel_values})")
                for arm_name, arm_subtree in v.arms.items():
                    if not arm_subtree:
                        lines.append(f"{pad}  when '{arm_name}': (no sub-knobs)")
                    else:
                        lines.append(f"{pad}  when '{arm_name}':")
                        lines.extend(_describe_node(arm_subtree, indent + 4))
            elif isinstance(v, dict):
                lines.append(f"{pad}{k}:")
                lines.extend(_describe_node(v, indent + 2))
    return lines


def _sensitivity(param_spec: dict, family_name: str, current_combos: int) -> list[str]:
    """Show what happens to combo count if steps are halved or doubled."""

    def count_with_factor(factor: float) -> int:
        spec2: dict = {}
        for k, leaf in param_spec.items():
            if isinstance(leaf, sc.IntRange):
                new_step = max(1, int(round(leaf.step * factor)))
                spec2[k] = sc.IntRange(leaf.min, leaf.max, step=new_step)
            elif isinstance(leaf, sc.FloatRange) and leaf.step is not None:
                spec2[k] = sc.FloatRange(leaf.min, leaf.max, scale=leaf.scale, step=leaf.step * factor)
            else:
                spec2[k] = leaf
        est = sl.estimate_library_size({family_name: spec2})
        return est["_total"]

    half = count_with_factor(0.5)
    double = count_with_factor(2.0)
    return [
        f"      → current steps:        {current_combos:>4} valid combos",
        f"      → halve every step:     {half:>4} combos ({'more' if half > current_combos else 'fewer'} variants, slower build)",
        f"      → double every step:    {double:>4} combos (fewer variants, less search room)",
    ]


# ── Structured (JSON-friendly) tree ────────────────────────────────────


def _leaf_to_dict(leaf) -> dict:
    """Serialise a single leaf node to a JSON-friendly dict.

    The ``kind`` field distinguishes the three leaf shapes. Always stable keys
    so the UI can depend on them.
    """
    if isinstance(leaf, sc.FloatRange):
        return {
            "kind": "float",
            "min": leaf.min,
            "max": leaf.max,
            "scale": leaf.scale,
            "step": leaf.step,
        }
    if isinstance(leaf, sc.IntRange):
        return {
            "kind": "int",
            "min": leaf.min,
            "max": leaf.max,
            "step": leaf.step,
        }
    if isinstance(leaf, sc.Choice):
        return {
            "kind": "choice",
            "values": list(leaf.values),
        }
    # Fallback — should never fire for a well-formed EA, but we don't want
    # to crash the web UI.
    return {"kind": "unknown", "repr": repr(leaf)}


def _schema_tree_to_list(subtree: dict) -> list[dict]:
    """Convert an engine-schema sub-dict into an ordered list of entries.

    Each entry carries ``name`` (original dict key) + ``kind`` + whatever the
    node-type-specific fields are (see module docstring for shapes). Arrays
    (not dicts) are used so front-end rendering gets stable ordering.
    """
    out: list[dict] = []
    if not isinstance(subtree, dict):
        return out
    for key, node in subtree.items():
        if isinstance(node, (sc.FloatRange, sc.IntRange, sc.Choice)):
            entry = {"name": key, **_leaf_to_dict(node)}
            out.append(entry)
        elif isinstance(node, sc.Group):
            out.append(
                {
                    "name": key,
                    "kind": "group",
                    "test_values": list(node.test.values),
                    "on_value": node.on_value,
                    "when_on": _schema_tree_to_list(node.when_on),
                }
            )
        elif isinstance(node, sc.Branch):
            arms: dict[str, list[dict]] = {}
            for arm_name, arm_subtree in node.arms.items():
                arms[arm_name] = _schema_tree_to_list(arm_subtree or {})
            out.append(
                {
                    "name": key,
                    "kind": "branch",
                    "selector_values": list(node.selector.values),
                    "arms": arms,
                }
            )
        elif isinstance(node, dict):
            # Nested dict (rare — a plain sub-namespace). Treat as an anonymous
            # group-like container so the UI can render it.
            out.append(
                {
                    "name": key,
                    "kind": "namespace",
                    "children": _schema_tree_to_list(node),
                }
            )
        # Anything else (shouldn't happen) is silently dropped.
    return out


def _signal_param_to_dict(name: str, leaf) -> dict:
    """Serialise a single signal-family parameter leaf (always a Leaf)."""
    return {"name": name, **_leaf_to_dict(leaf)}


def inspect_dict(ea: dict) -> dict:
    """Return a structured tree view of ``ea`` suitable for JSON rendering.

    Shape::

        {
          "name": str,
          "data": {"pair": str, "main_tf": str, "sub_tf": str},
          "execution": { ... resolved execution dict ... },
          "signals": [
            {"name": "<family>",
             "params": [{"name": "<knob>", "kind": ..., ...}, ...]},
            ...
          ],
          "engine_schema": [
            {"name": "<knob>", "kind": "float"|"int"|"choice"|"group"|"branch", ...},
            ...
          ],
        }

    - Arrays (not dicts) at the engine_schema / signals-params level so the
      UI gets stable ordering.
    - Leaves carry ``min``/``max``/``step`` (plus ``scale`` for floats) and
      ``values`` (for Choice).
    - ``when_on``/``arms``/``selector_values`` only appear on Group/Branch
      entries respectively.
    """
    data = dict(ea.get("data", {}))
    exe = dict(ea.get("execution", {}))

    # Signals — arrays, not dicts, for UI stability.
    signals_list: list[dict] = []
    for family, spec in ea.get("signals", {}).items():
        params: list[dict] = []
        if isinstance(spec, dict):
            for knob, leaf in spec.items():
                params.append(_signal_param_to_dict(knob, leaf))
        signals_list.append({"name": family, "params": params})

    engine_list = _schema_tree_to_list(ea.get("engine_schema", {}) or {})

    return {
        "name": ea.get("name", "?"),
        "data": {
            "pair": data.get("pair", "?"),
            "main_tf": data.get("main_tf", "?"),
            "sub_tf": data.get("sub_tf", "?"),
        },
        "execution": exe,
        "signals": signals_list,
        "engine_schema": engine_list,
    }


# ── Main report (text formatter built on top of inspect_dict) ──────────


def inspect_report(ea: dict, ea_path: str | None = None) -> str:
    """Return a plain-English, line-by-line inspection of an EA.

    Internally calls :func:`inspect_dict` for structured fields, then layers
    on the data-file-sizes, sensitivity tables, and footer that are only
    useful in the text form.
    """
    tree = inspect_dict(ea)
    lines: list[str] = []
    name = tree["name"]
    data = tree["data"]
    # Prefer the raw EA execution dict for "auto" pip-value pretty-printing.
    exe = ea.get("execution", {})

    W = 78
    lines.append("═" * W)
    lines.append(f"  Fire Forex · {name} · parameter inspection")
    if ea_path:
        lines.append(f"  Source: {ea_path}")
    lines.append("═" * W)

    # ── DATA + timeframe preview ──
    lines.append("\nDATA")
    lines.append("─" * W)
    pair = data.get("pair", "?")
    main_tf = data.get("main_tf", "?")
    sub_tf = data.get("sub_tf", "?")
    lines.append(f"  Pair        : {pair}")
    lines.append(f"  Main TF     : {main_tf}   (used for entries / signal generation)")
    lines.append(f"  Sub TF      : {sub_tf}   (used for SL/TP/trail fills — must be finer)")
    main_path = hn.DATA_ROOT / f"{pair}_{main_tf}.parquet"
    lines.append("")
    lines.append(f"  Main TF data file: {main_path}")
    if main_path.exists():
        try:
            df = hn.load_parquet(main_path)
            lines.append(f"    Bars: {len(df):,}   ({df.index.min()} → {df.index.max()})")
        except Exception as exc:
            lines.append(f"    (failed to read: {exc})")
    else:
        lines.append("    (file not found — check the pair / timeframe name)")
    # Other timeframes available for this pair:
    lines.append("")
    lines.append(f"  Other timeframes available for {pair}:")
    available = []
    for tf in ["M1", "M5", "M15", "M30", "H1", "H4", "D", "W"]:
        p = hn.DATA_ROOT / f"{pair}_{tf}.parquet"
        if p.exists():
            try:
                size_mb = p.stat().st_size / (1024 * 1024)
                marker = "  ← current main" if tf == main_tf else ("  ← current sub" if tf == sub_tf else "")
                available.append(f"    {tf:<4}  ~{size_mb:>6.1f} MB{marker}")
            except OSError:
                pass
    if available:
        lines.extend(available)
    else:
        lines.append("    (none found)")

    # ── EXECUTION ──
    lines.append("\nEXECUTION")
    lines.append("─" * W)
    pip = exe.get("pip_value")
    pip_str = f"auto ({hn.pip_value_for(pair):.4f} for {pair})" if pip is None else f"{pip}"
    lines.append(f"  pip_value         : {pip_str}")
    lines.append(f"  commission_pips   : {exe.get('commission_pips', '?')}")
    lines.append(f"  max_spread_pips   : {exe.get('max_spread_pips', '?')}  (filter: skip trades when actual spread exceeds this)")
    lines.append(f"  slippage_pips     : {exe.get('slippage_pips', '?')}")
    lines.append(f"  atr_period        : {exe.get('atr_period', '?')}  (lookback for ATR/volatility measure)")

    # ── SIGNAL LIBRARY ──
    lines.append("\nSIGNAL LIBRARY  (indicator grids → one pre-computed variant per combo)")
    lines.append("─" * W)
    total = 0
    for family, spec in ea["signals"].items():
        lines.append(f"\n  [{family}]")
        for knob, leaf in spec.items():
            if isinstance(leaf, sc.IntRange):
                values = list(range(leaf.min, leaf.max + 1, leaf.step))
                lines.append(f"      {knob:<10} {leaf.min} to {leaf.max}, step {leaf.step}  → {len(values)} values  {_fmt_values(values)}")
            elif isinstance(leaf, sc.Choice):
                lines.append(f"      {knob:<10} Choice of {list(leaf.values)}")
            elif isinstance(leaf, sc.FloatRange):
                vs = sc.expand(leaf)
                lines.append(f"      {knob:<10} {leaf.min} to {leaf.max}, step {leaf.step}  → {len(vs)} values")
        est = sl.estimate_library_size({family: spec})
        combos = est["_total"]
        total += combos
        lines.extend(_sensitivity(spec, family, combos))
    lines.append(f"\n  TOTAL PRE-COMPUTED VARIANTS: {total}")
    lines.append("  (Each variant is one complete entry-signal configuration. The optimiser")
    lines.append("   picks exactly one variant per trial via PL_SIGNAL_VARIANT.)")

    # ── ENGINE KNOBS ──
    lines.append("\nENGINE KNOBS  (sampled fresh every trial)")
    lines.append("─" * W)
    lines.extend(_describe_node(ea["engine_schema"], indent=2))
    n_leaves = sc.count_leaves(ea["engine_schema"])
    lines.append(f"\n  Max effective dimensions (every group ON): {n_leaves}")

    # ── FOOTER ──
    lines.append("")
    lines.append("─" * W)
    if ea_path:
        lines.append(f"  To change anything, edit: {ea_path}")
    lines.append("  After editing, run:")
    if ea_path:
        lines.append(f"    python run.py {ea_path} --trials 2000")
    else:
        lines.append("    python run.py eas/<this_ea>.py --trials 2000")
    lines.append("")
    lines.append("  Halving a signal-library step generally multiplies the library size and the")
    lines.append("  runtime — use --inspect again after any edit to see the new estimate.")
    lines.append("─" * W)
    return "\n".join(lines)
