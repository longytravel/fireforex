"""Laptop-side pull loop for the VPS's ``live-state`` branch.

Mirror image of ``ff.live.state_sync`` (which runs on the VPS and pushes
plans/tickets/state every 60s). This module runs inside the laptop's
FastAPI process and fetches the branch + extracts it into
``artifacts/live/`` on a timer, so the Live tab stays fresh while the
user is away from the desk.

Design notes:

- Uses ``git fetch`` + ``git archive | tar`` — no worktree, no checkout.
  Safe to run against the main working tree.
- Silently skips if the branch doesn't exist yet (first ever VPS run).
- Swallows every failure — this is telemetry, must never take the UI down.
- Disabled by env var ``FF_DISABLE_LIVE_STATE_PULL=1`` for local dev.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

LOG = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_DIR = REPO_ROOT / "artifacts" / "live"
REMOTE = "origin"
BRANCH = "live-state"


def _git(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT),
        timeout=timeout, capture_output=True, text=True,
    )


def pull_once() -> bool:
    """One-shot: fetch the branch and extract its contents into LIVE_DIR.

    Returns True on successful extract, False on soft failure (branch
    missing, fetch failed, etc.).
    """
    fetch = _git("fetch", REMOTE, BRANCH)
    if fetch.returncode != 0:
        # Branch not on remote yet — that's fine early in the cycle.
        return False

    verify = _git("rev-parse", "--verify", f"{REMOTE}/{BRANCH}")
    if verify.returncode != 0:
        return False

    LIVE_DIR.mkdir(parents=True, exist_ok=True)

    # Pipe `git archive --format=tar <ref>` into `tar -xf - -C <dir>`.
    # Same technique as the laptop Restart bat — works on Windows 10+ (built-in tar).
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", f"{REMOTE}/{BRANCH}"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    tar = subprocess.Popen(
        ["tar", "-xf", "-", "-C", str(LIVE_DIR)],
        stdin=archive.stdout, stderr=subprocess.PIPE,
    )
    if archive.stdout is not None:
        archive.stdout.close()
    try:
        tar_err = tar.communicate(timeout=30)[1]
        archive_err = archive.communicate(timeout=5)[1]
    except subprocess.TimeoutExpired:
        archive.kill()
        tar.kill()
        return False

    if tar.returncode != 0 or archive.returncode != 0:
        LOG.warning("[live_pull] tar failed rc=%s archive_rc=%s tar_err=%s",
                    tar.returncode, archive.returncode,
                    tar_err.decode(errors="ignore").strip() if tar_err else "")
        return False
    return True


def start_pull_thread(interval_sec: int = 60) -> threading.Event:
    """Daemon thread that calls :func:`pull_once` on a timer.

    Returns the stop event. Callers should keep a reference if they want
    graceful shutdown; otherwise the daemon dies with the process.
    """
    stop_event = threading.Event()
    if os.environ.get("FF_DISABLE_LIVE_STATE_PULL") == "1":
        LOG.info("[live_pull] disabled via FF_DISABLE_LIVE_STATE_PULL=1")
        return stop_event

    def _loop() -> None:
        LOG.info("[live_pull] thread running (every %ds)", interval_sec)
        # Immediate first pull at startup so the UI doesn't wait a full
        # interval before the first refresh.
        try:
            pull_once()
        except Exception:
            LOG.exception("[live_pull] initial pull failed")
        while not stop_event.wait(interval_sec):
            try:
                pull_once()
            except Exception:
                LOG.exception("[live_pull] iteration failed — continuing")

    t = threading.Thread(target=_loop, name="ff-live-state-pull", daemon=True)
    t.start()
    return t  # Thread returned so tests can monkey-check it exists.
