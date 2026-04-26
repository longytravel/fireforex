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
gh pr merge "$PR" --squash --delete-branch

git checkout main
git pull --ff-only origin main

echo "done: PR #$PR merged, local main synced"
