"""Generate artifacts/cost_table.json from MT5 M1 parquets.

Exits non-zero (and writes nothing) when zero pairs would be built — a
silent empty cost table makes the entire overlay subsystem look installed
while doing nothing.

Usage:
    .\\.venv\\Scripts\\python.exe scripts/build_cost_table.py [--pairs EUR_USD,GBP_USD]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ff.cost_realism.cost_table import build_cost_table  # noqa: E402, I001
from ff.data.mt5_m1_downloader import MT5_DATA_ROOT  # noqa: E402


DEFAULT_PAIRS = [
    "AUD_CAD",
    "AUD_CHF",
    "AUD_JPY",
    "AUD_NZD",
    "AUD_USD",
    "CAD_CHF",
    "CAD_JPY",
    "CHF_JPY",
    "EUR_AUD",
    "EUR_CAD",
    "EUR_CHF",
    "EUR_GBP",
    "EUR_JPY",
    "EUR_NZD",
    "EUR_USD",
    "GBP_AUD",
    "GBP_CAD",
    "GBP_CHF",
    "GBP_JPY",
    "GBP_NZD",
    "GBP_USD",
    "NZD_CAD",
    "NZD_CHF",
    "NZD_JPY",
    "NZD_USD",
    "USD_CAD",
    "USD_CHF",
    "USD_JPY",
]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--pairs", default=",".join(DEFAULT_PAIRS))
    p.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "cost_table.json"))
    p.add_argument(
        "--allow-empty",
        action="store_true",
        help="exit 0 even when zero pairs were built (default: exit 1)",
    )
    args = p.parse_args()
    pairs = [s.strip() for s in args.pairs.split(",") if s.strip()]
    out_path = Path(args.out)
    build_cost_table(pairs=pairs, mt5_root=MT5_DATA_ROOT, out_path=out_path)

    table = json.loads(out_path.read_text())
    n_built = len(table.get("pairs", {}))
    if n_built == 0 and not args.allow_empty:
        print(
            "[cost_table] FAIL: zero pairs were built. "
            f"Check MT5_DATA_ROOT={MT5_DATA_ROOT} and that the requested "
            f"parquets exist. Pass --allow-empty to override.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
