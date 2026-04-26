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
from ff.cost_realism import bt_gate  # noqa: E402
from ff.cost_realism import overlay as _overlay  # noqa: E402
from ff.live import reconcile as _reconcile  # noqa: E402

LIVE_DIR = REPO_ROOT / "artifacts" / "live"
REPLAY_DIR = REPO_ROOT / "artifacts" / "replay"
RECONCILE_DIR = LIVE_DIR / "reconcile"
COST_TABLE_PATH = REPO_ROOT / "artifacts" / "cost_table.json"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
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
    """Load a replay NPZ by stamped directory name.

    ``stamp`` is the actual directory name under ``artifacts/replay/<run_id>/``
    — post-Step-3 that includes the ``_<data_source>`` suffix
    (``20260422_203000_dukascopy``). Callers must pass the stamped form.
    """
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


def _resolve_stamp(source_run_id: str, data_source: str) -> str:
    """Find the latest replay stamp for a given data source.

    Reads ``latest_stamp_<data_source>.txt`` if present, falls back to
    ``latest_stamp.txt`` for dukascopy (legacy pre-Step-3 layout).
    """
    root = REPLAY_DIR / source_run_id
    per_source = root / f"latest_stamp_{data_source}.txt"
    if per_source.exists():
        return per_source.read_text(encoding="utf-8").strip()
    if data_source == "dukascopy":
        legacy = root / "latest_stamp.txt"
        if legacy.exists():
            return legacy.read_text(encoding="utf-8").strip()
    raise FileNotFoundError(f"no replay stamp pointer found for source={data_source!r} under {root} — run without --skip-replay first.")


def _run_or_reuse_replay(
    config_path: Path,
    config: dict,
    data_source: str,
    *,
    skip_replay: bool,
) -> tuple[str, str, pd.DataFrame]:
    """Returns ``(source_run_id, stamp, bt_df)`` for one data source."""
    source_run_id = str(config.get("source_run_id") or "unknown_run")
    if skip_replay:
        stamp = _resolve_stamp(source_run_id, data_source)
        print(f"[reconcile][{data_source}] reusing replay {source_run_id}/{stamp}")
    else:
        print(f"[reconcile][{data_source}] replaying {config_path}")
        summary = _replay.replay_service_config(config_path, data_source=data_source)
        source_run_id = summary["source_run_id"]
        # replay_service_config writes to "<stamp>_<data_source>/" — summary["stamp"]
        # carries only the bare timestamp, so we reconstruct the dir name.
        stamp = f"{summary['stamp']}_{data_source}"
        print(
            f"[reconcile][{data_source}] replay done -> {source_run_id}/{stamp} "
            f"({summary['n_trades_total']} trades, {summary['elapsed_sec']}s)"
        )
    bt_df = _load_bt_trades(source_run_id, stamp)
    print(f"[reconcile][{data_source}] backtest df: {len(bt_df)} rows")
    return source_run_id, stamp, bt_df


def _resolve_instance_config(instance_id: str | None, explicit_config: Path | None) -> Path:
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
            raise FileNotFoundError(f"No config for instance '{instance_id}' at {path}")
        return path

    candidates = sorted(p for p in LIVE_DIR.glob("*/config.json") if p.parent.name not in ("archive", "reconcile"))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) == 0:
        legacy = LIVE_DIR / "service_config.json"
        if legacy.exists():
            return legacy
        raise FileNotFoundError(f"No instance configs found under {LIVE_DIR}. Deploy one via the web UI first.")
    listed = "\n".join(f"  - {c.parent.name}" for c in candidates)
    raise SystemExit(f"Multiple active instances found; pass --instance <id>:\n{listed}")


def _run_pass(
    name: str,
    bt_df: pd.DataFrame,
    target_df: pd.DataFrame,
    reconcile_out: Path,
    stamp_base: str,
) -> dict[str, int]:
    """One reconcile pass. ``name`` is a short tag (e.g. ``A_live_vs_duka``)
    folded into the stamp so the three outputs don't collide."""
    report = _reconcile.reconcile(bt_df, target_df)
    stamp = f"{stamp_base}_{name}"
    html_path, json_path = _reconcile.write_report(report, reconcile_out, stamp)
    counts = {
        "matched": len(report.matched),
        "missing_in_live": len(report.missing_in_live),
        "extra_in_live": len(report.extra_in_live),
    }
    print(f"[reconcile][{name}] counts: {counts}")
    print(f"[reconcile][{name}] html:  {html_path}")
    print(f"[reconcile][{name}] json:  {json_path}")
    return counts


def _apply_cost_realism(bt_df: pd.DataFrame) -> pd.DataFrame:
    """Stamp cost-realism gate + overlay columns onto a replay bt_df.

    Column mapping from the NPZ structured array to the overlay API:
    - ``spread_entry_pips``  → ``duka_bt_spread_pips``
    - ``pnl_pips``           → ``raw_pnl_pips``
    - ``entry_ts`` and ``pair`` are already named correctly.

    ``telemetry_slippage_pips`` falls back to the cost-table default until
    Task 6 (per-pair telemetry) lands.
    """
    if bt_df.empty:
        return bt_df

    df = bt_df.copy()

    # Map NPZ column names to the overlay API's expected names.
    if "spread_entry_pips" in df.columns and "duka_bt_spread_pips" not in df.columns:
        df["duka_bt_spread_pips"] = df["spread_entry_pips"].astype(float)
    if "pnl_pips" in df.columns and "raw_pnl_pips" not in df.columns:
        df["raw_pnl_pips"] = df["pnl_pips"].astype(float)

    # Slippage telemetry fallback: cost-table default until Task 6.
    if "telemetry_slippage_pips" not in df.columns:
        table: dict = {}
        if COST_TABLE_PATH.exists():
            table = json.loads(COST_TABLE_PATH.read_text())
        pair_to_slip = {p: e["slippage_per_side_pips"] for p, e in table.get("pairs", {}).items()}
        df["telemetry_slippage_pips"] = df["pair"].map(pair_to_slip).fillna(0.5)

    # Guard: both required columns must exist before calling gate/overlay.
    missing = [c for c in ("duka_bt_spread_pips", "raw_pnl_pips") if c not in df.columns]
    if missing:
        print(f"[reconcile][cost-realism] skipping overlay — missing columns: {missing}")
        return df

    df = bt_gate.apply(df)
    df = _overlay.apply(df, cost_table_path=COST_TABLE_PATH)
    print(f"[reconcile][cost-realism] overlay applied: {len(df)} rows, gated={df['gated_out_reason'].notna().sum()}")
    return df


def main() -> int:
    parser = argparse.ArgumentParser(description="Fire Forex live vs backtest reconcile")
    parser.add_argument(
        "--instance",
        type=str,
        default=None,
        help="Instance id (dir under artifacts/live/). Omit to auto-pick if only one active instance exists.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Explicit config path. Overrides --instance.",
    )
    parser.add_argument(
        "--skip-replay",
        action="store_true",
        help="Reuse the most recent replay NPZ instead of re-running the backtest.",
    )
    parser.add_argument(
        "--data-source",
        type=str,
        default="dukascopy",
        choices=("dukascopy", "mt5", "both"),
        help="Which data source to backtest against. 'both' runs two "
        "replays (Duka + MT5) and produces three reconcile reports: "
        "A=live vs Duka-BT, B=live vs MT5-BT, C=Duka-BT vs MT5-BT.",
    )
    args = parser.parse_args()
    args.config = _resolve_instance_config(args.instance, args.config)
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))

    sources = ["dukascopy", "mt5"] if args.data_source == "both" else [args.data_source]

    # Phase 1 — backtest side. One replay per requested source.
    bt_by_source: dict[str, pd.DataFrame] = {}
    stamps: dict[str, str] = {}
    for ds in sources:
        _run_id, stamp, bt_df = _run_or_reuse_replay(
            args.config,
            config,
            ds,
            skip_replay=args.skip_replay,
        )
        bt_by_source[ds] = _apply_cost_realism(bt_df)
        stamps[ds] = stamp

    # Phase 2 — live side.
    instance_root = args.config.parent if args.config.parent != LIVE_DIR else LIVE_DIR
    plans = _load_plans(instance_root / "plans")
    tickets = _read_jsonl(instance_root / "tickets.jsonl")
    deals = _read_jsonl(instance_root / "deals.jsonl")
    print(f"[reconcile] live inputs from {instance_root}: plans={len(plans)} tickets={len(tickets)} deals={len(deals)}")
    live_df = _reconcile.build_live_df(plans, tickets, deals)
    print(f"[reconcile] live df: {len(live_df)} rows")

    # Phase 3 — match + write. Stamps for each pass share a base (latest
    # replay timestamp, taken from whichever source ran last) so they
    # sort together in the reconcile dir.
    reconcile_out = instance_root / "reconcile"
    stamp_base = stamps[sources[-1]]
    all_counts: dict[str, dict[str, int]] = {}

    if args.data_source == "both":
        # A: live vs Duka-BT  — the parity question.
        all_counts["A_live_vs_duka"] = _run_pass(
            "A_live_vs_duka",
            bt_by_source["dukascopy"],
            live_df,
            reconcile_out,
            stamp_base,
        )
        # B: live vs MT5-BT  — sanity cross-check (different data source).
        all_counts["B_live_vs_mt5"] = _run_pass(
            "B_live_vs_mt5",
            bt_by_source["mt5"],
            live_df,
            reconcile_out,
            stamp_base,
        )
        # C: Duka-BT vs MT5-BT — pure data-source drift, live excluded.
        # Pass MT5 as the "target_df" to reuse the matcher — rows are the
        # same shape as live_df for matching purposes (pair, direction,
        # signal_bar_ts).
        all_counts["C_duka_vs_mt5"] = _run_pass(
            "C_duka_vs_mt5",
            bt_by_source["dukascopy"],
            bt_by_source["mt5"],
            reconcile_out,
            stamp_base,
        )
    else:
        ds = args.data_source
        all_counts[f"live_vs_{ds}"] = _run_pass(
            f"live_vs_{ds}",
            bt_by_source[ds],
            live_df,
            reconcile_out,
            stamp_base,
        )

    print(f"[reconcile] summary: {all_counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
