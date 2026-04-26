#!/usr/bin/env bash
# scripts/finalize_pr.sh — format + commit + push in one atomic step.
#
# Why: pre-commit was bouncing commits because formatting drifted between local
# edits and the ruff version pinned in pre-commit. Format first, stage, commit,
# push — single pass, no double-cycle. Pre-commit hooks are check-only so they
# either pass (we just formatted) or fail loud (something else is wrong).
#
# Usage:
#   scripts/finalize_pr.sh "feat(scope): commit message"
#   scripts/finalize_pr.sh "fix(scope): bug" --no-push   # commit but don't push
#
# Refuses to commit directly to main — branch first.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <commit-message> [--no-push]" >&2
  exit 2
fi

MSG="$1"
shift
PUSH=1
for arg in "$@"; do
  case "$arg" in
    --no-push) PUSH=0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$BRANCH" == "main" ]]; then
  echo "refusing to commit directly to main — branch first" >&2
  exit 1
fi

ruff format . --quiet

git add -A

if git diff --cached --quiet; then
  echo "nothing staged — working tree matches HEAD"
  exit 0
fi

git commit -m "$MSG"

if [[ "$PUSH" == "1" ]]; then
  git push -u origin "$BRANCH"
fi

echo "done: $BRANCH @ $(git rev-parse --short HEAD)"
