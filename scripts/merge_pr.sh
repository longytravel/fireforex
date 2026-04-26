#!/usr/bin/env bash
# scripts/merge_pr.sh — resolve threads + wait for CI + squash-merge + sync main.
#
# Why: after addressing CodeRabbit / Gemini comments the merge was a 4-step dance
# (resolve every thread, watch CI, merge, switch back to main). One command now.
#
# Usage:
#   scripts/merge_pr.sh <PR#>
#
# Assumes you've already addressed every comment substantively — the script just
# clicks "resolve" on the threads. If a real "must fix" is open and unaddressed,
# CI / branch protection should catch it, not this script.

set -euo pipefail

PR="${1:-}"
if [[ -z "$PR" ]]; then
  echo "usage: $0 <PR#>" >&2
  exit 2
fi

REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
OWNER="${REPO%/*}"
NAME="${REPO#*/}"
HEAD_BRANCH=$(gh pr view "$PR" --json headRefName -q .headRefName)

echo ">> fetching review threads on PR #$PR"
THREADS=$(gh api graphql -f query='
  query($owner: String!, $name: String!, $pr: Int!) {
    repository(owner: $owner, name: $name) {
      pullRequest(number: $pr) {
        reviewThreads(first: 100) {
          nodes { id isResolved }
        }
      }
    }
  }' -F owner="$OWNER" -F name="$NAME" -F pr="$PR" \
  --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | .id')

if [[ -z "$THREADS" ]]; then
  echo "   no unresolved threads"
else
  COUNT=$(printf '%s\n' "$THREADS" | grep -c .)
  echo ">> resolving $COUNT thread(s)"
  while IFS= read -r tid; do
    [[ -z "$tid" ]] && continue
    gh api graphql -f query='
      mutation($id: ID!) {
        resolveReviewThread(input: {threadId: $id}) {
          thread { id }
        }
      }' -F id="$tid" >/dev/null
  done <<< "$THREADS"
fi

echo ">> waiting for CI on PR #$PR"
gh pr checks "$PR" --watch --fail-fast

echo ">> squash-merging PR #$PR"
if gh pr merge "$PR" --squash --delete-branch; then
  echo "   merged directly"
else
  echo "   direct merge blocked; enabling/queueing auto-merge"
  if [[ "$(gh repo view "$REPO" --json viewerCanAdminister -q .viewerCanAdminister)" == "true" ]]; then
    gh repo edit "$REPO" --enable-auto-merge >/dev/null
  fi
  gh pr merge "$PR" --squash --delete-branch --auto

  echo ">> waiting for auto-merge to land"
  for _ in {1..60}; do
    STATE=$(gh pr view "$PR" --json state -q .state)
    [[ "$STATE" == "MERGED" ]] && break
    sleep 10
  done
  if [[ "$(gh pr view "$PR" --json state -q .state)" != "MERGED" ]]; then
    echo "error: PR #$PR is queued for auto-merge but has not merged yet" >&2
    gh pr view "$PR" --json mergeStateStatus,autoMergeRequest,url
    exit 1
  fi
fi

if [[ -n "$HEAD_BRANCH" ]] && git ls-remote --exit-code --heads origin "$HEAD_BRANCH" >/dev/null 2>&1; then
  echo ">> deleting merged remote branch $HEAD_BRANCH"
  git push origin --delete "$HEAD_BRANCH" >/dev/null || true
fi

git checkout main
git pull --ff-only origin main

echo "done: PR #$PR merged, local main synced"
