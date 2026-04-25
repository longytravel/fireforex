"""One-shot migration: backfill `signal_family` + `signal_params` into
every `best_trial` dict already sitting in deploy/live configs.

Context — `ff/harness.py` now writes fingerprint fields alongside the bare
`signal_variant` int (docs/live/BUG-variant-id-not-stable-2026-04-22.md).
Configs deployed before the fix carry only the int, which reshuffles across
signal-library builds. This script resolves the fingerprint from the
source NPZ's `variant_map_json`, which is stable per-run, and writes it
back.

Idempotent: skips any config whose `best_trial` already has `signal_family`.
Safe to re-run.

Usage:
    .\\.venv\\Scripts\\python.exe scripts\\migrate_best_trial_fingerprint.py
    .\\.venv\\Scripts\\python.exe scripts\\migrate_best_trial_fingerprint.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

RUNS_DIR = REPO_ROOT / "artifacts" / "runs"


def _iter_configs() -> list[Path]:
    paths: list[Path] = []
    deploy_dir = REPO_ROOT / "deploy" / "instances"
    if deploy_dir.exists():
        for p in sorted(deploy_dir.glob("*.json")):
            if p.name == "active.json":
                continue
            paths.append(p)
    live_dir = REPO_ROOT / "artifacts" / "live"
    if live_dir.exists():
        for p in sorted(live_dir.glob("*/config.json")):
            paths.append(p)
    legacy = REPO_ROOT / "artifacts" / "live" / "service_config.json"
    if legacy.exists():
        paths.append(legacy)
    return paths


def _lookup_fingerprint(source_run_id: str, variant_id: int) -> tuple[str, dict] | None:
    """Open the source NPZ, read variant_map_json, return (family, params)
    for the given int. Returns None on any failure."""
    npz_path = RUNS_DIR / f"{source_run_id}.npz"
    if not npz_path.exists():
        return None
    try:
        z = np.load(npz_path, allow_pickle=True)
        variant_map = json.loads(str(z["variant_map_json"]))
    except Exception:
        return None
    if variant_id < 0 or variant_id >= len(variant_map):
        return None
    entry = variant_map[variant_id]
    return str(entry.get("family", "")), dict(entry.get("params", {}))


def _migrate_one(path: Path, *, dry_run: bool) -> str:
    """Return a one-line status string for console output."""
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return f"  SKIP  {path}  — parse error: {e}"

    best_trial = config.get("best_trial")
    if not isinstance(best_trial, dict):
        return f"  SKIP  {path}  — no best_trial dict"

    if best_trial.get("signal_family"):
        return f"  OK    {path}  — already has signal_family={best_trial['signal_family']!r}"

    source_run_id = config.get("source_run_id")
    variant_id = best_trial.get("signal_variant")
    if not source_run_id or variant_id is None:
        return f"  SKIP  {path}  — missing source_run_id or signal_variant (run_id={source_run_id!r}, variant={variant_id!r})"

    found = _lookup_fingerprint(str(source_run_id), int(variant_id))
    if found is None:
        return f"  SKIP  {path}  — could not resolve variant {variant_id} from {source_run_id}.npz (file missing or variant_map_json bad)"
    family, params = found

    best_trial["signal_family"] = family
    best_trial["signal_params"] = params
    action = "WOULD WRITE" if dry_run else "WROTE"
    if not dry_run:
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return f"  {action}  {path}  — variant {variant_id} -> family={family!r}  params={params}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")
    args = p.parse_args()

    configs = _iter_configs()
    print(f"Scanning {len(configs)} config(s)")
    changed = 0
    for cfg_path in configs:
        line = _migrate_one(cfg_path, dry_run=args.dry_run)
        print(line)
        if " WROTE " in line or "WOULD WRITE" in line:
            changed += 1
    suffix = " (dry run)" if args.dry_run else ""
    print(f"Done — {changed} config(s) updated{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
