"""One-at-a-time background backtest runner.

The API exposes a single slot — if a job is running, new POST /api/run requests
get a 409 until it finishes. Jobs are keyed by a short UUID.

Jobs always (re)generate the EA from a *recipe* (pair, main_tf, sub_tf, level)
on the server, so engine-mapping callables never need to round-trip through
JSON.
"""

from __future__ import annotations

import csv
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ff import harness
from ff.defaults.complexity import complexity_to_ea
from ff.defaults.overrides import apply_overrides
from ff.inspect import inspect_dict

from . import baselines

ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / "artifacts"
HISTORY_CSV = ARTIFACTS_DIR / "history.csv"
RUNS_DIR = ARTIFACTS_DIR / "runs"


_lock = threading.Lock()
_jobs: dict[str, "JobState"] = {}


class JobState:
    def __init__(self, job_id: str, recipe: dict[str, Any]) -> None:
        self.id = job_id
        self.recipe = recipe
        self.overrides: dict[str, Any] = {}
        self.status = "running"
        self.progress = 0.0
        self.message = "queued"
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.error: str | None = None
        self.result: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "recipe": self.recipe,
            "overrides": self.overrides,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
        }


def get(job_id: str) -> Optional[JobState]:
    return _jobs.get(job_id)


def list_jobs() -> list[dict[str, Any]]:
    return [j.as_dict() for j in _jobs.values()]


def any_running() -> bool:
    return any(j.status == "running" for j in _jobs.values())


def start(
    recipe: dict[str, Any],
    *,
    n_trials: int,
    seed: int,
    layer_name: str | None,
    overrides: dict[str, Any] | None = None,
    artifact_mode: str = "auto",
    chunk_size: int | None = None,
) -> str:
    """Kick off a backtest thread. Returns job_id.

    ``recipe`` = ``{pair, main_tf, sub_tf?, level, name?}``.
    ``overrides`` = optional per-knob / per-group / global edits applied on top
    of the complexity preset.
    Raises ``RuntimeError`` if another job is already running.
    """
    if not _lock.acquire(blocking=False):
        raise RuntimeError("another job is already running")

    job_id = uuid.uuid4().hex[:12]
    state = JobState(job_id, recipe)
    state.overrides = overrides or {}
    _jobs[job_id] = state

    def _worker() -> None:
        try:
            ea = complexity_to_ea(
                level=int(recipe["level"]),
                pair=recipe["pair"],
                main_tf=recipe["main_tf"],
                sub_tf=recipe.get("sub_tf"),
                name=recipe.get("name"),
            )
            if overrides:
                ea = apply_overrides(ea, overrides)
            # Propagate the user-requested backtest window into EA.data so the
            # harness slices main_df / sub_df accordingly.
            start_date = recipe.get("start_date")
            end_date = recipe.get("end_date")
            if start_date or end_date:
                ea.setdefault("data", {})
                if start_date:
                    ea["data"]["start_date"] = start_date
                if end_date:
                    ea["data"]["end_date"] = end_date
            label = layer_name or ea.get("name") or f"web_{job_id}"

            def cb(frac: float, msg: str) -> None:
                state.progress = max(state.progress, min(1.0, float(frac)))
                state.message = msg

            result = harness.run(
                ea,
                layer_name=label,
                seed=seed,
                n_trials=n_trials,
                open_browser=False,
                progress_cb=cb,
                artifact_mode=artifact_mode,
                chunk_size=chunk_size,
            )
            state.progress = 1.0
            state.message = "done"

            kpis = _kpi_block(result)
            state.result = {
                "kpis": kpis,
                "best_params_english": _best_params_english(ea, result),
                "equity_curve": _equity_curve_from_result(result),
                "run_file": result.get("run_file"),
                "layer": label,
                "raw": {k: v for k, v in result.items() if _jsonable(v)},
                "baseline_delta": baselines.delta(kpis),
            }
            state.status = "done"
        except Exception as e:
            state.status = "error"
            state.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        finally:
            state.finished_at = time.time()
            _lock.release()

    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception:
        # Thread couldn't be created — release the slot so the next request
        # isn't permanently blocked.
        _jobs.pop(job_id, None)
        try:
            _lock.release()
        except RuntimeError:
            pass
        raise
    return job_id


def _jsonable(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool, type(None), list, dict))


def _kpi_block(result: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "trades",
        "win_rate_pct",
        "total_pips",
        "expectancy_pips",
        "max_dd_pct",
        "profit_factor",
        "sharpe",
        "return_pct",
    )
    return {k: result.get(k) for k in keys}


def _equity_curve_from_result(result: dict[str, Any]) -> list[float]:
    run_file = result.get("run_file")
    if not run_file:
        return []
    path = RUNS_DIR / run_file if not Path(run_file).is_absolute() else Path(run_file)
    if not path.exists():
        return []
    try:
        data = np.load(path, allow_pickle=True)
    except Exception:
        return []
    for key in ("equity", "equity_array", "equity_curve", "pnl_cumsum"):
        if key in data.files:
            arr = np.asarray(data[key], dtype=np.float64).ravel()
            if arr.size > 2000:
                idx = np.linspace(0, arr.size - 1, 2000).astype(int)
                arr = arr[idx]
            return arr.tolist()
    for key in ("pnl", "pnl_array"):
        if key in data.files:
            pnl = np.asarray(data[key], dtype=np.float64).ravel()
            arr = np.cumsum(pnl)
            if arr.size > 2000:
                idx = np.linspace(0, arr.size - 1, 2000).astype(int)
                arr = arr[idx]
            return arr.tolist()
    return []


_ENGLISH_LABELS = {
    "stop_loss": "Stop loss",
    "take_profit": "Take profit",
    "trailing": "Trailing stop",
    "breakeven": "Breakeven",
    "partial": "Partial close",
    "stale": "Stale exit",
    "session": "Session",
    "max_bars": "Max bars in trade",
    "days": "Days of week",
}


def _best_params_english(ea: dict[str, Any], result: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    run_file = result.get("run_file")
    best_trial = None
    variant_map: list[dict[str, Any]] = []
    best_variant_id: int | None = None
    if run_file:
        path = RUNS_DIR / run_file if not Path(run_file).is_absolute() else Path(run_file)
        if path.exists():
            try:
                data = np.load(path, allow_pickle=True)
                if "best_trial_json" in data.files:
                    import json as _json

                    best_trial = _json.loads(str(data["best_trial_json"]))
                    if isinstance(best_trial, dict):
                        best_variant_id = best_trial.get("signal_variant")
                if "variant_map_json" in data.files:
                    import json as _json

                    variant_map = _json.loads(str(data["variant_map_json"]))
            except Exception:
                pass

    for k in ("strategy", "best_variant_id", "best_variant_family"):
        v = result.get(k)
        if v is not None:
            lines.append(f"{k}: {v}")

    if best_variant_id is not None and 0 <= best_variant_id < len(variant_map):
        v = variant_map[best_variant_id]
        params_txt = ", ".join(f"{k}={vv}" for k, vv in (v.get("params") or {}).items())
        lines.append(f"Winning signal: {v.get('family')} ({params_txt})")

    if isinstance(best_trial, dict):
        engine = best_trial.get("engine") or {}
        for key, label in _ENGLISH_LABELS.items():
            node = engine.get(key)
            if node is None:
                continue
            lines.append(f"{label}: {_describe_engine_node(node)}")

    try:
        tree = inspect_dict(ea)
        lines.append(f"effective dims: {len(tree.get('engine_schema', []))} top-level knobs")
    except Exception:
        pass
    return lines


def _describe_engine_node(node: Any) -> str:
    """Render one slice of a best_trial engine entry as plain English."""
    if isinstance(node, dict):
        sel = node.get("selector")
        if sel is not None:
            arm = node.get(sel, {})
            if isinstance(arm, dict) and arm:
                inner = ", ".join(f"{k}={_fmt_val(v)}" for k, v in arm.items())
                return f"{sel} ({inner})"
            return str(sel)
        test = node.get("test")
        if test in (True, False):
            if not test:
                return "OFF"
            when_on = node.get("when_on") or {}
            if not when_on:
                return "ON"
            parts: list[str] = []
            for k, v in when_on.items():
                if isinstance(v, dict):
                    parts.append(f"{k}={_describe_engine_node(v)}")
                else:
                    parts.append(f"{k}={_fmt_val(v)}")
            return "ON (" + ", ".join(parts) + ")"
        return ", ".join(f"{k}={_fmt_val(v)}" for k, v in node.items())
    return _fmt_val(node)


def _fmt_val(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def history_rows() -> list[dict[str, Any]]:
    if not HISTORY_CSV.exists():
        return []
    with HISTORY_CSV.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _safe_npz_name(run_file: str) -> str:
    """Return the basename of a run_file path only if it is a plain .npz
    filename with no traversal. Empty string if invalid."""
    if not run_file:
        return ""
    name = Path(run_file).name
    if not name.endswith(".npz") or "/" in name or "\\" in name or ".." in name:
        return ""
    return name


def delete_runs(run_files: list[str]) -> int:
    """Remove matching rows from history.csv and delete the corresponding
    npz files under artifacts/runs/. Returns count of npz files removed."""
    wanted = {_safe_npz_name(r) for r in run_files}
    wanted.discard("")
    if not wanted:
        return 0

    if HISTORY_CSV.exists():
        with HISTORY_CSV.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            kept = [row for row in reader if _safe_npz_name(row.get("run_file", "")) not in wanted]
        if fieldnames:
            with HISTORY_CSV.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(kept)

    removed = 0
    for name in wanted:
        p = RUNS_DIR / name
        if p.exists() and p.parent == RUNS_DIR:
            p.unlink()
            removed += 1
    return removed


def delete_all_runs() -> int:
    """Clear history.csv entirely and delete every npz under artifacts/runs/.
    Returns count of npz files removed."""
    removed = 0
    if RUNS_DIR.exists():
        for p in RUNS_DIR.glob("*.npz"):
            p.unlink()
            removed += 1
    if HISTORY_CSV.exists():
        HISTORY_CSV.unlink()
    return removed


# ── Download jobs ──────────────────────────────────────────────────────────
# Separate lock from backtest runs — a download is network-bound, a sweep is
# CPU-bound, and we want them to coexist.

from collections import deque  # noqa: E402
from datetime import date as _date  # noqa: E402 — kept near the section it serves

DOWNLOADS_DIR = ARTIFACTS_DIR / "downloads"
_download_lock = threading.Lock()
_downloads: dict[str, "DownloadJob"] = {}


class DownloadJob:
    def __init__(self, job_id: str, pair: str, tf: str, start: _date, end: _date, append: bool) -> None:
        self.id = job_id
        self.pair = pair
        self.tf = tf
        self.start = start.isoformat()
        self.end = end.isoformat()
        self.append = append
        self.status = "running"
        self.message = "queued"
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.error: str | None = None
        self.result: dict[str, Any] | None = None
        self._cancel = False
        self._log: deque[str] = deque(maxlen=2000)

    def append_log(self, line: str) -> None:
        stamped = f"[{time.strftime('%H:%M:%S')}] {line}"
        self._log.append(stamped)

    def full_log(self) -> str:
        return "\n".join(self._log)

    def tail_lines(self, limit: int = 200) -> list[str]:
        return list(self._log)[-limit:]

    def cancel(self) -> None:
        self._cancel = True
        self.append_log("cancel requested")

    def was_cancelled(self) -> bool:
        return self._cancel

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pair": self.pair,
            "tf": self.tf,
            "start": self.start,
            "end": self.end,
            "append": self.append,
            "status": self.status,
            "message": self.message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
            "tail_lines": self.tail_lines(),
        }


def get_download(job_id: str) -> Optional[DownloadJob]:
    return _downloads.get(job_id)


def list_downloads() -> list[dict[str, Any]]:
    return [d.as_dict() for d in _downloads.values()]


def start_download(pair: str, tf: str, start: _date, end: _date, append: bool = True) -> str:
    """Kick off a download thread. Returns job_id."""
    if not _download_lock.acquire(blocking=False):
        raise RuntimeError("another download is already running")

    job_id = uuid.uuid4().hex[:12]
    state = DownloadJob(job_id, pair, tf, start, end, append)
    _downloads[job_id] = state

    def _worker() -> None:
        try:
            from ff.data import m1_bi5_downloader as _m1dl
            from ff.data import resample as _rs

            def _log_cb(msg: str) -> None:
                state.message = msg
                state.append_log(msg)

            state.append_log(f"downloading {pair} {tf}  {start} → {end}  append={append}")

            # Only M1 is supported by the raw-bi5 path. Higher TFs always
            # come from the rollup chain — do not round-trip the network
            # for anything else.
            if tf != "M1":
                raise ValueError(f"only M1 direct download is supported — got tf={tf!r}. Fetch M1, rollup derives M5/M15/M30/H1/H4/D/W.")

            result = _m1dl.download(pair, start, end, append=append, log_cb=_log_cb, cancel_cb=state.was_cancelled)
            state.result = result
            state.status = "cancelled" if state.was_cancelled() else "done"
            state.message = state.status
            state.append_log(f"finished — new_bars={result.get('new_bars')} total_bars={result.get('total_bars')}")
            # Auto fan-out: derive all higher TFs so the Claude-Backtester
            # invariant (one fetch, every TF on disk) holds. Tick downloads
            # handle their own tick→M1 chain.
            if state.status == "done":
                try:
                    state.append_log("deriving higher TFs from M1 …")
                    written = _rs.derive_higher_tfs(pair)
                    state.append_log(f"derived {', '.join(p.stem.split('_')[-1] for p in written)}")
                except Exception as exc:
                    state.append_log(f"derive error: {exc}")
        except Exception as e:
            state.status = "error"
            state.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            state.append_log(f"ERROR: {e}")
        finally:
            state.finished_at = time.time()
            _download_lock.release()

    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception:
        _downloads.pop(job_id, None)
        try:
            _download_lock.release()
        except RuntimeError:
            pass
        raise
    return job_id


# ── Tick download jobs ─────────────────────────────────────────────────────
# Separate lock so a tick fetch and a bar fetch can run in parallel — both are
# network-bound but hit different endpoints and different files.

_tick_lock = threading.Lock()
_tick_jobs: dict[str, "TickDownloadJob"] = {}


class TickDownloadJob(DownloadJob):
    """Bar-download lookalike for the tick pipeline. Keeps the same
    ``as_dict`` / ``full_log`` / ``tail_lines`` surface the frontend already
    polls."""

    def __init__(self, job_id: str, pair: str, start: _date, end: _date, append: bool) -> None:
        super().__init__(job_id, pair, "TICK", start, end, append)


def get_tick_download(job_id: str) -> Optional[TickDownloadJob]:
    return _tick_jobs.get(job_id)


def list_tick_downloads() -> list[dict[str, Any]]:
    return [d.as_dict() for d in _tick_jobs.values()]


def start_tick_download(pair: str, start: _date, end: _date, append: bool = True) -> str:
    """Kick off a tick download + tick→M1 + M5..W fan-out. Returns job_id."""
    if not _tick_lock.acquire(blocking=False):
        raise RuntimeError("another tick download is already running")

    job_id = uuid.uuid4().hex[:12]
    state = TickDownloadJob(job_id, pair, start, end, append)
    _tick_jobs[job_id] = state

    def _worker() -> None:
        try:
            from ff.data import resample as _rs
            from ff.data import tick_downloader as _tdl

            def _log_cb(msg: str) -> None:
                state.message = msg
                state.append_log(msg)

            state.append_log(f"downloading {pair} TICK  {start} → {end}  append={append}")
            result = _tdl.download(pair, start, end, append=append, log_cb=_log_cb, cancel_cb=state.was_cancelled)
            state.result = result
            if state.was_cancelled():
                state.status = "cancelled"
                state.message = "cancelled"
                state.append_log("cancelled — skipping resample")
                return
            state.append_log(f"ticks stored — new_rows={result.get('new_rows')} total_rows={result.get('total_rows')}")

            state.append_log("tick → M1 …")
            m1_path = _rs.tick_to_m1(pair)
            state.append_log(f"wrote {m1_path.name}")

            state.append_log("deriving higher TFs from M1 …")
            written = _rs.derive_higher_tfs(pair)
            state.append_log(f"derived {', '.join(p.stem.split('_')[-1] for p in written)}")
            state.status = "done"
            state.message = "done"
        except Exception as e:
            state.status = "error"
            state.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            state.append_log(f"ERROR: {e}")
        finally:
            state.finished_at = time.time()
            _tick_lock.release()

    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception:
        _tick_jobs.pop(job_id, None)
        try:
            _tick_lock.release()
        except RuntimeError:
            pass
        raise
    return job_id


# ── One-shot resample jobs ─────────────────────────────────────────────────
# Manual "Roll up from M1" / "Rebuild from ticks" inventory buttons go through
# these. No lock — they're fast and safe to run in parallel with downloads.


def run_derive_now(pair: str) -> dict[str, Any]:
    """Sync call — returns the list of TFs written plus the durations."""
    from ff.data import resample as _rs

    t0 = time.time()
    written = _rs.derive_higher_tfs(pair)
    return {
        "pair": pair,
        "derived": [p.stem.split("_")[-1] for p in written],
        "elapsed_s": round(time.time() - t0, 3),
    }


def run_tick_to_m1_now(pair: str) -> dict[str, Any]:
    """Sync call — returns the written path."""
    from ff.data import resample as _rs

    t0 = time.time()
    path = _rs.tick_to_m1(pair)
    return {"pair": pair, "path": str(path), "elapsed_s": round(time.time() - t0, 3)}
