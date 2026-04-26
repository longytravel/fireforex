"""Issue #12 — path-traversal validation on `/api/runs/...` endpoints.

The relevant endpoints are:
- ``GET /api/runs/{run_file}/scatter``
- ``GET /api/runs/{run_file}/trial/{trial_idx}``
- ``GET /api/runs/{run_id}/trades.csv``

All three must reject anything that isn't a plain ``[A-Za-z0-9_-]+`` stem
(plus an optional ``.npz`` suffix on the trades.csv route). Defence in
depth: even if the regex were bypassed, the resolved path must live
inside ``artifacts/runs``.
"""

from __future__ import annotations

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    runs_dir = tmp_path / "artifacts" / "runs"
    runs_dir.mkdir(parents=True)
    monkeypatch.setattr(routes, "ARTIFACTS_DIR", tmp_path / "artifacts")
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), runs_dir


def _make_run(runs_dir, name: str = "fixture.npz") -> None:
    """Minimal NPZ that satisfies all three endpoints."""
    n_trials = 1
    metrics = np.zeros((n_trials, 25), dtype=np.float32)
    pnl = np.zeros((n_trials, 4), dtype=np.float32)
    pnl[0] = [1.0, 1.0, 1.0, 1.0]
    n_trades = np.array([4], dtype=np.int32)
    trades_dtype = np.dtype([("entry_ts", "datetime64[s]"), ("exit_ts", "datetime64[s]"), ("pair", "U10"), ("pnl_pips", "f4")])
    trades = np.zeros(4, dtype=trades_dtype)
    np.savez(
        runs_dir / name,
        per_trial_metrics=metrics,
        per_trial_pnl=pnl,
        per_trial_n_trades=n_trades,
        trades=trades,
    )


@pytest.mark.parametrize(
    "bad_input",
    [
        "../etc/passwd",
        "..%2Fetc%2Fpasswd",
        "foo/bar.npz",
        "foo\\bar.npz",
        "foo bar.npz",  # whitespace
        ".hidden.npz",  # leading dot
        "run.npz.bak",  # double extension
        "/absolute/path.npz",
        "C:\\Windows\\System32.npz",
    ],
)
def test_scatter_rejects_path_traversal_attempts(client, bad_input):
    c, _ = client
    resp = c.get(f"/api/runs/{bad_input}/scatter")
    # FastAPI may return 404 (route not matched due to '/' in input) or 400
    # from our validator. Either is a refusal.
    assert resp.status_code in (400, 404, 422), (bad_input, resp.status_code, resp.text)


@pytest.mark.parametrize(
    "bad_input",
    [
        "../etc/passwd",
        "foo bar",
        ".hidden",
    ],
)
def test_trades_csv_rejects_path_traversal_attempts(client, bad_input):
    c, _ = client
    resp = c.get(f"/api/runs/{bad_input}/trades.csv")
    assert resp.status_code in (400, 404, 422), (bad_input, resp.status_code, resp.text)


def test_trades_csv_accepts_valid_stem(client):
    c, runs_dir = client
    _make_run(runs_dir, "valid_run.npz")
    resp = c.get("/api/runs/valid_run/trades.csv")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")


def test_trades_csv_accepts_explicit_npz_suffix(client):
    c, runs_dir = client
    _make_run(runs_dir, "valid_run.npz")
    resp = c.get("/api/runs/valid_run.npz/trades.csv")
    assert resp.status_code == 200, resp.text


def test_scatter_accepts_valid_run_file(client):
    c, runs_dir = client
    _make_run(runs_dir, "valid_run.npz")
    resp = c.get("/api/runs/valid_run.npz/scatter")
    assert resp.status_code == 200, resp.text


def test_resolve_run_npz_rejects_symlink_escape(tmp_path, monkeypatch):
    """If a symlink in artifacts/runs points outside, reject. (Posix-only;
    on Windows where mklink requires admin we just skip.)"""
    import os

    runs_dir = tmp_path / "artifacts" / "runs"
    runs_dir.mkdir(parents=True)
    outside = tmp_path / "outside.npz"
    outside.write_bytes(b"")
    link = runs_dir / "escape.npz"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform / no permission")
    monkeypatch.setattr(routes, "ARTIFACTS_DIR", tmp_path / "artifacts")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        routes._resolve_run_npz("escape.npz")
    assert exc.value.status_code == 400
