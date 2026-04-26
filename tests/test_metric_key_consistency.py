"""Issue #14 — `win_rate` vs `win_rate_pct` key mismatch.

Engine column 1 is a fraction (0..1). Before this fix the harness
registered the column as `"win_rate"`, history.csv stored it as
`"win_rate_pct"`, and the JS UI tried to defend against either by
checking `m.win_rate <= 1`. The standardised key is now
`"win_rate_pct"` and the API boundary multiplies the fraction by 100
so the UI never has to guess at the unit.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import routes
from ff import harness


@pytest.fixture
def client(tmp_path, monkeypatch):
    runs_dir = tmp_path / "artifacts" / "runs"
    runs_dir.mkdir(parents=True)
    monkeypatch.setattr(routes, "ARTIFACTS_DIR", tmp_path / "artifacts")
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), runs_dir


def _make_fake_run(runs_dir: Path, name: str = "fake.npz") -> str:
    """Write a minimal NPZ matching the scatter/trial endpoints' shape."""
    n_trials = 3
    n_metrics = len(harness.METRIC_COLUMNS)
    metrics = np.zeros((n_trials, n_metrics), dtype=np.float32)
    # win_rate column (index 1) — three trials with 50%, 70%, 12.5% as fractions.
    win_idx = harness.METRIC_INDEX["win_rate_pct"]
    metrics[0, win_idx] = 0.50
    metrics[1, win_idx] = 0.70
    metrics[2, win_idx] = 0.125
    # Other useful columns for trial endpoint.
    metrics[0, 0] = 100  # trades
    metrics[1, 0] = 50
    metrics[2, 0] = 80

    pnl = np.zeros((n_trials, 100), dtype=np.float32)
    n_trades = np.array([100, 50, 80], dtype=np.int32)
    pnl[0, :100] = 1.0  # cumulative final = 100
    pnl[1, :50] = 2.0  # cumulative final = 100
    pnl[2, :80] = -0.5  # cumulative final = -40

    np.savez(
        runs_dir / name,
        per_trial_metrics=metrics,
        per_trial_pnl=pnl,
        per_trial_n_trades=n_trades,
    )
    return name


def test_metric_columns_uses_win_rate_pct():
    """METRIC_COLUMNS canonical key is `win_rate_pct`, not `win_rate`."""
    keys = [k for k, _, _ in harness.METRIC_COLUMNS]
    assert "win_rate_pct" in keys
    assert "win_rate" not in keys


def test_scatter_returns_win_rate_as_percentage(client):
    c, runs_dir = client
    name = _make_fake_run(runs_dir)
    resp = c.get(f"/api/runs/{name}/scatter")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    win_idx = body["metric_columns"].index("win_rate_pct")
    rows = body["metrics"]
    # Stored fractions 0.50, 0.70, 0.125 should surface as 50.0, 70.0, 12.5.
    assert rows[0][win_idx] == pytest.approx(50.0, abs=1e-3)
    assert rows[1][win_idx] == pytest.approx(70.0, abs=1e-3)
    assert rows[2][win_idx] == pytest.approx(12.5, abs=1e-3)


def test_trial_endpoint_returns_win_rate_as_percentage(client):
    c, runs_dir = client
    name = _make_fake_run(runs_dir)
    resp = c.get(f"/api/runs/{name}/trial/1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    metrics = body["metrics"]
    assert "win_rate_pct" in metrics
    assert "win_rate" not in metrics
    assert metrics["win_rate_pct"] == pytest.approx(70.0, abs=1e-3)


def test_trial_endpoint_does_not_double_convert_already_percentage(client, tmp_path, monkeypatch):
    """Defensive — a legacy NPZ that already stores percentage (>1) must not
    be multiplied by 100 a second time."""
    runs_dir = tmp_path / "artifacts" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(routes, "ARTIFACTS_DIR", tmp_path / "artifacts")
    app = FastAPI()
    app.include_router(routes.router)
    legacy_client = TestClient(app)

    n_trials = 1
    n_metrics = len(harness.METRIC_COLUMNS)
    metrics = np.zeros((n_trials, n_metrics), dtype=np.float32)
    # Legacy NPZ — value already stored as percentage (66.6 not 0.666).
    win_idx = harness.METRIC_INDEX["win_rate_pct"]
    metrics[0, win_idx] = 66.6
    pnl = np.zeros((n_trials, 10), dtype=np.float32)
    pnl[0, :5] = 1.0
    n_trades = np.array([5], dtype=np.int32)
    np.savez(
        runs_dir / "legacy.npz",
        per_trial_metrics=metrics,
        per_trial_pnl=pnl,
        per_trial_n_trades=n_trades,
    )
    resp = legacy_client.get("/api/runs/legacy.npz/trial/0")
    assert resp.status_code == 200, resp.text
    assert resp.json()["metrics"]["win_rate_pct"] == pytest.approx(66.6, abs=1e-3)
