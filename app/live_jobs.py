"""Singleton host for the live-parity runner.

Mirrors ``app.jobs`` — one active runner at a time, cooperative stop via a
``threading.Event``. Runs in-process so it participates in the existing
``uvicorn`` lifecycle; the VPS Scheduled Task wrapper restarts the whole web
process on crash, same guarantee as jobs.py.

The runner is deliberately NOT hosted in a subprocess. A subprocess would
survive uvicorn reloads at the cost of a second credential-loading path and
a harder stop protocol; the trade-off isn't worth it until v2.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ARTIFACTS = _PROJECT_ROOT / "artifacts"
_LIVE_DIR = _ARTIFACTS / "live"


@dataclass
class LiveRunnerState:
    status: str = "idle"  # idle | starting | running | stopping | error
    started_at: float | None = None
    pairs: list[str] = field(default_factory=list)
    recipe: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None


class LiveRunnerHost:
    """Process-wide singleton wrapping :func:`ff.live.runner.run`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event = threading.Event()
        self._state = LiveRunnerState()

    # ------------------------------------------------------------------ start
    def start(
        self,
        recipe: dict[str, Any],
        overrides: dict[str, Any],
        pairs: list[str],
        broker_profile: dict[str, Any],
        *,
        poll_interval_sec: float = 10.0,
        size_lots: float = 0.01,
        best_trial: dict[str, Any] | None = None,
        max_open_per_pair: int = 1,
    ) -> LiveRunnerState:
        with self._lock:
            if self._state.status in ("starting", "running"):
                return self._state

            from ff.live.runner import BrokerCfg, LiveConfig
            from ff.live.runner import run as runner_run

            # LiveRunnerHost is the laptop-side in-process runner (UI
            # trigger). Each start gets its own instance_id scoped to
            # the host process, so artifacts do not collide with a
            # prior or concurrent deploy on the same machine.
            instance_id = f"laptop_host_{int(time.time())}"
            cfg = LiveConfig(
                instance_id=instance_id,
                recipe=recipe,
                overrides=overrides,
                pairs=list(pairs),
                broker=BrokerCfg(**broker_profile),
                poll_interval_sec=poll_interval_sec,
                size_lots=size_lots,
                best_trial=best_trial,
                max_open_per_pair=max_open_per_pair,
            )

            self._stop_event = threading.Event()
            self._state = LiveRunnerState(
                status="starting",
                started_at=time.time(),
                pairs=list(pairs),
                recipe=dict(recipe),
            )

            def _thread_target() -> None:
                try:
                    self._state.status = "running"
                    runner_run(cfg, self._stop_event)
                except Exception as exc:  # noqa: BLE001
                    LOG.exception("[live-host] runner crashed")
                    self._state.last_error = repr(exc)
                    self._state.status = "error"
                finally:
                    if self._state.status != "error":
                        self._state.status = "idle"

            self._thread = threading.Thread(
                target=_thread_target,
                daemon=True,
                name="ff-live-runner",
            )
            self._thread.start()
            return self._state

    # ------------------------------------------------------------------- stop
    def stop(self, timeout: float = 10.0) -> LiveRunnerState:
        with self._lock:
            if self._state.status not in ("running", "starting"):
                return self._state
            self._state.status = "stopping"
            self._stop_event.set()

        if self._thread is not None:
            self._thread.join(timeout=timeout)
        with self._lock:
            if self._state.status != "error":
                self._state.status = "idle"
            return self._state

    # ----------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        with self._lock:
            uptime = time.time() - self._state.started_at if self._state.started_at is not None and self._state.status == "running" else 0.0
            return {
                "status": self._state.status,
                "uptime_sec": round(uptime, 1),
                "pairs": self._state.pairs,
                "recipe": self._state.recipe,
                "last_error": self._state.last_error,
                "open_positions": _read_open_positions(),
                "plans_today": _count_plans_today(),
            }


# Process-wide singleton.
_host = LiveRunnerHost()


def get_host() -> LiveRunnerHost:
    return _host


# ── Read helpers — exposed for the status endpoint ─────────────────────


def _instance_roots() -> list[tuple[str, Any]]:
    """Active instance dirs under artifacts/live/ plus the legacy flat
    layout if present. Returned as ``[(instance_id, Path), ...]``.

    Honours ``artifacts/live/instances.json.instances[<id>].active`` when
    present; if missing (older state), treats every config-bearing
    subdir as active.

    ``instance_id`` is the subdir name, or ``"__legacy__"`` for the old
    flat layout so callers can still group by some owner.
    """
    active_filter: dict[str, bool] = {}
    index_file = _LIVE_DIR / "instances.json"
    if index_file.exists():
        try:
            idx = json.loads(index_file.read_text(encoding="utf-8"))
            for iid, meta in (idx.get("instances") or {}).items():
                active_filter[iid] = bool(meta.get("active", True))
        except (json.JSONDecodeError, TypeError):
            pass

    roots: list[tuple[str, Any]] = []
    for sub in _LIVE_DIR.glob("*/config.json"):
        if sub.parent.name in ("archive", "reconcile"):
            continue
        iid = sub.parent.name
        if not active_filter.get(iid, True):
            continue
        roots.append((iid, sub.parent))
    if (_LIVE_DIR / "state.json").exists() or (_LIVE_DIR / "plans").exists():
        roots.append(("__legacy__", _LIVE_DIR))
    return roots


def _read_open_positions() -> dict[str, Any]:
    """Return ``{instance_id: {pair: {plan_id: position_dict}}}``.

    Legacy flat layout surfaces under instance_id ``"__legacy__"`` to
    keep the shape uniform.
    """
    out: dict[str, Any] = {}
    for instance_id, root in _instance_roots():
        state_file = root / "state.json"
        if not state_file.exists():
            continue
        try:
            out[instance_id] = json.loads(state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            out[instance_id] = {}
    return out


def _count_plans_today() -> int:
    import datetime as _dt

    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    total = 0
    for _instance_id, root in _instance_roots():
        plans_file = root / "plans" / f"{today}.jsonl"
        if not plans_file.exists():
            continue
        total += sum(1 for _ in plans_file.open(encoding="utf-8"))
    return total


def tail_plans(since_ts: str | None = None, pair: str | None = None, limit: int = 100) -> list[dict]:
    """Return today's plans across every instance (newest last),
    optionally filtered. Each row is tagged with ``instance_id``.
    """
    import datetime as _dt

    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    rows: list[dict] = []
    for instance_id, root in _instance_roots():
        plans_file = root / "plans" / f"{today}.jsonl"
        if not plans_file.exists():
            continue
        for line in plans_file.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if pair and row.get("pair") != pair:
                continue
            if since_ts and row.get("fired_at_ts", "") <= since_ts:
                continue
            # Caller-side tag so plans can be grouped in the UI.
            row.setdefault("instance_id", instance_id)
            rows.append(row)
    # Sort newest-last by fired_at_ts so cross-instance tail is coherent.
    rows.sort(key=lambda r: r.get("fired_at_ts", ""))
    return rows[-limit:]
