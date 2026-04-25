"""Pinned baseline runs.

A baseline is a snapshot of a previously-completed run that we compare new
runs against. Persisted at ``artifacts/baseline.json`` so it survives
restarts.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

BASELINE_PATH = Path(__file__).resolve().parent.parent / "artifacts" / "baseline.json"

_WRITE_LOCK = threading.Lock()

_KPI_KEYS = (
    "trades",
    "win_rate_pct",
    "total_pips",
    "expectancy_pips",
    "max_dd_pct",
    "profit_factor",
    "sharpe",
    "return_pct",
)


def load() -> dict[str, Any] | None:
    if not BASELINE_PATH.exists():
        return None
    try:
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def save(baseline: dict[str, Any]) -> None:
    """Atomic write: tmp file + rename, serialised behind a lock."""
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = BASELINE_PATH.with_suffix(".json.tmp")
    with _WRITE_LOCK:
        tmp.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
        tmp.replace(BASELINE_PATH)


def pin_from_job(
    job_result: dict[str, Any],
    recipe: dict[str, Any],
    overrides: dict[str, Any] | None,
    layer: str | None,
) -> dict[str, Any]:
    kpis = job_result.get("kpis") or {}
    baseline = {
        "layer": layer,
        "recipe": recipe,
        "overrides": overrides or {},
        "kpis": {k: kpis.get(k) for k in _KPI_KEYS},
        "run_file": job_result.get("run_file"),
        "pinned_at": __import__("time").time(),
    }
    save(baseline)
    return baseline


def pin_from_history_row(row: dict[str, Any]) -> dict[str, Any]:
    """Pin a past run directly from a history.csv row."""

    def _num(k: str) -> float | None:
        v = row.get(k)
        if v in (None, ""):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    baseline = {
        "layer": row.get("layer"),
        "recipe": {
            "pair": row.get("pair"),
            "main_tf": row.get("main_tf"),
            "sub_tf": row.get("sub_tf"),
        },
        "overrides": {},
        "kpis": {k: _num(k) for k in _KPI_KEYS},
        "run_file": row.get("run_file"),
        "pinned_at": __import__("time").time(),
        "pinned_from": "history",
    }
    save(baseline)
    return baseline


def delta(result_kpis: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    base = load()
    if not base:
        return None
    base_kpis = base.get("kpis") or {}
    out: dict[str, dict[str, Any]] = {}
    for k in _KPI_KEYS:
        bv = base_kpis.get(k)
        nv = result_kpis.get(k)
        if bv is None or nv is None:
            out[k] = {"baseline": bv, "new": nv, "delta": None, "delta_pct": None}
            continue
        try:
            d = float(nv) - float(bv)
            dp = (d / abs(float(bv))) * 100.0 if float(bv) != 0 else None
            out[k] = {"baseline": bv, "new": nv, "delta": d, "delta_pct": dp}
        except Exception:
            out[k] = {"baseline": bv, "new": nv, "delta": None, "delta_pct": None}
    return out
