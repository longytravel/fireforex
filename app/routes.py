"""HTTP endpoints for the Fire Forex web UI."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from ff.defaults.complexity import complexity_to_ea
from ff.defaults.overrides import apply_overrides, flatten_schema
from ff.harness import METRIC_COLUMNS as _HARNESS_METRIC_COLUMNS, pick_best
from ff.inspect import inspect_dict
from ff.preflight import preflight_dict
from ff.schema_json import ea_to_dict
from ff.VERSION import VERSION

from . import baselines, jobs, live_jobs
from .pairs_scan import scan_pairs_cached


router = APIRouter(prefix="/api")


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"
EAS_DIR = PROJECT_ROOT / "eas"
DOCS_DIR = PROJECT_ROOT / "docs"


# ── Catalog ────────────────────────────────────────────────────────────

@router.get("/pairs")
def get_pairs() -> dict[str, Any]:
    pairs = scan_pairs_cached()
    if not pairs:
        raise HTTPException(status_code=503, detail="no data roots found — check G:\\My Drive\\BackTestData")
    from ff.data.groups import group_pairs
    return {"pairs": pairs, "groups": group_pairs(list(pairs))}


@router.get("/timeframes")
def get_timeframes() -> dict[str, Any]:
    return {"timeframes": ["M1", "M5", "M15", "M30", "H1", "H4", "D"]}


# ── Defaults (complexity → EA) ─────────────────────────────────────────

def _build_defaults_bundle(pair: str, main_tf: str, sub_tf: str | None, level: int,
                            name: str | None = None, overrides: dict | None = None,
                            start_date: str | None = None,
                            end_date: str | None = None) -> dict[str, Any]:
    try:
        ea = complexity_to_ea(level=level, pair=pair, main_tf=main_tf, sub_tf=sub_tf, name=name)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"cannot build EA: {e}") from e

    if overrides:
        try:
            ea = apply_overrides(ea, overrides)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"override error: {e}") from e

    if start_date or end_date:
        ea.setdefault("data", {})
        if start_date:
            ea["data"]["start_date"] = start_date
        if end_date:
            ea["data"]["end_date"] = end_date

    return {
        "recipe": {"pair": pair, "main_tf": main_tf,
                   "sub_tf": ea.get("data", {}).get("sub_tf"),
                   "level": level, "name": ea.get("name"),
                   "start_date": ea.get("data", {}).get("start_date"),
                   "end_date": ea.get("data", {}).get("end_date")},
        "overrides": overrides or {},
        "ea": ea_to_dict(ea),
        "inspect": inspect_dict(ea),
        "preflight": preflight_dict(ea, n_trials=2000),
        "flat_schema": {
            "engine": flatten_schema(ea.get("engine_schema", {})),
            "signals": flatten_schema(ea.get("signals", {})),
        },
    }


@router.get("/defaults")
def get_defaults(pair: str, main_tf: str, level: int = 6,
                 sub_tf: str | None = None, name: str | None = None) -> dict[str, Any]:
    if level < 1 or level > 10:
        raise HTTPException(status_code=400, detail="level must be 1..10")
    return _build_defaults_bundle(pair, main_tf, sub_tf, level, name)


@router.post("/defaults")
def post_defaults(body: dict[str, Any]) -> dict[str, Any]:
    for k in ("pair", "main_tf", "level"):
        if k not in body:
            raise HTTPException(status_code=400, detail=f"missing '{k}'")
    level = int(body["level"])
    if level < 1 or level > 10:
        raise HTTPException(status_code=400, detail="level must be 1..10")
    return _build_defaults_bundle(
        body["pair"], body["main_tf"], body.get("sub_tf"), level,
        body.get("name"), body.get("overrides"),
        body.get("start_date"), body.get("end_date"),
    )


# ── Preflight (POST recipe) ────────────────────────────────────────────

@router.post("/preflight")
def post_preflight(body: dict[str, Any]) -> dict[str, Any]:
    for k in ("pair", "main_tf", "level"):
        if k not in body:
            raise HTTPException(status_code=400, detail=f"missing '{k}'")
    try:
        ea = complexity_to_ea(level=int(body["level"]), pair=body["pair"],
                              main_tf=body["main_tf"], sub_tf=body.get("sub_tf"),
                              name=body.get("name"))
        if body.get("overrides"):
            ea = apply_overrides(ea, body["overrides"])
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return preflight_dict(ea, n_trials=int(body.get("n_trials", 2000)))


# ── Inspect ────────────────────────────────────────────────────────────

@router.post("/inspect")
def post_inspect(body: dict[str, Any]) -> dict[str, Any]:
    for k in ("pair", "main_tf", "level"):
        if k not in body:
            raise HTTPException(status_code=400, detail=f"missing '{k}'")
    try:
        ea = complexity_to_ea(level=int(body["level"]), pair=body["pair"],
                              main_tf=body["main_tf"], sub_tf=body.get("sub_tf"),
                              name=body.get("name"))
        if body.get("overrides"):
            ea = apply_overrides(ea, body["overrides"])
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return inspect_dict(ea)


# ── Runs (jobs) ────────────────────────────────────────────────────────

@router.post("/run")
def post_run(body: dict[str, Any]) -> dict[str, Any]:
    recipe = body.get("recipe") or {}
    for k in ("pair", "main_tf", "level"):
        if k not in recipe:
            raise HTTPException(status_code=400, detail=f"recipe missing '{k}'")

    # Reject unsupported timeframes before they reach the worker. Mirrors
    # ff/harness.py:403-409 so a stale frontend (or a curl by hand) gets a
    # clean 400 instead of a mid-run crash inside the Rust engine.
    from ff import harness as _h
    _known = set(_h.TF_MINUTES.keys())
    _main = recipe["main_tf"]
    _sub = recipe.get("sub_tf")
    if _main not in _known:
        raise HTTPException(status_code=400,
                            detail=f"main_tf {_main!r} not supported; known={sorted(_known)}")
    if _sub is not None and _sub not in _known:
        raise HTTPException(status_code=400,
                            detail=f"sub_tf {_sub!r} not supported; known={sorted(_known)}")
    if _sub is not None and _h.TF_MINUTES[_sub] >= _h.TF_MINUTES[_main]:
        raise HTTPException(status_code=400,
                            detail=f"sub_tf must be finer than main_tf (got {_sub} vs {_main})")

    n_trials = int(body.get("n_trials", 2000))
    seed = int(body.get("seed", 42))
    layer_name = body.get("layer_name")
    overrides = body.get("overrides") or {}
    if not (10 <= n_trials <= 50_000):
        raise HTTPException(status_code=400, detail="n_trials must be 10..50000")

    # Normalise optional date window from either the recipe or the top level.
    for key in ("start_date", "end_date"):
        val = body.get(key) or recipe.get(key)
        if val:
            if not _ISO_DATE_RE.match(str(val)):
                raise HTTPException(status_code=400, detail=f"bad {key}: expected YYYY-MM-DD")
            recipe[key] = val

    try:
        job_id = jobs.start(recipe, n_trials=n_trials, seed=seed, layer_name=layer_name,
                            overrides=overrides)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"job_id": job_id}


@router.get("/jobs")
def get_jobs() -> dict[str, Any]:
    return {"jobs": jobs.list_jobs()}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    j = jobs.get(job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="job not found")
    return j.as_dict()


# ── Version ────────────────────────────────────────────────────────────

@router.get("/version")
def get_version() -> dict[str, Any]:
    return {"version": VERSION}


# ── History ────────────────────────────────────────────────────────────

@router.get("/history")
def get_history() -> dict[str, Any]:
    return {"rows": jobs.history_rows()}


@router.post("/history/delete")
def post_history_delete(body: dict[str, Any]) -> dict[str, Any]:
    run_files = body.get("run_files") or []
    if not isinstance(run_files, list) or not run_files:
        raise HTTPException(status_code=400, detail="run_files must be a non-empty list")
    if not all(isinstance(r, str) for r in run_files):
        raise HTTPException(status_code=400, detail="run_files must be strings")
    removed = jobs.delete_runs(run_files)
    return {"removed": removed}


@router.post("/history/clear")
def post_history_clear() -> dict[str, Any]:
    removed = jobs.delete_all_runs()
    return {"removed": removed}


# ── Saved EAs ──────────────────────────────────────────────────────────

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/eas")
def list_eas() -> dict[str, Any]:
    if not EAS_DIR.exists():
        return {"eas": []}
    out = []
    for p in sorted(EAS_DIR.glob("*.json")):
        out.append({"name": p.stem, "path": str(p.relative_to(PROJECT_ROOT))})
    return {"eas": out}


@router.post("/eas")
def save_ea(body: dict[str, Any]) -> dict[str, Any]:
    """Save a recipe as ``eas/user_<name>.json``. Body: {name, recipe}."""
    name = str(body.get("name") or "").strip()
    recipe = body.get("recipe") or {}
    for k in ("pair", "main_tf", "level"):
        if k not in recipe:
            raise HTTPException(status_code=400, detail=f"recipe missing '{k}'")
    if not _SAFE_NAME.match(name):
        raise HTTPException(status_code=400, detail="name must match [A-Za-z0-9_-]{1,64}")
    target = EAS_DIR / f"user_{name}.json"
    target.write_text(json.dumps({"type": "recipe", "recipe": recipe}, indent=2), encoding="utf-8")
    return {"saved": str(target.relative_to(PROJECT_ROOT))}


@router.get("/eas/{name}")
def load_ea(name: str) -> dict[str, Any]:
    if not _SAFE_NAME.match(name):
        raise HTTPException(status_code=400, detail="bad name")
    target = EAS_DIR / f"user_{name}.json"
    if not target.exists():
        raise HTTPException(status_code=404, detail="not found")
    return json.loads(target.read_text(encoding="utf-8"))


# ── Explain ────────────────────────────────────────────────────────────

def _parse_knob_explanations() -> dict[str, dict[str, str]]:
    path = DOCS_DIR / "knob-explanations.md"
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            out[current] = {}
        elif line.startswith("- ") and current:
            body = line[2:]
            if ":" in body:
                k, v = body.split(":", 1)
                out[current][k.strip().lower()] = v.strip()
    return out


_EXPLAIN_CACHE: dict[str, dict[str, str]] | None = None


def _explain_all() -> dict[str, dict[str, str]]:
    global _EXPLAIN_CACHE
    if _EXPLAIN_CACHE is None:
        _EXPLAIN_CACHE = _parse_knob_explanations()
    return _EXPLAIN_CACHE


@router.get("/explain-bundle")
def get_explain_bundle() -> dict[str, Any]:
    return {"items": _explain_all()}


@router.get("/explain/{knob:path}")
def get_explain(knob: str) -> dict[str, Any]:
    data = _explain_all().get(knob)
    if not data:
        raise HTTPException(status_code=404, detail=f"no explanation for '{knob}'")
    return {"knob": knob, **data}


# ── Baseline ──────────────────────────────────────────────────────────

@router.get("/baseline")
def get_baseline() -> dict[str, Any]:
    return {"baseline": baselines.load()}


@router.post("/baseline")
def set_baseline(body: dict[str, Any]) -> dict[str, Any]:
    """Pin a baseline from an in-flight job (``job_id``) or a history row
    (``run_file`` or ``layer``)."""
    job_id = body.get("job_id")
    if job_id:
        j = jobs.get(job_id)
        if j is None or j.status != "done" or not j.result:
            raise HTTPException(status_code=400, detail="job not complete")
        pinned = baselines.pin_from_job(
            j.result, j.recipe, j.overrides, j.result.get("layer") or j.recipe.get("name"),
        )
        return {"baseline": pinned}

    run_file = body.get("run_file")
    layer = body.get("layer")
    if not (run_file or layer):
        raise HTTPException(status_code=400, detail="need job_id, run_file, or layer")
    rows = jobs.history_rows()
    match = None
    for r in reversed(rows):
        if run_file and r.get("run_file") == run_file:
            match = r; break
        if layer and r.get("layer") == layer:
            match = r; break
    if match is None:
        raise HTTPException(status_code=404, detail="no matching history row")
    pinned = baselines.pin_from_history_row(match)
    return {"baseline": pinned}


@router.delete("/baseline")
def clear_baseline() -> dict[str, Any]:
    if baselines.BASELINE_PATH.exists():
        baselines.BASELINE_PATH.unlink()
    return {"baseline": None}


# ── Scatter (per-trial results) ────────────────────────────────────────

_RUN_FILE_RE = re.compile(r"^[A-Za-z0-9_\-]+\.npz$")

# Scatter API schema. The harness registry covers the Rust metric columns;
# ``total_pips`` is appended by this endpoint from the per-trial PnL buffer.
_METRIC_COLUMN_KEYS: tuple[str, ...] = tuple(k for k, _, _ in _HARNESS_METRIC_COLUMNS) + ("total_pips",)
_METRIC_COLUMN_LABELS: tuple[str, ...] = tuple(lbl for _, lbl, _ in _HARNESS_METRIC_COLUMNS) + ("Total pips",)
_METRIC_COLUMN_GROUPS: tuple[str, ...] = tuple(grp for _, _, grp in _HARNESS_METRIC_COLUMNS) + ("Return",)
_METRIC_COLUMNS = _METRIC_COLUMN_KEYS  # back-compat alias

_SCATTER_MAX_POINTS = 5000


def _resolve_run_npz(run_file: str) -> Path:
    if not _RUN_FILE_RE.match(run_file):
        raise HTTPException(status_code=400, detail="bad run_file name")
    path = ARTIFACTS_DIR / "runs" / run_file
    if not path.exists() or path.parent != (ARTIFACTS_DIR / "runs"):
        raise HTTPException(status_code=404, detail="run file not found")
    return path


def _baseline_quality() -> float | None:
    """Best-quality score of the pinned baseline's run, if any. Used as the
    zero-point of the scatter colour gradient."""
    import numpy as np
    b = baselines.load()
    if not b:
        return None
    run_file = b.get("run_file")
    if not run_file:
        return None
    path = ARTIFACTS_DIR / "runs" / run_file
    if not path.exists():
        return None
    try:
        with np.load(path, allow_pickle=False) as z:
            if "quality" in z.files:
                return float(z["quality"].max())
    except Exception:
        return None
    return None


@router.get("/runs/{run_file}/scatter")
def get_scatter(run_file: str) -> dict[str, Any]:
    """Return per-trial metrics for scatter rendering. Decimates to
    ``_SCATTER_MAX_POINTS`` via stride sampling for large sweeps; the
    returned ``indices`` array preserves original trial indices so the
    click handler can map a point back to its true trial."""
    import numpy as np
    path = _resolve_run_npz(run_file)
    with np.load(path, allow_pickle=False) as z:
        if "per_trial_metrics" not in z.files:
            raise HTTPException(status_code=409,
                                detail="this run predates the scatter feature; re-run to enable")
        metrics = z["per_trial_metrics"]  # (n_trials, N_rust_cols) float32
        # Pad legacy runs (saved with 10 Rust cols) up to the current schema
        # so new columns show as NaN rather than wrap-around indices.
        n_rust_cols = len(_HARNESS_METRIC_COLUMNS)
        if metrics.shape[1] < n_rust_cols:
            pad = np.full((metrics.shape[0], n_rust_cols - metrics.shape[1]),
                          np.nan, dtype=metrics.dtype)
            metrics = np.concatenate([metrics, pad], axis=1)
        # Derive total_pips per trial — appended as the final column.
        if "per_trial_pnl" in z.files and "per_trial_n_trades" in z.files:
            pnl = z["per_trial_pnl"]
            n_tr = z["per_trial_n_trades"]
            mask = np.arange(pnl.shape[1])[None, :] < n_tr[:, None]
            total_pips = np.where(mask, pnl, 0.0).sum(axis=1, dtype=np.float64).astype(np.float32)
        else:
            total_pips = np.zeros(metrics.shape[0], dtype=np.float32)
        metrics = np.concatenate([metrics, total_pips[:, None]], axis=1)
    n_trials = int(metrics.shape[0])
    if n_trials > _SCATTER_MAX_POINTS:
        stride = int(np.ceil(n_trials / _SCATTER_MAX_POINTS))
        idx = np.arange(0, n_trials, stride, dtype=np.int32)
        metrics = metrics[idx]
    else:
        idx = np.arange(n_trials, dtype=np.int32)
    # Replace NaN with None for JSON (NaN is not valid JSON).
    metrics_list = [[None if (v != v) else float(v) for v in row] for row in metrics]
    return {
        "n_trials": n_trials,
        "n_points": int(metrics.shape[0]),
        "metric_columns": list(_METRIC_COLUMN_KEYS),
        "metric_labels": list(_METRIC_COLUMN_LABELS),
        "metric_groups": list(_METRIC_COLUMN_GROUPS),
        "metrics": metrics_list,
        "indices": idx.tolist(),
        "baseline_quality": _baseline_quality(),
    }


@router.get("/runs/{run_file}/trial/{trial_idx}")
def get_trial(run_file: str, trial_idx: int) -> dict[str, Any]:
    """Return one trial's equity curve + metric row for click-to-replay."""
    import numpy as np
    path = _resolve_run_npz(run_file)
    with np.load(path, allow_pickle=False) as z:
        if "per_trial_pnl" not in z.files or "per_trial_n_trades" not in z.files:
            raise HTTPException(status_code=409,
                                detail="this run predates the scatter feature; re-run to enable")
        n_trials = int(z["per_trial_metrics"].shape[0])
        if not (0 <= trial_idx < n_trials):
            raise HTTPException(status_code=404, detail=f"trial_idx out of range 0..{n_trials-1}")
        n_trades = int(z["per_trial_n_trades"][trial_idx])
        pnl = z["per_trial_pnl"][trial_idx, :n_trades].astype(np.float64)
        metrics_row = z["per_trial_metrics"][trial_idx]
    equity = np.cumsum(pnl).tolist() if n_trades > 0 else []
    total_pips = float(pnl.sum()) if n_trades > 0 else 0.0
    rust_keys = _METRIC_COLUMN_KEYS[:-1]  # drop total_pips (added below)
    metric_dict: dict[str, float | None] = {}
    for i, name in enumerate(rust_keys):
        if i < len(metrics_row):
            v = float(metrics_row[i])
            metric_dict[name] = None if v != v else v  # NaN → None for JSON
        else:
            metric_dict[name] = None
    metric_dict["total_pips"] = total_pips
    return {
        "trial_idx": trial_idx,
        "n_trades": n_trades,
        "equity": equity,
        "metrics": metric_dict,
    }


# ── Per-run trade log (live parity validator) ─────────────────────────

@router.get("/runs/{run_id}/trades.csv", include_in_schema=False)
def get_run_trades_csv(run_id: str) -> Response:
    """Stream the best-trial per-trade log for a completed run.

    Consumed by the live-parity reconciler to join against MT5 deals.
    """
    # run_id is the stem of the npz file (e.g. "baseline_v2_random_20260420_200445").
    # Resist path traversal.
    if "/" in run_id or "\\" in run_id or ".." in run_id:
        raise HTTPException(status_code=400, detail="invalid run id")
    if run_id.endswith(".npz"):
        run_id = run_id[:-4]

    run_file = ARTIFACTS_DIR / "runs" / f"{run_id}.npz"
    if not run_file.exists():
        raise HTTPException(status_code=404, detail=f"no run: {run_id}")

    import numpy as np
    import pandas as pd

    z = np.load(run_file, allow_pickle=True)
    if "trades" not in z.files:
        raise HTTPException(
            status_code=404,
            detail=f"run {run_id} has no trade log — re-run after the live-parity upgrade",
        )
    trades = z["trades"]
    df = pd.DataFrame(trades)
    # Coerce datetime64 columns to ISO strings so the CSV is portable.
    for col in ("entry_ts", "exit_ts"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col]).dt.strftime("%Y-%m-%dT%H:%M:%S")
    csv_body = df.to_csv(index=False)
    return Response(
        content=csv_body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{run_id}_trades.csv"'},
    )


# ── Live-parity runner ────────────────────────────────────────────────

@router.get("/live/status")
def get_live_status() -> dict[str, Any]:
    return live_jobs.get_host().status()


@router.post("/live/start")
def post_live_start(body: dict[str, Any]) -> dict[str, Any]:
    """Start the live runner.

    Body shape (all required unless noted):
      recipe: {pair, main_tf, sub_tf, level}
      overrides: {...} (UI override dict; may be empty)
      pairs: ["EUR_USD", "GBP_USD", ...]
      broker: {login, password, server, terminal_path?, deviation_pips?,
               magic_number?, symbol_map?}
      poll_interval_sec: optional float (default 10.0)
      size_lots: optional float (default 0.01)

    Broker credentials are NOT persisted to disk by this endpoint. They flow
    through memory into the runner thread. Rotating = POST /live/stop,
    edit `.env.live`, POST /live/start.
    """
    missing = [k for k in ("recipe", "overrides", "pairs", "broker") if k not in body]
    if missing:
        raise HTTPException(status_code=400, detail=f"missing fields: {missing}")

    host = live_jobs.get_host()
    state = host.start(
        recipe=body["recipe"],
        overrides=body.get("overrides") or {},
        pairs=list(body["pairs"]),
        broker_profile=body["broker"],
        poll_interval_sec=float(body.get("poll_interval_sec", 10.0)),
        size_lots=float(body.get("size_lots", 0.01)),
    )
    return host.status()


@router.post("/live/stop")
def post_live_stop() -> dict[str, Any]:
    live_jobs.get_host().stop()
    return live_jobs.get_host().status()


@router.post("/live/deploy_from_run")
def post_live_deploy_from_run(body: dict[str, Any]) -> dict[str, Any]:
    """One-click deploy: take a completed backtest run and make it trade live.

    Body: ``{run_id: "...", pairs: ["EUR_USD", ...]}``

    Pulls recipe + best-trial overrides from the npz, writes
    ``artifacts/live/service_config.json`` for the VPS service, pins the
    source run_id so the hourly auto-reconciler knows what to diff against,
    and starts the in-process runner if the caller is the VPS host.

    No MT5 credentials are taken through this endpoint — the VPS service
    reads them from ``.env.live`` on disk when it boots.
    """
    import json as _json
    from pathlib import Path as _Path
    import numpy as _np

    run_id = body.get("run_id")
    pairs = body.get("pairs") or []
    if not run_id or not pairs:
        raise HTTPException(status_code=400, detail="run_id + pairs required")

    # UI passes the basename with `.npz` suffix; endpoint used to double it.
    if run_id.endswith(".npz"):
        run_id = run_id[:-4]

    run_file = ARTIFACTS_DIR / "runs" / f"{run_id}.npz"
    if not run_file.exists():
        raise HTTPException(status_code=404, detail=f"no run {run_id}")

    z = _np.load(run_file, allow_pickle=True)
    best_trial = _json.loads(str(z["best_trial_json"]))

    # Reconstruct the recipe from the history.csv row for this run (pair,
    # main_tf, sub_tf). For now accept from the body as a fallback — it
    # matches the recipe the UI had posted at run time.
    recipe = body.get("recipe") or {}
    overrides = body.get("overrides") or {}

    live_dir = ARTIFACTS_DIR / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    service_config = {
        "source_run_id": run_id,
        "recipe": recipe,
        "overrides": overrides,
        "pairs": list(pairs),
        "best_trial": best_trial,
        "poll_interval_sec": float(body.get("poll_interval_sec", 1.0)),
        "size_lots": float(body.get("size_lots", 0.01)),
        "deviation_pips": float(body.get("deviation_pips", 3.0)),
        "magic_number": int(body.get("magic_number", 20260420)),
        "symbol_map": body.get("symbol_map") or {},
        "auto_reconcile_interval_min": int(body.get("auto_reconcile_interval_min", 60)),
    }
    (live_dir / "service_config.json").write_text(
        _json.dumps(service_config, default=str, indent=2), encoding="utf-8"
    )

    # Pin the source run so the auto-reconciler knows what to diff against.
    (live_dir / "pinned_run.json").write_text(
        _json.dumps({"run_id": run_id}, indent=2), encoding="utf-8"
    )

    return {
        "ok": True,
        "source_run_id": run_id,
        "service_config_path": str((live_dir / "service_config.json").relative_to(ARTIFACTS_DIR.parent)),
        "note": "VPS: restart ff-live-runner Scheduled Task. Local dev: POST /api/live/start with broker creds.",
    }


@router.get("/live/plans")
def get_live_plans(
    since: str | None = None,
    pair: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    return {"plans": live_jobs.tail_plans(since_ts=since, pair=pair, limit=limit)}


@router.get("/live/positions")
def get_live_positions() -> dict[str, Any]:
    return {"positions": live_jobs._read_open_positions()}


@router.post("/live/reconcile/run")
def post_live_reconcile(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the reconciler across today's plans + MT5 deal history.

    Body (optional): { "run_id": "...", "tolerances": { ... } }
    If ``run_id`` is provided, backtest trades are loaded from that npz.
    Otherwise the endpoint assumes the user runs it manually via
    ``scripts/run_reconcile.py`` with explicit inputs.
    """
    import time as _t
    from ff.live import reconcile as _recon
    import pandas as _pd

    body = body or {}
    run_id = body.get("run_id")
    if not run_id:
        raise HTTPException(
            status_code=400,
            detail="run_id required — specify which backtest run's trade log to reconcile against",
        )
    if run_id.endswith(".npz"):
        run_id = run_id[:-4]
    run_file = ARTIFACTS_DIR / "runs" / f"{run_id}.npz"
    if not run_file.exists():
        raise HTTPException(status_code=404, detail=f"no run {run_id}")

    import numpy as _np
    z = _np.load(run_file, allow_pickle=True)
    if "trades" not in z.files:
        raise HTTPException(status_code=404, detail="run has no trade log")
    bt = _pd.DataFrame(z["trades"])
    bt["entry_ts"] = _pd.to_datetime(bt["entry_ts"], utc=True)
    bt["exit_ts"] = _pd.to_datetime(bt["exit_ts"], utc=True)
    bt["pair"] = body.get("pair", "EUR_USD")  # single-pair golden path for v1

    live_df = _pd.DataFrame([])  # placeholder until MT5 history ingest lands

    tol_raw = body.get("tolerances") or {}
    tol = _recon.Tolerances(**tol_raw) if tol_raw else None
    report = _recon.reconcile(bt, live_df, tol)

    out_dir = ARTIFACTS_DIR / "live" / "reconcile"
    stamp = _t.strftime("%Y%m%d_%H%M%S")
    html_path, _ = _recon.write_report(report, out_dir, stamp)
    # Drop a "latest" symlink-style file that the UI can point an iframe at.
    (out_dir / "latest.html").write_bytes(html_path.read_bytes())
    return {"stamp": stamp, "html": f"/api/live/reconcile/latest.html",
            "counts": report.counts}


@router.get("/live/reconcile/latest.html", include_in_schema=False)
def get_live_reconcile_latest() -> FileResponse:
    path = ARTIFACTS_DIR / "live" / "reconcile" / "latest.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="no reconcile report yet — POST /live/reconcile/run first")
    return FileResponse(path, media_type="text/html")


# ── Static comparison dashboard ────────────────────────────────────────

@router.get("/comparison.html", include_in_schema=False)
def get_comparison() -> FileResponse:
    path = ARTIFACTS_DIR / "comparison.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="no comparison dashboard yet — run a backtest first")
    return FileResponse(path, media_type="text/html")


# ── Data page: inventory / health / download ──────────────────────────

from datetime import date as _date  # noqa: E402
from fastapi.responses import PlainTextResponse  # noqa: E402

from ff.data import health as _data_health  # noqa: E402
from ff.data import inventory as _data_inventory  # noqa: E402


_PAIR_RE = re.compile(r"^[A-Z]{3}_[A-Z]{3}$")
_TF_RE = re.compile(r"^(M1|M5|M15|M30|H1|H4|D|W)$")


def _parse_iso_date(s: str, field: str) -> _date:
    if not isinstance(s, str) or not _ISO_DATE_RE.match(s):
        raise HTTPException(status_code=400, detail=f"bad {field}: expected YYYY-MM-DD")
    try:
        return _date.fromisoformat(s)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"bad {field}: {e}") from e


@router.get("/data/inventory")
def get_data_inventory() -> dict[str, Any]:
    return {"files": _data_inventory.scan(force=False)}


@router.post("/data/inventory/rescan")
def post_data_inventory_rescan() -> dict[str, Any]:
    return {"files": _data_inventory.scan(force=True)}


@router.get("/data/health/{pair}/{tf}")
def get_data_health(pair: str, tf: str) -> dict[str, Any]:
    if not _PAIR_RE.match(pair):
        raise HTTPException(status_code=400, detail="bad pair (want e.g. EUR_USD)")
    if not _TF_RE.match(tf):
        raise HTTPException(status_code=400, detail="bad tf")
    try:
        return _data_health.check(pair, tf)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        # Common case on Google Drive File Stream: truncated or partially-synced
        # parquet → pyarrow raises ArrowInvalid ("Parquet magic bytes not found
        # in footer"). Surface this as a structured report so the UI renders a
        # real status chip and retry path instead of a raw 500 traceback.
        name = type(e).__name__
        msg = str(e)
        truncation_markers = (
            "ArrowInvalid",
            "magic bytes",
            "Could not open Parquet",
            "Invalid parquet",
        )
        is_truncation = any(m in name or m in msg for m in truncation_markers)
        if not is_truncation:
            raise HTTPException(status_code=500, detail=f"{name}: {msg}") from e
        return {
            "pair": pair,
            "tf": tf,
            "bars": 0,
            "range": {"start": None, "end": None},
            "summary": "error",
            "error_detail": (
                f"{msg}. File is truncated or not fully synced from "
                f"Google Drive — redownload via Bars download."
            ),
            "nan_counts": {},
            "ohlc_violations": {},
            "timestamp_issues": {},
            "spread": {},
            "gap_samples": [],
        }


@router.post("/data/download")
def post_data_download(body: dict[str, Any]) -> dict[str, Any]:
    pair = body.get("pair") or ""
    tf = body.get("tf") or ""
    if not _PAIR_RE.match(pair):
        raise HTTPException(status_code=400, detail="bad pair")
    if not _TF_RE.match(tf):
        raise HTTPException(status_code=400, detail="bad tf")
    start = _parse_iso_date(body.get("start", ""), "start")
    end = _parse_iso_date(body.get("end", ""), "end")
    if end < start:
        raise HTTPException(status_code=400, detail="end must be >= start")
    append = bool(body.get("append", True))
    try:
        job_id = jobs.start_download(pair, tf, start, end, append)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"job_id": job_id}


@router.get("/data/download")
def get_data_downloads() -> dict[str, Any]:
    return {"downloads": jobs.list_downloads()}


@router.get("/data/download/{job_id}")
def get_data_download(job_id: str) -> dict[str, Any]:
    j = jobs.get_download(job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="download job not found")
    return j.as_dict()


@router.get("/data/download/{job_id}/log", response_class=PlainTextResponse)
def get_data_download_log(job_id: str) -> PlainTextResponse:
    j = jobs.get_download(job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="download job not found")
    return PlainTextResponse(j.full_log())


@router.post("/data/download/{job_id}/cancel")
def post_data_download_cancel(job_id: str) -> dict[str, Any]:
    j = jobs.get_download(job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="download job not found")
    j.cancel()
    return {"ok": True, "id": job_id}


# ── Tick downloads ────────────────────────────────────────────────────────

@router.post("/data/download/tick")
def post_data_download_tick(body: dict[str, Any]) -> dict[str, Any]:
    pair = body.get("pair") or ""
    if not _PAIR_RE.match(pair):
        raise HTTPException(status_code=400, detail="bad pair")
    start = _parse_iso_date(body.get("start", ""), "start")
    end = _parse_iso_date(body.get("end", ""), "end")
    if end < start:
        raise HTTPException(status_code=400, detail="end must be >= start")
    append = bool(body.get("append", True))
    try:
        job_id = jobs.start_tick_download(pair, start, end, append)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"job_id": job_id}


@router.get("/data/download/tick")
def get_data_tick_downloads() -> dict[str, Any]:
    return {"downloads": jobs.list_tick_downloads()}


@router.get("/data/download/tick/{job_id}")
def get_data_tick_download(job_id: str) -> dict[str, Any]:
    j = jobs.get_tick_download(job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="tick download job not found")
    return j.as_dict()


@router.get("/data/download/tick/{job_id}/log", response_class=PlainTextResponse)
def get_data_tick_download_log(job_id: str) -> PlainTextResponse:
    j = jobs.get_tick_download(job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="tick download job not found")
    return PlainTextResponse(j.full_log())


@router.post("/data/download/tick/{job_id}/cancel")
def post_data_tick_download_cancel(job_id: str) -> dict[str, Any]:
    j = jobs.get_tick_download(job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="tick download job not found")
    j.cancel()
    return {"ok": True, "id": job_id}


# ── Manual resample (inventory Roll-up / Rebuild buttons) ─────────────────

@router.post("/data/derive/{pair}")
def post_data_derive(pair: str) -> dict[str, Any]:
    if not _PAIR_RE.match(pair):
        raise HTTPException(status_code=400, detail="bad pair")
    try:
        return jobs.run_derive_now(pair)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e


@router.post("/data/tick-to-m1/{pair}")
def post_data_tick_to_m1(pair: str) -> dict[str, Any]:
    if not _PAIR_RE.match(pair):
        raise HTTPException(status_code=400, detail="bad pair")
    try:
        return jobs.run_tick_to_m1_now(pair)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e
