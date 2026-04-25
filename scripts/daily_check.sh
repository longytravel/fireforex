#!/usr/bin/env bash
# scripts/daily_check.sh — daily live-vs-backtest parity check (one command).
#
# The full loop the user wants run every session:
#   1. Pull the day's MT5 history into artifacts/live/incoming/
#   2. BT replay the active deploy bundle against Dukascopy data
#   3. BT replay the active deploy bundle against MT5 data
#   4. Compare both BT outputs to the MT5 trade list (last --hours hours)
#
# Usage:
#   bash scripts/daily_check.sh                    # default window: 48h
#   bash scripts/daily_check.sh --hours 24
#   bash scripts/daily_check.sh --skip-mt5-bt      # if MT5 BT path is broken
#
# Output: stdout summary + artifacts/parity/<stamp>_parity.md

set -euo pipefail
cd "$(dirname "$0")/.."

HOURS=48
SKIP_MT5_BT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hours) HOURS="$2"; shift 2 ;;
    --skip-mt5-bt) SKIP_MT5_BT=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

PYTHON=".venv/Scripts/python.exe"
[[ -x "$PYTHON" ]] || { echo "venv python not found at $PYTHON" >&2; exit 1; }

ACTIVE=$("$PYTHON" -c "import json; print(json.load(open('deploy/instances/active.json'))['active'][0])")
BUNDLE="deploy/instances/${ACTIVE}.json"
[[ -f "$BUNDLE" ]] || { echo "active deploy bundle not found: $BUNDLE" >&2; exit 1; }

echo "============================================================"
echo " Daily parity check"
echo " bundle: $ACTIVE"
echo " window: last ${HOURS}h"
echo "============================================================"

echo ""
echo ">> 1/4  Pull MT5 history (last 14 days)"
"$PYTHON" scripts/import_mt5_report.py --days 14 | tail -8

echo ""
echo ">> 2/4  BT replay against Dukascopy"
"$PYTHON" run.py replay "$BUNDLE" --data-source dukascopy 2>&1 | tail -6

if [[ "$SKIP_MT5_BT" == "1" ]]; then
  echo ""
  echo ">> 3/4  BT replay against MT5 — SKIPPED (--skip-mt5-bt)"
else
  echo ""
  echo ">> 3/4  BT replay against MT5 data"
  if ! "$PYTHON" run.py replay "$BUNDLE" --data-source mt5 2>&1 | tail -6 ; then
    echo "   (MT5 BT replay failed — continuing with Dukascopy-only comparison)"
  fi
fi

echo ""
echo ">> 4/4  Parity comparison (last ${HOURS}h)"
"$PYTHON" scripts/daily_parity_check.py --hours "$HOURS"
