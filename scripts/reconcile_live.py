"""End-to-end live vs backtest reconcile stitcher.

Run from the laptop after a live trade has closed:

    .\\.venv\\Scripts\\python.exe scripts\\reconcile_live.py

What it does:
    1. Call ``ff.replay.replay_service_config()`` — re-runs the deployed
       ``best_trial`` as a single-trial backtest over the live window
       (derived from ``artifacts/live/plans/*.jsonl``), writing
       ``artifacts/replay/<source_run_id>/<stamp>/trades.npz``.
    2. Read live artifacts from ``artifacts/live/`` — plans, tickets,
       and (optionally) deals.jsonl dumped by the VPS-side helper.
    3. Build the live DataFrame via ``ff.live.reconcile.build_live_df``.
    4. Call ``ff.live.reconcile.reconcile`` to match live vs backtest.
    5. Write HTML + JSON report to
       ``artifacts/live/reconcile/<stamp>.html`` /``.json``.
    6. Print the report counts dict to stdout.

No new reconcile logic — this is pure glue over existing primitives so
the end-to-end path is a single command.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows console default codepage can't encode the box-drawing + arrow
# chars the harness prints. Match run.py so a reconcile from stdout works.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ff import replay as _replay  # noqa: E402
from ff.live import reconcile as _reconcile  # noqa: E402

LIVE_DIR = REPO_ROOT / "artifacts" / "live"
REPLAY_DIR = REPO_ROOT / "artifacts" / "replay"
RECONCILE_DIR = LIVE_DIR / "reconcile"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"[reconcile] warn: skipped malformed line in {path.name}")
    return out


def _load_plans(plans_dir: Path) -> list[dict]:
    if not plans_dir.exists():
        return []
    plans: list[dict] = []
    for p in sorted(plans_dir.glob("*.jsonl")):
        plans.extend(_read_jsonl(p))
    return plans


def _load_bt_trades(source_run_id: str, stamp: str) -> pd.DataFrame:
    npz_path = REPLAY_DIR / source_run_id / stamp / "trades.npz"
    if not npz_path.exists():
        raise FileNotFoundError(f"replay npz missing: {npz_path}")
    z = np.load(npz_path, allow_pickle=True)
    bt = pd.DataFrame(z["trades"])
    if "entry_ts" in bt.columns:
        bt["entry_ts"] = pd.to_datetime(bt["entry_ts"], utc=True, errors="coerce")
    if "exit_ts" in bt.columns:
        bt["exit_ts"] = pd.to_datetime(bt["exit_ts"], utc=True, errors="coerce")
    if "pair" in bt.columns and bt["pair"].dtype == object:
        bt["pair"] = bt["pair"].astype(str)
    return bt


def _resolve_instance_config(instance_id: str | None,
                              explicit_config: Path | None) -> Path:
    """Pick which config.json to reconcile.

    1. If ``--config`` passed explicitly, use that.
    2. Else if ``--instance`` passed, use ``artifacts/live/<id>/config.json``.
    3. Else if exactly one instance exists under ``artifacts/live/*/config.json``,
       use it.
    4. Else if legacy ``artifacts/live/service_config.json`` exists, use it.
    5. Else error with the list of available instances.
    """
    if explicit_config is not None:
        return explicit_config

    if instance_id is not None:
        path = LIVE_DIR / instance_id / "config.json"
        if not path.exists():
            raise FileNotFoundError(
                f"No config for instance '{instance_id}' at {path}")
        return path

    candidates = sorted(p for p in LIVE_DIR.glob("*/config.json")
                        if p.parent.name not in ("archive", "reconcile"))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) == 0:
        legacy = LIVE_DIR / "service_config.json"
        if legacy.exists():
            return legacy
        raise FileNotFoundError(
            f"No instance configs found under {LIVE_DIR}. "
            "Deploy one via the web UI first.")
    listed = "\n".join(f"  - {c.parent.name}" for c in candidates)
    raise SystemExit(
        f"Multiple active instances found; pass --instance <id>:\n{listed}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fire Forex live vs backtest reconcile")
    parser.add_argument(
        "--instance", type=str, default=None,
        help="Instance id (dir under artifacts/live/). "
             "Omit to auto-pick if only one active instance exists.",
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Explicit config path. Overrides --instance.",
    )
    parser.add_argument(
        "--skip-replay", action="store_true",
        help="Reuse the most recent replay NPZ instead of re-running the backtest.",
    )
    args = parser.parse_args()
    args.config = _resolve_instance_config(args.instance, args.config)

    # Phase 1 — backtest side (fresh replay, or reuse latest).
    if args.skip_replay:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        source_run_id = str(config.get("source_run_id") or "unknown_run")
        latest_stamp_file = REPLAY_DIR / source_run_id / "latest_stamp.txt"
        if not latest_stamp_file.exists():
            raise FileNotFoundError(
                f"--skip-replay requested but no prior replay found at {latest_stamp_file}"
            )
        stamp = latest_stamp_file.read_text(encoding="utf-8").strip()
        print(f"[reconcile] reusing replay {source_run_id}/{stamp}")
    else:
        print(f"[reconcile] replaying {args.config}")
        summary = _replay.replay_service_config(args.config)
        source_run_id = summary["source_run_id"]
        stamp = summary["stamp"]
        print(f"[reconcile] replay done -> {source_run_id}/{stamp} "
              f"({summary['n_trades_total']} trades, {summary['elapsed_sec']}s)")

    bt_df = _load_bt_trades(source_run_id, stamp)
    print(f"[reconcile] backtest df: {len(bt_df)} rows")

    # Phase 2 — live side. Prefer the per-instance dir (next to config.json);
    # fall back to the legacy flat layout if the config is the top-level
    # service_config.json.
    instance_root = args.config.parent if args.config.parent != LIVE_DIR \
        else LIVE_DIR
    plans = _load_plans(instance_root / "plans")
    tickets = _read_jsonl(instance_root / "tickets.jsonl")
    deals = _read_jsonl(instance_root / "deals.jsonl")
    print(f"[reconcile] live inputs from {instance_root}: "
          f"plans={len(plans)} tickets={len(tickets)} deals={len(deals)}")
    live_df = _reconcile.build_live_df(plans, tickets, deals)
    print(f"[reconcile] live df: {len(live_df)} rows")

    # Phase 3 — match + write. Report goes inside the instance's reconcile
    # subdir (isolation; no collision across instances).
    reconcile_out = instance_root / "reconcile"
    report = _reconcile.reconcile(bt_df, live_df)
    html_path, json_path = _reconcile.write_report(report, reconcile_out, stamp)

    counts = {
        "matched": len(report.matched),
        "missing_in_live": len(report.missing_in_live),
        "extra_in_live": len(report.extra_in_live),
    }
    print(f"[reconcile] counts: {counts}")
    print(f"[reconcile] html:  {html_path}")
    print(f"[reconcile] json:  {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
