#!/usr/bin/env bash
# Stop-hook nag: if files in mapped directories changed in this session
# but docs/ARCHITECTURE_MAP.md didn't, prompt to update before session ends.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)" 2>/dev/null || exit 0

# Mapped paths: stage/appendix-routed top-level dirs + any tracked root file.
# Root files are `[^/]+$` (no slash anywhere in the path).
mapped_re='^(deploy/|app/|core/|ff/|scripts/|docs/|eas/|tests/|\.claude/|\.github/|[^/]+$)'

# git status --porcelain output is "XY filename" or "XY old -> new" for renames.
# Strip the 3-char status prefix, then for renames keep only the new name.
changed=$(git status --porcelain 2>/dev/null \
  | sed -e 's/^...//' -e 's/.* -> //' \
  | grep -E "$mapped_re" \
  || true)

if [ -z "$changed" ]; then
  exit 0
fi

# If the map itself was touched, all good.
if echo "$changed" | grep -q "^docs/ARCHITECTURE_MAP\.md$"; then
  exit 0
fi

cat >&2 <<EOF
[architecture-map nag]
Mapped files changed this session, but docs/ARCHITECTURE_MAP.md was not updated.
Update the map (audit verdicts, new files, removed files) before ending the session.

Changed files:
$(echo "$changed" | sed 's/^/  /')
EOF

exit 1
