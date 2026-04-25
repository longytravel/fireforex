#!/usr/bin/env bash
# Stop hook: refuses to end the session if HANDOFF.md is stale relative to the
# most recent commit, or if uncommitted non-paperwork changes exist.
# Soft-warns (not blocks) if PROGRESS.md wasn't touched but main commits landed.

set -u
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || { echo '{"continue": true}'; exit 0; }

LOG=".claude/hooks/log.txt"
mkdir -p .claude/hooks
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) stop-hook fired" >> "$LOG" 2>/dev/null || true

INPUT="${INPUT:-$(cat 2>/dev/null || echo '{}')}"

if command -v python >/dev/null 2>&1; then
  PY=python
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=""
fi

is_reentry() {
  [ -z "$PY" ] && return 1
  printf '%s' "$INPUT" | "$PY" -c 'import json,sys
try:
    sys.exit(0 if json.load(sys.stdin).get("stop_hook_active") else 1)
except Exception:
    sys.exit(1)' 2>/dev/null
}
if is_reentry; then
  echo '{"continue": true}'
  exit 0
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo '{"continue": true}'
  exit 0
fi

head_commit=$(git rev-parse HEAD 2>/dev/null || echo "")
last_handoff_commit=$(git log -1 --format=%H -- HANDOFF.md 2>/dev/null || echo "")
handoff_uncommitted=$(git status --porcelain -- HANDOFF.md 2>/dev/null | wc -l | tr -d ' ')

if [ ! -f HANDOFF.md ] && [ "${handoff_uncommitted:-0}" -eq 0 ]; then
  echo '{"continue": false, "stopReason": "HANDOFF.md is missing. Create it with the current session state before ending."}'
  exit 0
fi

commits_since_handoff=0
if [ -n "$last_handoff_commit" ] && [ -n "$head_commit" ]; then
  commits_since_handoff=$(git rev-list --count "${last_handoff_commit}..${head_commit}" 2>/dev/null || echo 0)
  commits_since_handoff=$(echo "$commits_since_handoff" | tr -d ' ')
fi

# Uncommitted work outside paperwork / runtime state / artifacts.
uncommitted=$(git status --porcelain 2>/dev/null \
  | cut -c4- \
  | grep -v '^HANDOFF\.md$' \
  | grep -v '^PROGRESS\.md$' \
  | grep -v '^\.claude/hooks/log\.txt$' \
  | grep -v '^\.claude/scheduled_tasks\.lock$' \
  | grep -v '^artifacts/' \
  | grep -v '^live_artifacts/' \
  | wc -l | tr -d ' ')

# HARD BLOCK: real work pending but HANDOFF untouched and not in a recent commit.
if [ "${uncommitted:-0}" -gt 0 ] \
   && [ "${handoff_uncommitted:-0}" -eq 0 ] \
   && [ "${commits_since_handoff:-0}" -gt 3 ]; then
  echo '{"continue": false, "stopReason": "Uncommitted work exists and HANDOFF.md has not been updated. Refresh HANDOFF.md (run /handoff) before ending the session."}'
  exit 0
fi

# SOFT WARN: commits landed on main this session but PROGRESS not touched.
last_progress_commit=$(git log -1 --format=%H -- PROGRESS.md 2>/dev/null || echo "")
progress_uncommitted=$(git status --porcelain -- PROGRESS.md 2>/dev/null | wc -l | tr -d ' ')
commits_since_progress=0
if [ -n "$last_progress_commit" ] && [ -n "$head_commit" ]; then
  commits_since_progress=$(git rev-list --count "${last_progress_commit}..${head_commit}" 2>/dev/null || echo 0)
  commits_since_progress=$(echo "$commits_since_progress" | tr -d ' ')
fi

if [ "${commits_since_progress:-0}" -gt 5 ] && [ "${progress_uncommitted:-0}" -eq 0 ]; then
  echo '{"continue": true, "systemMessage": "Reminder: PROGRESS.md has not been updated despite recent commits. If any milestone shipped, tick its box before next session."}'
  exit 0
fi

echo '{"continue": true}'
exit 0
