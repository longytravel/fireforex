"""Multi-pair high-trade-count calibration for the live-parity validator.

Goal: produce a per-pair override/param set that fires enough trades (≥5/day)
over a short window so that live-vs-backtest parity can be measured quickly.
Profitability is explicitly NOT the optimisation target — trade density is.

Fitness: ``trade_count / window_days`` subject to two sanity floors:
  - ``total_pips >= 0`` (weeds out pure-churn "winners")
  - ``max_drawdown_pct < 30``

Usage (from repo root)::

    .venv\\Scripts\\python.exe scripts\\calibrate_for_parity.py \\
        --pairs EUR_USD GBP_USD USD_JPY \\
        --main-tf H1 --sub-tf M1 \\
        --start 2026-04-06 --end 2026-04-20 \\
        --trials 300 --seed 42

Output: ``artifacts/calibration/{pair}_{main_tf}_parity.json`` per pair,
containing the full best trial (for pinning in
``artifacts/live/params_pinned.json``) plus the objective we optimised for
and summary metrics of that trial.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
# Scripts run from anywhere — make sure `ff` and `app` imports resolve.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
EA_PATH = ROOT / "eas" / "complex01.py"
OUT_DIR = ROOT / "artifacts" / "calibration"


def _load_ea(path: Path):
    spec = importlib.util.spec_from_file_location("_parity_ea", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.EA


def _window_days(start: str, end: str) -> int:
    a = datetime.fromisoformat(start)
    b = datetime.fromisoformat(end)
    return max((b - a).days, 1)


def calibrate_pair(
    pair: str,
    main_tf: str,
    sub_tf: str,
    start: str,
    end: str,
    n_trials: int,
    seed: int,
) -> dict:
    """Run a sweep for one pair; return the best trade-dense trial."""
    import numpy as np

    from ff import harness as hs
    from ff.harness import METRIC_COLUMNS  # noqa: F401

    ea = dict(_load_ea(EA_PATH))
    ea["data"] = {**ea.get("data", {}), "pair": pair, "main_tf": main_tf, "sub_tf": sub_tf,
                  "start_date": start, "end_date": end}

    print(f"\n[calibrate] {pair} {main_tf}/{sub_tf} · {start} -> {end} · {n_trials} trials")

    # Run the sweep. The harness writes artifacts/runs/<id>.npz which we then
    # load back to get the per-trial metrics matrix.
    hs.run(
        ea,
        layer_name=f"parity_calib_{pair}",
        optimizer="random",
        seed=seed,
        n_trials=n_trials,
        open_browser=False,
    )

    run_file = sorted(
        (ROOT / "artifacts" / "runs").glob(f"parity_calib_{pair}_*.npz"),
        key=lambda p: p.stat().st_mtime,
    )[-1]
    z = np.load(run_file, allow_pickle=True)
    metrics = z["per_trial_metrics"]
    best_trial_all = json.loads(str(z["best_trial_json"]))

    # Post-facto re-rank by trade density under the sanity floors. The harness
    # already picks "quality" as its default; we override here.
    days = _window_days(start, end)
    trades = metrics[:, _metric_col("trades")]
    expectancy = metrics[:, _metric_col("expectancy_pips")]
    total_pips = trades * expectancy
    max_dd = metrics[:, _metric_col("max_dd_pct")]
    # Use metrics' trades_per_day if valid; fall back to trades/window_days.
    density_metric = metrics[:, _metric_col("trades_per_day")]
    density = np.where(np.isfinite(density_metric), density_metric, trades / days)

    valid = (total_pips >= 0) & (max_dd < 30.0) & (trades > 0)
    if not valid.any():
        # Fall back to the engine's default pick. Better than bailing.
        best_idx = int(np.argmax(trades))
        note = "NO trial met sanity floors — fell back to raw max trade count"
    else:
        density_valid = np.where(valid, density, -np.inf)
        best_idx = int(np.argmax(density_valid))
        note = f"{int(valid.sum())} / {len(valid)} trials passed sanity floors"

    summary = {
        "pair": pair,
        "main_tf": main_tf,
        "sub_tf": sub_tf,
        "start": start,
        "end": end,
        "n_trials": n_trials,
        "seed": seed,
        "window_days": days,
        "best_trial_index": best_idx,
        "trade_count": int(trades[best_idx]),
        "trades_per_day": float(density[best_idx]),
        "total_pips": float(total_pips[best_idx]),
        "max_drawdown_pct": float(max_dd[best_idx]),
        "note": note,
    }
    print(f"  → {summary['trade_count']} trades "
          f"({summary['trades_per_day']:.1f}/day) "
          f"pnl={summary['total_pips']:+.0f}p dd={summary['max_drawdown_pct']:.1f}% · {note}")

    return {
        "summary": summary,
        "best_trial_from_harness": best_trial_all,  # quality-based pick
        "best_trial_idx_by_density": best_idx,
        "run_file": str(run_file.relative_to(ROOT)).replace("\\", "/"),
    }


def _metric_col(name: str) -> int:
    """Resolve a metric column index by name via harness."""
    from ff.harness import METRIC_INDEX
    return METRIC_INDEX[name]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs", nargs="+", required=True,
                    help="e.g. EUR_USD GBP_USD USD_JPY AUD_USD USD_CAD NZD_USD USD_CHF EUR_JPY GBP_JPY")
    ap.add_argument("--main-tf", default="H1")
    ap.add_argument("--sub-tf", default="M1")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--trials", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    rollup = []
    for pair in args.pairs:
        try:
            payload = calibrate_pair(
                pair=pair,
                main_tf=args.main_tf,
                sub_tf=args.sub_tf,
                start=args.start,
                end=args.end,
                n_trials=args.trials,
                seed=args.seed,
            )
        except FileNotFoundError as exc:
            print(f"  !! skipped {pair}: {exc}")
            continue
        out_file = OUT_DIR / f"{pair}_{args.main_tf}_parity.json"
        out_file.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
        rollup.append(payload["summary"])
        print(f"  → wrote {out_file.relative_to(ROOT)}")

    if rollup:
        summary_path = OUT_DIR / "summary.json"
        summary_path.write_text(json.dumps(rollup, default=str, indent=2), encoding="utf-8")
        print(f"\n[calibrate] wrote rollup → {summary_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
