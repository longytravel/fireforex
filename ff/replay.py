"""Replay a deployed live config as a single-trial backtest, per pair.

One-shot orchestrator that answers *"what would this live config have
traded if the backtest engine saw the same Dukascopy window?"*. Called
by ``run.py replay`` and (optionally) the web UI.

Steps:
  1. Load ``service_config.json``.
  2. Derive the replay window from ``artifacts/live/plans/*.jsonl``
     timestamps (±1 day pad). Falls back to (today-30, today) if no
     plans yet.
  3. For each pair in ``config["pairs"]``:
       a. Top up Dukascopy M1 (+ derive M5/M15/M30/H1/H4/D).
       b. Build the EA via ``complexity_to_ea`` + ``apply_overrides``
          — the same code path the live runner uses.
       c. Call ``harness.run(..., frozen_trial=config['best_trial'],
          save_artifacts=False)``.
       d. Collect the returned trade log (already carries the ``pair``
          column from Piece A).
  4. Concatenate, write ``artifacts/replay/<source_run_id>/<stamp>/
     trades.npz`` + ``summary.json``, and update ``latest_stamp.txt``.

No sweep artifacts are touched. The replay tree is orthogonal to
``artifacts/runs``.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .defaults.complexity import complexity_to_ea
from .defaults.overrides import apply_overrides
from . import harness

LOG = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = REPO_ROOT / "artifacts"
LIVE_DIR = ARTIFACTS_DIR / "live"
REPLAY_DIR = ARTIFACTS_DIR / "replay"


def _resolve_window(plans_dir: Path) -> tuple[date, date]:
    """Derive replay window from the min/max signal_bar_ts in plans JSONL.

    Pads ±1 calendar day so the live signal bar itself sits comfortably
    inside the window (the engine aligns on closed bars). Falls back to
    (today-30, today) if no plans exist yet — useful for first-deploy
    sanity checks before a single bar has fired.
    """
    ts_values: list[datetime] = []
    if plans_dir.exists():
        for jsonl in sorted(plans_dir.glob("*.jsonl")):
            try:
                for line in jsonl.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    ts_raw = rec.get("signal_bar_ts")
                    if not ts_raw:
                        continue
                    ts_values.append(datetime.fromisoformat(ts_raw))
            except Exception as e:  # pragma: no cover — malformed jsonl
                LOG.warning("[replay] skipping malformed plans file %s: %s", jsonl, e)

    today = datetime.now(timezone.utc).date()
    if not ts_values:
        LOG.info("[replay] no plans yet — defaulting window to last 30 days")
        return today - timedelta(days=30), today
    lo = min(ts_values).date() - timedelta(days=1)
    hi = max(ts_values).date() + timedelta(days=1)
    # Never ask the downloader for a future date — Dukascopy won't have it.
    if hi > today:
        hi = today
    return lo, hi


def _extract_level(config: dict[str, Any]) -> int:
    """Recover the complexity level from the config, falling back to source_run_id.

    Deploy currently ships the recipe from the UI which may omit ``level``
    (because the UI stores it on the run record, not the recipe payload).
    ``source_run_id`` encodes it as ``complexity_L<N>_...``, so we parse
    it if the recipe doesn't carry one.
    """
    recipe = config.get("recipe") or {}
    lvl_raw = recipe.get("level")
    if lvl_raw is not None:
        return int(lvl_raw)
    src = str(config.get("source_run_id") or "")
    m = re.search(r"_L(\d+)_", src)
    if m:
        return int(m.group(1))
    LOG.warning("[replay] no level in recipe nor source_run_id — defaulting to 1")
    return 1


def _build_ea_for_pair(config: dict[str, Any], pair: str) -> dict[str, Any]:
    """Build the EA dict for one pair — same path as live runner + app/jobs."""
    recipe = config.get("recipe") or {}
    level = _extract_level(config)
    ea = complexity_to_ea(
        level=level,
        pair=pair,
        main_tf=recipe.get("main_tf", "M15"),
        sub_tf=recipe.get("sub_tf", "M1"),
        name=recipe.get("name"),
    )
    overrides = config.get("overrides") or {}
    if overrides:
        ea = apply_overrides(ea, overrides)
    return ea


def _ensure_data(pair: str, start: date, end: date,
                 data_source: str = "dukascopy") -> None:
    """Top up M1 data + fan out higher TFs for the chosen source. Idempotent.

    ``data_source="dukascopy"`` uses the bi5 downloader against
    ``DATA_ROOT``; ``data_source="mt5"`` uses the MT5 downloader against
    ``MT5_DATA_ROOT``. Each source writes to its own parquet root so the
    three-way reconcile can load them side-by-side.
    """
    from .data import m1_bi5_downloader, mt5_m1_downloader, resample

    def _log(msg: str) -> None:
        LOG.info("[replay][data][%s][%s] %s", data_source, pair, msg)

    if data_source == "dukascopy":
        result = m1_bi5_downloader.download(
            pair, start, end, append=True, log_cb=_log,
        )
        root = harness.DATA_ROOT
    elif data_source == "mt5":
        result = mt5_m1_downloader.download(
            pair, start, end, append=True, log_cb=_log,
        )
        root = mt5_m1_downloader.MT5_DATA_ROOT
    else:
        raise ValueError(
            f"unknown data_source={data_source!r} (expected 'dukascopy' or 'mt5')"
        )

    new_bars = int(result.get("new_bars", 0))
    if new_bars > 0:
        try:
            written = resample.derive_higher_tfs(pair, source_tf="M1", root=root)
            _log(f"derived higher TFs: {[p.name for p in written]}")
        except FileNotFoundError:
            _log("no M1 parquet to resample — skipping higher-TF fanout")


def replay_service_config(
    config_path: Path | str = LIVE_DIR / "service_config.json",
    *,
    data_source: str = "dukascopy",
) -> dict[str, Any]:
    """Replay the deployed config pair-by-pair. Returns the summary dict.

    ``data_source`` picks which parquet root the backtest reads —
    ``"dukascopy"`` (default) or ``"mt5"``. Output is stamped per-source
    (``<source_run_id>/<stamp>_<data_source>/``) so successive runs don't
    overwrite each other.
    """
    if data_source not in ("dukascopy", "mt5"):
        raise ValueError(
            f"unknown data_source={data_source!r} (expected 'dukascopy' or 'mt5')"
        )
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"service_config not found: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))

    pairs: list[str] = list(config.get("pairs") or [])
    if not pairs:
        pairs = [config.get("recipe", {}).get("pair", "EUR_USD")]
    best_trial = config.get("best_trial")
    if not best_trial:
        raise ValueError("service_config has no best_trial — cannot replay")
    source_run_id = str(config.get("source_run_id") or "unknown_run")

    # Plans live next to the config — works for both the legacy flat
    # layout (artifacts/live/plans) and the multi-instance layout
    # (artifacts/live/<id>/plans).
    plans_dir = config_path.parent / "plans"
    if not plans_dir.exists():
        # Legacy fallback — old flat layout kept plans at the top level.
        plans_dir = LIVE_DIR / "plans"
    window_start, window_end = _resolve_window(plans_dir)
    LOG.info("[replay] window %s → %s  (%d pairs)",
             window_start, window_end, len(pairs))

    # Stamp the replay output tree. Suffix the data source so MT5 and
    # Dukascopy runs for the same window don't clobber each other when the
    # three-way reconcile fires both in succession.
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = REPLAY_DIR / source_run_id / f"{stamp}_{data_source}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Pin the per-pair backtest start/end inside the EA so the harness
    # slices the big parquet down to the live window. Keeps replay runs
    # fast and parity-focused.
    start_str = window_start.isoformat()
    end_str = window_end.isoformat()

    per_pair_rows: list[np.ndarray] = []
    per_pair_summary: list[dict[str, Any]] = []
    exec_scalars: dict[str, float] = {}

    t_total = time.perf_counter()
    for i, pair in enumerate(pairs, 1):
        LOG.info("[replay] (%d/%d) %s", i, len(pairs), pair)
        try:
            _ensure_data(pair, window_start, window_end, data_source=data_source)
        except Exception as e:
            LOG.error("[replay] data download failed for %s: %s", pair, e)
            per_pair_summary.append({
                "pair": pair, "status": "data_error", "error": str(e),
                "n_trades": 0, "total_pips": 0.0,
            })
            continue

        try:
            ea = _build_ea_for_pair(config, pair)
            ea.setdefault("data", {})
            ea["data"]["start_date"] = start_str
            ea["data"]["end_date"] = end_str

            result = harness.run(
                ea,
                layer_name=f"replay_{pair}",
                seed=int(config.get("recipe", {}).get("seed", 42)),
                n_trials=1,
                open_browser=False,
                frozen_trial=best_trial,
                save_artifacts=False,
                data_source=data_source,
            )
        except Exception as e:
            LOG.error("[replay] harness.run failed for %s: %s", pair, e)
            per_pair_summary.append({
                "pair": pair, "status": "run_error", "error": str(e),
                "n_trades": 0, "total_pips": 0.0,
            })
            continue

        log = result.get("trade_log")
        n_trades = 0 if log is None else int(len(log))
        total_pips = 0.0 if not n_trades else float(log["pnl_pips"].sum())
        if log is not None and n_trades > 0:
            per_pair_rows.append(log)
        per_pair_summary.append({
            "pair": pair, "status": "ok",
            "n_trades": n_trades, "total_pips": total_pips,
            "win_rate_pct": result.get("win_rate_pct"),
            "quality_best": result.get("quality_best"),
        })
        # Capture per-run execution scalars once — identical across pairs
        # since they come from the same exe_cfg defaults.
        if not exec_scalars:
            for key in ("commission_pips", "slippage_pips",
                        "max_spread_pips", "pip_value"):
                if key in result:
                    exec_scalars[key] = float(result[key])

    elapsed = time.perf_counter() - t_total

    # Concatenate all per-pair trade logs and persist.
    trades_all: np.ndarray
    if per_pair_rows:
        trades_all = np.concatenate(per_pair_rows)
    else:
        # Build an empty structured array matching the harness dtype so the
        # downstream reconciler can still load it.
        trades_all = harness._build_best_trade_log(
            np.zeros(0, dtype=np.float64), 0,
            # Empty DatetimeIndex — _build_best_trade_log handles n_trades=0.
            _empty_dt_index(), _empty_dt_index(),
        )

    npz_path = out_dir / "trades.npz"
    np.savez_compressed(
        npz_path,
        trades=trades_all,
        **{k: np.float64(v) for k, v in exec_scalars.items()},
    )

    summary = {
        "source_run_id": source_run_id,
        "stamp": stamp,
        "data_source": data_source,
        "window": {"start": start_str, "end": end_str},
        "pairs": per_pair_summary,
        "n_trades_total": int(len(trades_all)),
        "total_pips_all": float(trades_all["pnl_pips"].sum()) if len(trades_all) else 0.0,
        "elapsed_sec": round(elapsed, 2),
        "config_path": str(config_path),
        "exec_scalars": exec_scalars,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    # Latest pointer — plain text on Windows, cross-platform safe. One
    # pointer per data source so reconcile can find each independently.
    (REPLAY_DIR / source_run_id / f"latest_stamp_{data_source}.txt").write_text(
        f"{stamp}_{data_source}", encoding="utf-8"
    )
    # Legacy pointer (dukascopy only) — kept so older reconcile callers
    # still resolve without a migration step.
    if data_source == "dukascopy":
        (REPLAY_DIR / source_run_id / "latest_stamp.txt").write_text(
            f"{stamp}_{data_source}", encoding="utf-8"
        )

    LOG.info("[replay] done in %.2fs — %d trades across %d pairs → %s",
             elapsed, summary["n_trades_total"], len(pairs), npz_path)
    return summary


def _empty_dt_index():
    """Avoid importing pandas at module level for the happy path."""
    import pandas as pd
    return pd.DatetimeIndex([], tz="UTC")


__all__ = ["replay_service_config", "_resolve_window"]
