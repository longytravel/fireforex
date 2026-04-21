"""Unit tests for the live state sync module.

Mocks all git subprocess calls so the test is hermetic — no git repo,
no network, no credentials needed.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def _fake_git_factory(call_log: list) -> callable:
    def _fake_git(cwd, *args, check=True, timeout=60):
        call_log.append((str(cwd), args))
        # Simulate `git status --porcelain` returning a diff so snapshot_and_push
        # proceeds to commit+push rather than short-circuiting.
        stdout = ""
        if args and args[0] == "status":
            stdout = " M plans/2026-04-21.jsonl\n"
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)
    return _fake_git


def test_snapshot_and_push_copies_files_and_invokes_git(tmp_path: Path, monkeypatch):
    """Given a populated LIVE_DIR, snapshot_and_push should:
       1. Copy each matching file into the worktree (relative path preserved).
       2. Invoke git add / status / commit / push in order.
    """
    from ff.live import state_sync

    # Lay out a fake LIVE_DIR.
    live = tmp_path / "live"
    (live / "plans").mkdir(parents=True)
    (live / "plans" / "2026-04-21.jsonl").write_text(
        '{"plan_id": "x", "pair": "EUR_USD"}\n', encoding="utf-8",
    )
    (live / "tickets.jsonl").write_text('{"ticket": 1}\n', encoding="utf-8")
    (live / "state.json").write_text('{"EUR_USD": {}}', encoding="utf-8")

    worktree = tmp_path / "worktree"
    worktree.mkdir()

    monkeypatch.setattr(state_sync, "LIVE_DIR", live)
    monkeypatch.setattr(state_sync, "WORKTREE_DIR", worktree)
    # Pretend worktree is already attached — skip _ensure_worktree side effects.
    monkeypatch.setattr(state_sync, "_ensure_worktree", lambda: None)

    calls: list = []
    monkeypatch.setattr(state_sync, "_git", _fake_git_factory(calls))

    pushed = state_sync.snapshot_and_push()
    assert pushed is True

    # Files materialised into worktree.
    assert (worktree / "plans" / "2026-04-21.jsonl").exists()
    assert (worktree / "tickets.jsonl").exists()
    assert (worktree / "state.json").exists()

    # Git commands called in order: add, status, commit, push.
    actions = [c[1][0] for c in calls]
    assert actions == ["add", "status", "commit", "push"], actions


def test_snapshot_no_live_dir_is_noop(tmp_path: Path, monkeypatch):
    from ff.live import state_sync

    monkeypatch.setattr(state_sync, "LIVE_DIR", tmp_path / "nonexistent")
    # Fail loudly if any git call is attempted.
    monkeypatch.setattr(state_sync, "_git", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("git should not be called when LIVE_DIR is missing")
    ))

    assert state_sync.snapshot_and_push() is False


def test_snapshot_skips_when_no_diff(tmp_path: Path, monkeypatch):
    """git status returns empty — snapshot_and_push should NOT commit or push."""
    from ff.live import state_sync

    live = tmp_path / "live"
    (live / "plans").mkdir(parents=True)
    (live / "plans" / "2026-04-21.jsonl").write_text("{}\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    monkeypatch.setattr(state_sync, "LIVE_DIR", live)
    monkeypatch.setattr(state_sync, "WORKTREE_DIR", worktree)
    monkeypatch.setattr(state_sync, "_ensure_worktree", lambda: None)

    calls: list = []

    def _fake_git(cwd, *args, check=True, timeout=60):
        calls.append(args)
        # Return empty status → signal "no diff".
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(state_sync, "_git", _fake_git)
    assert state_sync.snapshot_and_push() is False

    actions = [c[0] for c in calls]
    assert "commit" not in actions, f"should not commit on clean status: {actions}"
    assert "push" not in actions, f"should not push on clean status: {actions}"
