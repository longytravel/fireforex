"""Pre-flight reporter: estimate what a run will cost BEFORE running it.

Shows signal-library combo counts (exact), estimated library build + sweep
times (heuristic), and effective-dimensionality range (sampled).

Estimates are deliberately rough — the point is to catch obvious
mis-configurations (e.g. EMA_FAST step=1 exploding the library to 10 000 combos)
before paying for a 10-minute run.

Two entry points:

- :func:`preflight_dict` — structured data for the web UI.
- :func:`preflight_report` — human-readable text (built on top of
  :func:`preflight_dict`), what ``run.py --dry-run`` prints.
"""

from __future__ import annotations

import csv
import statistics
from pathlib import Path
from typing import Any

from . import sampler as spl
from . import schema as sc
from . import signal_lib as sl

# Heuristic fallbacks — replaced by medians from history.csv once available.
SIGNAL_BUILD_SEC_PER_COMBO = 0.25  # seconds per indicator-combo (observed).
SWEEP_RATE_BT_PER_SEC_HINT = (120, 400)  # (low, high) per-trial rate range.

_HISTORY_CSV = Path(__file__).resolve().parent.parent / "artifacts" / "history.csv"


def _learn_rates_from_history(max_rows: int = 10) -> dict[str, float] | None:
    """Compute median (per-combo build time, bt/sec sweep rate) from recent runs.

    Returns ``None`` if history is empty or columns are missing.
    """
    if not _HISTORY_CSV.exists():
        return None
    try:
        with _HISTORY_CSV.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return None
    per_combo: list[float] = []
    bt_rates: list[float] = []
    for r in rows[-max_rows:]:
        try:
            nt = float(r.get("n_trials") or 0)
            bps = float(r.get("bt_per_sec") or 0)
            rt = float(r.get("runtime_s") or 0)
            nv = float(r.get("n_variants") or 0)
        except (ValueError, TypeError):
            continue
        if nt <= 0 or bps <= 0 or rt <= 0 or nv <= 0:
            continue
        sweep_s = nt / bps
        build_s = max(0.0, rt - sweep_s - 3.0)  # ~3s for load+encode+save
        pc = build_s / nv
        if 0 < pc < 10:
            per_combo.append(pc)
        bt_rates.append(bps)
    if not per_combo and not bt_rates:
        return None
    return {
        "per_combo_s": statistics.median(per_combo) if per_combo else None,
        "bt_per_sec": statistics.median(bt_rates) if bt_rates else None,
    }


def _count_leaves_and_groups(subtree: dict) -> tuple[int, int, int]:
    """Return (leaf_count, on_off_group_count, branch_count) walking the schema."""
    leaves = 0
    groups = 0
    branches = 0
    for _path, node in sc.walk(subtree):
        if isinstance(node, (sc.FloatRange, sc.IntRange, sc.Choice)):
            leaves += 1
        elif isinstance(node, sc.Group):
            groups += 1
        elif isinstance(node, sc.Branch):
            branches += 1
    return leaves, groups, branches


def _effective_dim_sample(engine_schema: dict, n_variants: int, sample_n: int = 200, seed: int = 0) -> dict:
    """Sample trials and count how many leaf decisions each one records.

    Returns {min, max, mean} effective-dim counts.
    """
    sampler = spl.RandomSampler(engine_schema, n_variants=max(1, n_variants), seed=seed)
    trials = sampler.sample(sample_n)
    counts = []
    for t in trials:
        counts.append(_count_trial_decisions(t))
    return {
        "min": min(counts) if counts else 0,
        "max": max(counts) if counts else 0,
        "mean": (sum(counts) / len(counts)) if counts else 0.0,
    }


def _count_trial_decisions(trial: dict | Any) -> int:
    """Count the number of terminal decisions recorded in a trial dict.

    - signal_variant counts as one.
    - Every dict-nested terminal value counts as one.
    - Missing sub-knobs (from off Groups / inactive Branch arms) don't count.
    """
    n = 0

    def visit(node):
        nonlocal n
        if isinstance(node, dict):
            for v in node.values():
                visit(v)
        else:
            n += 1

    visit(trial)
    return n


def preflight_dict(ea: dict, n_trials: int) -> dict:
    """Return structured pre-flight numbers for an EA + trial budget.

    Shape::

        {
          "signals":   {"library_size": int, "families": {name: int}},
          "engine":    {"n_leaves": int, "n_groups": int, "n_branches": int,
                        "eff_dims_min": int, "eff_dims_mean": float,
                        "eff_dims_max": int},
          "estimates": {"signal_build_s": float,
                        "sweep_s_low": float, "sweep_s_high": float,
                        "total_s_low": float, "total_s_high": float},
          "text":      str,   # the full human-readable report.
        }
    """
    # Signal library.
    lib_est = sl.estimate_library_size(ea["signals"])
    total = int(lib_est["_total"])
    families: dict[str, int] = {fam: int(d["combos"]) for fam, d in lib_est.items() if fam != "_total"}
    # Learn from recent runs when we can — otherwise fall back to the
    # heuristic constant.
    rates = _learn_rates_from_history()
    per_combo_s = (rates or {}).get("per_combo_s") or SIGNAL_BUILD_SEC_PER_COMBO
    build_sec = float(total * per_combo_s)

    # Engine schema dimensions.
    leaves, groups, branches = _count_leaves_and_groups(ea["engine_schema"])
    eff = _effective_dim_sample(ea["engine_schema"], n_variants=max(1, total), sample_n=200)

    bps = (rates or {}).get("bt_per_sec")
    if bps and bps > 0:
        sweep_lo = float(n_trials / (bps * 1.3))  # lucky (30% faster)
        sweep_hi = float(n_trials / (bps * 0.7))  # slow (30% slower)
    else:
        lo, hi = SWEEP_RATE_BT_PER_SEC_HINT
        sweep_lo = float(n_trials / hi)
        sweep_hi = float(n_trials / lo)

    out: dict[str, Any] = {
        "signals": {
            "library_size": total,
            "families": families,
        },
        "engine": {
            "n_leaves": int(leaves),
            "n_groups": int(groups),
            "n_branches": int(branches),
            "eff_dims_min": int(eff["min"]),
            "eff_dims_mean": float(eff["mean"]),
            "eff_dims_max": int(eff["max"]),
        },
        "estimates": {
            "signal_build_s": build_sec,
            "sweep_s_low": sweep_lo,
            "sweep_s_high": sweep_hi,
            "total_s_low": build_sec + sweep_lo,
            "total_s_high": build_sec + sweep_hi,
        },
    }
    out["text"] = _format_preflight_text(ea, n_trials, out, lib_est)
    return out


def _format_preflight_text(ea: dict, n_trials: int, structured: dict, lib_est: dict) -> str:
    """Render the text preflight report from the already-computed structured data."""
    name = ea.get("name", "unnamed")
    data = ea.get("data", {})
    exe = ea.get("execution", {})

    total = structured["signals"]["library_size"]
    build_sec = structured["estimates"]["signal_build_s"]
    leaves = structured["engine"]["n_leaves"]
    groups = structured["engine"]["n_groups"]
    eff_min = structured["engine"]["eff_dims_min"]
    eff_mean = structured["engine"]["eff_dims_mean"]
    eff_max = structured["engine"]["eff_dims_max"]
    lo, hi = SWEEP_RATE_BT_PER_SEC_HINT
    rt_lo = structured["estimates"]["sweep_s_low"]
    rt_hi = structured["estimates"]["sweep_s_high"]

    lines: list[str] = []
    lines.append("═" * 64)
    lines.append(f"Fire Forex pre-flight · {name}")
    lines.append("═" * 64)
    lines.append("[data]")
    lines.append(f"  pair              : {data.get('pair', '?')}")
    lines.append(f"  main / sub tf     : {data.get('main_tf', '?')} / {data.get('sub_tf', '?')}")
    lines.append("[execution]")
    lines.append(f"  pip_value         : {exe.get('pip_value', 'auto')}")
    lines.append(f"  commission_pips   : {exe.get('commission_pips', '?')}")
    lines.append(f"  max_spread_pips   : {exe.get('max_spread_pips', '?')}")
    lines.append(f"  slippage_pips     : {exe.get('slippage_pips', '?')}")
    lines.append(f"  atr_period        : {exe.get('atr_period', '?')}")
    lines.append("[signal library]")
    for fam, d in lib_est.items():
        if fam == "_total":
            continue
        lines.append(f"  {fam:<15} : {d['combos']:>5} combos ({d['raw']} raw)")
    lines.append(f"  {'TOTAL VARIANTS':<15} = {total}")
    lines.append(f"  estimated build time          ≈ {build_sec:,.1f} s")
    lines.append("[engine knobs]")
    lines.append(f"  total leaves (max dim)         : {leaves}")
    lines.append(f"  on/off groups                  : {groups}")
    lines.append("  effective-dim (sampled 200):")
    lines.append(f"     min / mean / max           : {eff_min} / {eff_mean:.1f} / {eff_max}")
    lines.append("[sweep]")
    lines.append(f"  N_TRIALS                       : {n_trials:,}")
    lines.append(f"  est. rate (range)              : {lo}–{hi} bt/sec")
    lines.append(f"  est. sweep time (range)        : {rt_lo:.1f}–{rt_hi:.1f} s")
    lines.append("─" * 64)
    return "\n".join(lines)


def preflight_report(ea: dict, n_trials: int) -> str:
    """Return a human-readable pre-flight string for an EA + trial budget.

    Thin formatter around :func:`preflight_dict` — same numbers, rendered as
    text for the CLI ``--dry-run`` path.
    """
    return preflight_dict(ea, n_trials)["text"]


# ── Self-test ──────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    ea = {
        "name": "demo_preflight",
        "data": {"pair": "EUR_USD", "main_tf": "H1", "sub_tf": "M1"},
        "execution": {
            "pip_value": 0.0001,
            "commission_pips": 0.3,
            "max_spread_pips": 10.0,
            "slippage_pips": 0.0,
            "atr_period": 14,
        },
        "signals": {
            "ema_cross": {"fast": sc.IntRange(5, 20, step=5), "slow": sc.IntRange(21, 50, step=10)},
        },
        "engine_schema": {
            "trailing": sc.Group(
                test=sc.Choice([True, False]),
                on_value=True,
                when_on={"activate": sc.FloatRange(5, 100, scale="log")},
            ),
            "days": sc.Choice([31, 127]),
        },
    }
    print(preflight_report(ea, n_trials=500))
