"""Run-page API coverage for lean mega-sweep artifacts."""

from __future__ import annotations

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import routes
from ff import harness


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(routes, "ARTIFACTS_DIR", tmp_path)
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_scatter_reads_lean_metrics_sidecar(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    runs.mkdir()
    metrics = np.zeros((20, len(harness.METRIC_COLUMNS)), dtype=np.float32)
    metrics[:, harness.METRIC_INDEX["trades"]] = np.arange(20)
    metrics[:, harness.METRIC_INDEX["expectancy_pips"]] = 2.0
    np.save(runs / "lean_metrics.npy", metrics)
    np.savez_compressed(
        runs / "lean_run.npz",
        artifact_mode=np.array("lean"),
        lean_metrics_file=np.array("lean_metrics.npy"),
        retained_trial_indices=np.array([3, 17], dtype=np.int64),
        retained_objectives_json=np.array('{"quality":[17],"total_pips":[3]}'),
    )

    r = c.get("/api/runs/lean_run.npz/scatter")
    assert r.status_code == 200
    body = r.json()
    assert body["n_trials"] == 20
    total_idx = body["metric_columns"].index("total_pips")
    assert body["metrics"][3][total_idx] == 6.0
    assert body["retained_indices"] == [3, 17]
    assert body["retained_objectives"]["quality"] == [17]


def test_trial_uses_retained_pnl_when_lean_artifact_has_it(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    runs.mkdir()
    metrics = np.zeros((20, len(harness.METRIC_COLUMNS)), dtype=np.float32)
    metrics[:, harness.METRIC_INDEX["trades"]] = 3
    metrics[:, harness.METRIC_INDEX["expectancy_pips"]] = 4.0
    np.save(runs / "lean_metrics.npy", metrics)
    np.savez_compressed(
        runs / "lean_run.npz",
        artifact_mode=np.array("lean"),
        lean_metrics_file=np.array("lean_metrics.npy"),
        retained_trial_indices=np.array([7], dtype=np.int64),
        retained_n_trades=np.array([3], dtype=np.int32),
        retained_pnl=np.array([[1.0, -0.5, 2.5]], dtype=np.float32),
    )

    retained = c.get("/api/runs/lean_run.npz/trial/7")
    assert retained.status_code == 200
    retained_body = retained.json()
    assert retained_body["equity"] == [1.0, 0.5, 3.0]
    assert retained_body["detail_available"] is True
    assert retained_body["metrics"]["total_pips"] == 3.0

    unretained = c.get("/api/runs/lean_run.npz/trial/8")
    assert unretained.status_code == 200
    unretained_body = unretained.json()
    assert unretained_body["equity"] == []
    assert unretained_body["detail_available"] is False
    assert unretained_body["metrics"]["total_pips"] == 12.0
