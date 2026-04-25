---
description: Refresh HANDOFF.md with the current session state so the next session starts from truth.
---

Rewrite `HANDOFF.md` from scratch using this exact structure:

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
- Commit HANDOFF.md (and PROGRESS.md if you ticked a box) as a separate `chore: refresh handoff` commit.
