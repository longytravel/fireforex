---
description: Refresh HANDOFF.md with the current session state so the next session starts from truth.
---

Update `HANDOFF.md` — keep what's still accurate, edit only the sections that have changed. The file is the next session's starting point, not a session log; it should always read as a fresh snapshot of "where we are right now".

Required sections (preserve them, don't add/remove):

```markdown
# Handoff — <UTC date and time>

**Branch:** <current branch>
**Status:** <one-sentence status>

## Goal
<what this session is trying to achieve, plain English>

## Completed this session
- <bullet per shipped thing>

## Not yet done
- <bullet per remaining thing, ordered by priority>

## Failed approaches — DON'T REPEAT
- <anything that was tried and didn't work>

## Exact resume steps for next session
1. <step>
2. <step>
```

Rules:
- Plain English only — no shell commands, no file paths unless essential.
- If PR-in-flight: link the PR number.
- If a box in PROGRESS.md shipped this session, tick it.
- Commit only at explicit handoff or after PR merge — not every refresh. The session-end Stop hook handles the final paperwork commit.
