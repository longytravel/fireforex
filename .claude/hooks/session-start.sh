#!/usr/bin/env bash
# SessionStart hook: load HANDOFF.md + PROGRESS.md into the new session so the
# agent always starts from the last known state. Non-blocking.

set -u
export LANG=C.UTF-8 LC_ALL=C.UTF-8 2>/dev/null || true
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || { echo '{}'; exit 0; }

LOG=".claude/hooks/log.txt"
mkdir -p .claude/hooks
SRC="${CLAUDE_HOOK_SOURCE:-unknown}"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) session-start-hook fired (source=${SRC})" >> "$LOG" 2>/dev/null || true

HANDOFF_TEXT="_HANDOFF.md not found_"
PROGRESS_TEXT="_PROGRESS.md not found_"
[ -f HANDOFF.md ] && HANDOFF_TEXT=$(cat HANDOFF.md)
[ -f PROGRESS.md ] && PROGRESS_TEXT=$(cat PROGRESS.md)

RECENT_COMMITS=$(git log --oneline -10 2>/dev/null || echo "_not a git repo_")
CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo "_unknown_")
STATUS=$(git status --short 2>/dev/null || echo "")

CONTEXT=$(cat <<SESSION_START_EOF
# Session start -- paperwork snapshot

**Current branch:** ${CURRENT_BRANCH}

## HANDOFF.md

${HANDOFF_TEXT}

## PROGRESS.md

${PROGRESS_TEXT}

## Recent commits (git log --oneline -10)

\`\`\`
${RECENT_COMMITS}
\`\`\`

## Uncommitted changes (git status --short)

\`\`\`
${STATUS:-"(clean)"}
\`\`\`

If HANDOFF.md says mid-stream work is in progress, **finish it before starting anything new**.
SESSION_START_EOF
)

if command -v python >/dev/null 2>&1; then
  printf '%s' "$CONTEXT" | python -c "import json,sys; print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': sys.stdin.read()}}))"
elif command -v python3 >/dev/null 2>&1; then
  printf '%s' "$CONTEXT" | python3 -c "import json,sys; print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': sys.stdin.read()}}))"
else
  echo '{}'
fi

exit 0
