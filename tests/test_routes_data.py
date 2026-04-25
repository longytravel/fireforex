"""HTTP surface for the Data tab: tick download, derive, tick-to-m1."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import jobs, routes
from ff import harness
from ff.data import inventory


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(inventory, "ROOTS", (tmp_path,))
    monkeypatch.setattr(inventory, "_CACHE_PATH", tmp_path / "inv.json")
    harness._PARQUET_CACHE.clear()
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app), tmp_path


def _m1(tmp_path: Path, pair: str = "EUR_USD", n: int = 60) -> None:
    ts = pd.date_range(datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc), periods=n, freq="1min")
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "open": 1.0,
            "high": 1.0001,
            "low": 0.9999,
            "close": 1.0,
            "volume": 1.0,
            "spread": 0.0002,
        }
    )
    df.to_parquet(tmp_path / f"{pair}_M1.parquet", index=False)


def _tick(tmp_path: Path, pair: str = "EUR_USD") -> None:
    start = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    rows = [
        (0, 1.2000, 1.2002),
        (30_000, 1.2001, 1.2003),
        (60_000, 1.2002, 1.2004),  # next minute
        (90_000, 1.2003, 1.2005),
    ]
    df = pd.DataFrame(
        {
            "timestamp": [start + pd.Timedelta(ms, unit="ms") for ms, _, _ in rows],
            "bid": [b for _, b, _ in rows],
            "ask": [a for _, _, a in rows],
            "bid_volume": [0.5] * len(rows),
            "ask_volume": [0.5] * len(rows),
        }
    )
    df.to_parquet(tmp_path / f"{pair}_TICK.parquet", index=False)


def test_derive_route_writes_higher_tfs(client):
    c, tmp = client
    _m1(tmp)
    r = c.post("/api/data/derive/EUR_USD")
    assert r.status_code == 200
    body = r.json()
    assert "H1" in body["derived"]
    assert (tmp / "EUR_USD_H1.parquet").exists()


def test_derive_route_rejects_bad_pair(client):
    c, _ = client
    r = c.post("/api/data/derive/not-a-pair")
    assert r.status_code == 400


def test_derive_route_404_when_no_source(client):
    c, _ = client
    r = c.post("/api/data/derive/EUR_USD")
    assert r.status_code == 404


def test_tick_to_m1_route_writes_m1(client):
    c, tmp = client
    _tick(tmp)
    r = c.post("/api/data/tick-to-m1/EUR_USD")
    assert r.status_code == 200
    body = r.json()
    assert body["pair"] == "EUR_USD"
    assert (tmp / "EUR_USD_M1.parquet").exists()


def test_tick_download_route_validates_and_queues(client, monkeypatch):
    c, tmp = client
    _tick(tmp)

    # Stub the network layer — the route should accept the request and hand
    # off to start_tick_download(). We verify the return shape, not the
    # Dukascopy HTTP call.
    from ff.data import tick_downloader as _tdl

    def _fake_download(pair, start, end, *, append, log_cb, cancel_cb):
        log_cb and log_cb("fake tick fetch ok")
        return {
            "path": str(tmp / f"{pair}_TICK.parquet"),
            "appended": False,
            "new_rows": 0,
            "total_rows": 0,
            "start_ts": None,
            "end_ts": None,
        }

    monkeypatch.setattr(_tdl, "download", _fake_download)

    r = c.post(
        "/api/data/download/tick",
        json={
            "pair": "EUR_USD",
            "start": "2024-01-02",
            "end": "2024-01-02",
            "append": False,
        },
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    assert job_id

    # Drain the job thread — sync-poll until it leaves running.
    import time

    for _ in range(100):
        j = jobs.get_tick_download(job_id)
        if j is None:
            break
        if j.status != "running":
            break
        time.sleep(0.05)
    j = jobs.get_tick_download(job_id)
    assert j is not None
    assert j.status in {"done", "error", "cancelled"}


def test_tick_download_route_rejects_bad_pair(client):
    c, _ = client
    r = c.post(
        "/api/data/download/tick",
        json={
            "pair": "bad",
            "start": "2024-01-02",
            "end": "2024-01-02",
        },
    )
    assert r.status_code == 400


def test_tick_download_route_rejects_reversed_dates(client):
    c, _ = client
    r = c.post(
        "/api/data/download/tick",
        json={
            "pair": "EUR_USD",
            "start": "2024-01-05",
            "end": "2024-01-02",
        },
    )
    assert r.status_code == 400


def test_health_route_handles_truncated_parquet(client):
    """Truncated / partially-synced parquet on Google Drive returns a
    structured 200 with summary='error' rather than a 500 traceback."""
    c, tmp = client
    # Parquet magic at head, no footer — pyarrow raises ArrowInvalid.
    (tmp / "EUR_USD_M5.parquet").write_bytes(b"PAR1" + b"\x00" * 128)
    r = c.get("/api/data/health/EUR_USD/M5")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == "error"
    assert body["bars"] == 0
    assert "error_detail" in body
    assert "truncated" in body["error_detail"].lower() or "magic bytes" in body["error_detail"]
