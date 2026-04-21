"""Push live runner state from VPS → remote git branch so the laptop can pull it.

Problem: the VPS runner writes ``artifacts/live/{plans/*.jsonl, tickets.jsonl,
state.json}``. Those files are gitignored on ``main`` and never leave the VPS,
so the laptop web UI has no idea what fired live. The parity workbench
(replay CLI + reconciler + per-pair cards) needs these files on the laptop.

Solution: maintain a dedicated orphan branch ``live-state`` in a separate
git worktree. Every N seconds the runner copies the latest snapshot into
the worktree, commits, and force-pushes. The laptop's Restart shortcut
does ``git fetch origin live-state`` + ``git archive`` to materialise the
snapshot into ``artifacts/live/`` before launching the web UI.

Why a worktree branch rather than committing to ``main``?

- Keeps ``main`` clean — no minute-by-minute state commits spamming history.
- Force-push on ``live-state`` is safe (nobody else writes to it).
- Works with the existing GitHub credential manager on the VPS (same as
  the Deploy push flow).

All failures are logged and swallowed — state sync is best-effort
telemetry, never blocks the trading loop.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

LOG = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIVE_DIR = REPO_ROOT / "artifacts" / "live"
# Sibling directory — git worktree cannot nest inside the main working tree.
WORKTREE_DIR = REPO_ROOT.parent / f"{REPO_ROOT.name}-live-state"
SYNC_BRANCH = "live-state"
REMOTE = "origin"

# Which live files to mirror. Glob patterns relative to LIVE_DIR.
SYNC_GLOBS = [
    "plans/*.jsonl",
    "tickets.jsonl",
    "state.json",
    "errors.jsonl",
    "crashes.jsonl",
]


def _git(cwd: Path, *args: str, check: bool = True,
         timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check,
        timeout=timeout, capture_output=True, text=True,
    )


def _ensure_worktree() -> None:
    """Create the ``live-state`` worktree if missing. Idempotent."""
    if (WORKTREE_DIR / ".git").exists() or WORKTREE_DIR.exists():
        return

    # Try to attach the worktree to an existing remote branch first.
    try:
        _git(REPO_ROOT, "fetch", REMOTE, SYNC_BRANCH)
        _git(REPO_ROOT, "worktree", "add", str(WORKTREE_DIR), SYNC_BRANCH)
        LOG.info("[state_sync] worktree attached to origin/%s", SYNC_BRANCH)
        return
    except subprocess.CalledProcessError:
        pass  # Branch does not exist remotely — create orphan below.

    # Create orphan branch with empty content.
    WORKTREE_DIR.mkdir(parents=True, exist_ok=True)
    _git(REPO_ROOT, "worktree", "add", "--detach", str(WORKTREE_DIR), "HEAD")
    _git(WORKTREE_DIR, "checkout", "--orphan", SYNC_BRANCH)
    # Blow away the main-branch files inherited by the detach.
    for item in WORKTREE_DIR.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    _git(WORKTREE_DIR, "commit", "--allow-empty", "-m", "state: initial orphan commit")
    LOG.info("[state_sync] created orphan %s branch in worktree", SYNC_BRANCH)


def snapshot_and_push() -> bool:
    """One-shot: copy LIVE_DIR snapshot into worktree, commit, push.

    Returns True on a push, False on a no-op (no diff) or soft failure.
    """
    if not LIVE_DIR.exists():
        return False
    _ensure_worktree()

    # Clear the worktree (except .git) so deletions on the source side
    # propagate. The copy loop below re-creates what's current.
    for item in WORKTREE_DIR.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    copied = 0
    for pattern in SYNC_GLOBS:
        for src in LIVE_DIR.glob(pattern):
            if not src.is_file():
                continue
            rel = src.relative_to(LIVE_DIR)
            dst = WORKTREE_DIR / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1

    if copied == 0:
        return False

    _git(WORKTREE_DIR, "add", "-A")
    status = _git(WORKTREE_DIR, "status", "--porcelain", check=False)
    if not status.stdout.strip():
        return False  # no diff, skip commit

    stamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
    _git(WORKTREE_DIR, "commit", "-m", f"state sync {stamp}")

    try:
        _git(WORKTREE_DIR, "push", "-f", REMOTE, SYNC_BRANCH)
    except subprocess.CalledProcessError as e:
        LOG.warning("[state_sync] push failed: %s", e.stderr.strip() if e.stderr else e)
        return False
    return True


def start_sync_thread(interval_sec: int = 60) -> threading.Event:
    """Start a daemon thread that runs :func:`snapshot_and_push` on a timer.

    Returns the stop event so callers can request graceful shutdown.
    Swallows every exception — sync is a best-effort side channel and
    must never block the live runner's main loop.
    """
    stop_event = threading.Event()

    def _loop() -> None:
        LOG.info("[state_sync] thread running (every %ds)", interval_sec)
        while not stop_event.wait(interval_sec):
            try:
                pushed = snapshot_and_push()
                if pushed:
                    LOG.info("[state_sync] pushed snapshot to %s", SYNC_BRANCH)
            except Exception:
                LOG.exception("[state_sync] iteration failed — continuing")

    t = threading.Thread(target=_loop, name="ff-live-state-sync", daemon=True)
    t.start()
    return stop_event
