#!/usr/bin/env bash
# scripts/sync_main.sh — bring local main back in sync with origin/main.
#
# Why: occasionally local main drifts (a stray commit lands on it, or upstream
# fast-forwards past us via PR squash-merges). The deny list correctly blocks
# `git reset --hard` for ad-hoc use; this curated script is the safe escape hatch.
#
# Usage:
#   scripts/sync_main.sh                # ff-only; refuses if local has unpushed commits
#   scripts/sync_main.sh --force-reset  # destructive: hard-reset local main to origin/main
#
# --force-reset prints what's about to be discarded BEFORE doing it, so you can
# Ctrl-C if the commits look unsafe.

set -euo pipefail

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force-reset) FORCE=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "working tree dirty — commit or stash first" >&2
  exit 1
fi

CURRENT=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT" != "main" ]]; then
  echo ">> switching to main (was on $CURRENT)"
  git checkout main
fi

git fetch origin

LOCAL_AHEAD=$(git rev-list --count origin/main..main)
LOCAL_BEHIND=$(git rev-list --count main..origin/main)

if [[ "$LOCAL_AHEAD" == "0" && "$LOCAL_BEHIND" == "0" ]]; then
  echo "already in sync"
  exit 0
fi

if [[ "$LOCAL_AHEAD" == "0" ]]; then
  echo ">> ff-only pull ($LOCAL_BEHIND commit(s) behind)"
  git merge --ff-only origin/main
  exit 0
fi

echo "local main has $LOCAL_AHEAD commit(s) not on origin/main:"
git log --oneline origin/main..main
echo

if [[ "$FORCE" != "1" ]]; then
  echo "refusing to clobber — pass --force-reset if these commits are safe to discard" >&2
  exit 1
fi

echo ">> hard-resetting local main to origin/main"
git reset --hard origin/main
