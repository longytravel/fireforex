"""Unit tests for the replay orchestrator.

Everything heavy (data download, harness.run) is monkeypatched so the
test is hermetic — no parquet required, no engine invocation.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pytest


def _make_plan(pair: str, ts: str) -> dict:
    return {
        "plan_id": f"{pair}_{ts}_+1",
        "pair": pair,
        "signal_bar_ts": ts,
        "direction": 1,
    }


def _fake_trade_log(pair: str, n: int = 3) -> np.ndarray:
    """Build a minimal structured array matching the harness dtype."""
    from ff.harness import TRADE_FIELD_NAMES
    dtype = np.dtype(
        [(name, np.float64) for name in TRADE_FIELD_NAMES]
        + [
            ("entry_ts", "datetime64[ns]"),
            ("exit_ts", "datetime64[ns]"),
            ("pair", "U10"),
            ("signal_variant_id", np.int32),
            ("signal_family", "U20"),
            ("spread_entry_pips", np.float32),
            ("exit_reason_name", "U16"),
        ]
    )
    arr = np.zeros(n, dtype=dtype)
    arr["pnl_pips"] = np.arange(n, dtype=np.float64) + 1.0
    arr["direction"] = 1
    arr["pair"] = pair
    arr["signal_variant_id"] = 42
    arr["signal_family"] = "ema_cross"
    arr["spread_entry_pips"] = 0.5
    arr["exit_reason_name"] = "TP"
    return arr


def test_resolve_window_derives_from_plans(tmp_path: Path):
    from ff import replay

    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    # Two plans on different days.
    (plans_dir / "2026-04-19.jsonl").write_text(
        json.dumps(_make_plan("EUR_USD", "2026-04-19T14:00:00+00:00")) + "\n",
        encoding="utf-8",
    )
    (plans_dir / "2026-04-21.jsonl").write_text(
        json.dumps(_make_plan("USD_CHF", "2026-04-21T09:00:00+00:00")) + "\n",
        encoding="utf-8",
    )
    start, end = replay._resolve_window(plans_dir)
    assert start == date(2026, 4, 18)
    # hi gets clipped to today if today < 2026-04-22. Test date arithmetic,
    # not the clamp — both are valid.
    assert end >= date(2026, 4, 21) or end == date.today()


def test_resolve_window_empty_dir(tmp_path: Path):
    from ff import replay

    plans_dir = tmp_path / "no_plans"
    start, end = replay._resolve_window(plans_dir)
    # Falls back to (today-30, today).
    assert (end - start).days == 30


def test_replay_service_config_end_to_end(tmp_path: Path, monkeypatch):
    """Mock data download + harness.run; verify per-pair loop + NPZ write."""
    from ff import replay

    config = {
        "source_run_id": "complexity_L10_EUR_USD_M15_20260421_095645",
        "recipe": {
            "pair": "EUR_USD", "main_tf": "M15", "sub_tf": "M1",
            "level": 10, "seed": 42,
        },
        "overrides": {},
        "pairs": ["EUR_USD", "USD_CHF", "USD_JPY"],
        "best_trial": {"signal_variant": 2423, "engine": {}},
    }
    config_path = tmp_path / "svc.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    # Point the replay output dir at tmp so we don't pollute the repo tree.
    monkeypatch.setattr(replay, "REPLAY_DIR", tmp_path / "replay_out")
    # Empty plans dir → 30-day fallback window.
    monkeypatch.setattr(replay, "LIVE_DIR", tmp_path / "live_fake")

    # Mock data ingest — just record calls, no network.
    download_calls = []

    def _fake_ensure_data(pair, start, end):
        download_calls.append((pair, start, end))

    monkeypatch.setattr(replay, "_ensure_data", _fake_ensure_data)

    # Mock EA build — don't need complexity_to_ea to actually work.
    monkeypatch.setattr(
        replay, "_build_ea_for_pair",
        lambda cfg, pair: {"data": {"pair": pair}, "name": f"ea_{pair}"},
    )

    # Mock harness.run — return a fake trade log per pair.
    run_calls = []

    def _fake_harness_run(ea, **kw):
        run_calls.append({"pair": ea["data"]["pair"], **kw})
        return {
            "trade_log": _fake_trade_log(ea["data"]["pair"], n=2),
            "win_rate_pct": 66.6,
            "quality_best": 1.5,
            "commission_pips": 0.3,
            "slippage_pips": 0.0,
            "max_spread_pips": 10.0,
            "pip_value": 0.0001,
        }

    monkeypatch.setattr(replay.harness, "run", _fake_harness_run)

    summary = replay.replay_service_config(config_path)

    # 3 pairs × data download + harness run.
    assert len(download_calls) == 3
    assert [c[0] for c in download_calls] == ["EUR_USD", "USD_CHF", "USD_JPY"]
    assert len(run_calls) == 3
    # Every run was given the frozen trial + save_artifacts=False.
    for call in run_calls:
        assert call["frozen_trial"] == config["best_trial"]
        assert call["save_artifacts"] is False
        assert call["n_trials"] == 1

    # NPZ has every pair + execution scalars.
    out_dir = tmp_path / "replay_out" / config["source_run_id"] / summary["stamp"]
    npz = np.load(out_dir / "trades.npz", allow_pickle=False)
    trades = npz["trades"]
    assert len(trades) == 6  # 3 pairs × 2 trades each
    assert set(trades["pair"]) == {"EUR_USD", "USD_CHF", "USD_JPY"}
    for key in ("commission_pips", "slippage_pips",
                "max_spread_pips", "pip_value"):
        assert key in npz.files, f"missing scalar {key}"

    # Summary + latest pointer written.
    assert (out_dir / "summary.json").exists()
    latest_ptr = tmp_path / "replay_out" / config["source_run_id"] / "latest_stamp.txt"
    assert latest_ptr.read_text(encoding="utf-8").strip() == summary["stamp"]
    assert summary["n_trades_total"] == 6
    assert summary["total_pips_all"] == pytest.approx(9.0)  # (1+2) * 3 pairs
