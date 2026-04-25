#!/usr/bin/env bash
# Stop-hook nag: if files in mapped directories changed in this session
# but docs/ARCHITECTURE_MAP.md didn't, surface a soft reminder.
#
# Non-blocking: emits a systemMessage rather than blocking the Stop event.
# (Pre-PR / CI is the real gate via `python scripts/check_map.py`.)

set -euo pipefail

cd "$(git rev-parse --show-toplevel)" 2>/dev/null || { echo '{"continue": true}'; exit 0; }

# Mapped paths: only real source/doc dirs. Excludes `.claude/`, root config
# (settings.json, .gitignore, .gitattributes…) and paperwork because byte-level
# changes to those files (often written by tooling, not Claude) wrongly
# triggered the nag every Stop event.
mapped_re='^(deploy/|app/|core/|ff/|scripts/|docs/|eas/|tests/|\.github/)'
ignore_re='^(\.claude/settings\.json|\.claude/hooks/log\.txt|HANDOFF\.md|PROGRESS\.md)$'

# git status --porcelain output is "XY filename" or "XY old -> new" for renames.
# Strip the 3-char status prefix, then for renames keep only the new name.
changed=$(git status --porcelain 2>/dev/null \
  | sed -e 's/^...//' -e 's/.* -> //' \
  | grep -E "$mapped_re" \
  | grep -Ev "$ignore_re" \
  || true)

if [ -z "$changed" ]; then
  echo '{"continue": true}'
  exit 0
fi

# If the map itself was touched, all good.
if echo "$changed" | grep -q "^docs/ARCHITECTURE_MAP\.md$"; then
  echo '{"continue": true}'
  exit 0
fi

# Soft reminder via systemMessage (does not block Stop, does not error).
file_list=$(echo "$changed" | head -10 | tr '\n' ',' | sed 's/,$//')
msg="Architecture map may need updating (changed: ${file_list}). Run python scripts/check_map.py before opening a PR."
echo "{\"continue\": true, \"systemMessage\": \"${msg}\"}"
exit 0
