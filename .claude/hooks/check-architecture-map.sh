#!/usr/bin/env bash
# Stop-hook nag: if files in mapped directories changed in this session
# but docs/ARCHITECTURE_MAP.md didn't, prompt to update before session ends.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)" 2>/dev/null || exit 0

# Files modified or untracked in mapped directories (relative to last commit on current branch)
mapped_dirs="app|core|ff|scripts|docs|eas|tests|\\.claude|\\.github"
changed=$(git status --porcelain 2>/dev/null \
  | awk '{print $2}' \
  | grep -E "^($mapped_dirs)/" \
  || true)

if [ -z "$changed" ]; then
  exit 0
fi

# If the map itself was touched, all good
if echo "$changed" | grep -q "^docs/ARCHITECTURE_MAP\.md$"; then
  exit 0
fi

cat >&2 <<EOF
[architecture-map nag]
Code changed in mapped directories this session, but docs/ARCHITECTURE_MAP.md
was not updated. Update the map (audit verdicts, new files, removed files)
before ending the session.

Changed files:
$(echo "$changed" | sed 's/^/  /')
EOF

exit 1
