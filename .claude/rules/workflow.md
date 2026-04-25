---
description: PR workflow and PROGRESS.md maintenance — keeps the user out of the loop
---

# Workflow

## PR cycle (small changes can still land on main direct; big changes go PR)
1. Branch: `feat/<slug>` or `fix/<slug>` for non-trivial work. One-line fixes and docs can land on main.
2. Red → green → refactor. Failing test first when feasible.
3. Pre-commit runs on every commit; fix its complaints.
4. Before opening PR, run the pre-PR ritual:
   - `/simplify` — housekeeping on the diff
   - `/code-review` — catch bugs on the diff
   - Codex mini review — second opinion via `codex exec -m gpt-5.4-mini --config model_reasoning_effort="high" --sandbox read-only`
   - **Skip the ritual entirely for docs-only PRs (`*.md` or `docs/*` only)** — the checklist CI auto-skips, and `/simplify` / Codex on prose adds nothing.
5. Paste review outputs into the PR body. (Docs-only PRs: skip this section, just describe what changed.)
6. Open PR with `gh pr create --fill`. CodeRabbit + Gemini Code Assist auto-review.
7. Address anything flagged; squash-merge with `gh pr merge --squash --delete-branch`.

## Reviewer priority — CodeRabbit primary, Gemini secondary
- **CodeRabbit is the gating reviewer.** Its catches have already saved real money once (PR #20 caught me about to delete the live trading instances). Address every CodeRabbit thread before merge.
- **Gemini Code Assist is the second opinion.** Useful but lower-priority — when its suggestion contradicts CodeRabbit, follow CodeRabbit unless Gemini's reasoning is materially stronger. Resolve threads with a brief reply when overruling.
- For docs-only PRs, Gemini's nits (mermaid quoting, prose tightening) are optional.

## Combine related phases into bigger PRs
- One logical task ≠ one PR per phase. The architecture stocktake shipped in 7 PRs (#16, #18, #19, #20, #21, #22, #23) when it could have shipped in 3-4. Each PR cycle costs ~5-10 minutes of CI + review wait.
- **Rule of thumb:** if multiple phases touch the same file, are all docs-only, or are all under ~300 added lines, bundle them. Split when the diff exceeds ~500 lines or crosses a clear boundary (docs ↔ code, code ↔ infra).
- Stacked PRs (one branch on top of another) are fragile — when the base branch merges via squash, GitHub auto-closes the stacked PR. Prefer a fresh branch off `main` after each merge.

## PROGRESS.md — I keep it current so the user doesn't have to
- When a PR merges to main: if the work matches an unchecked item in `PROGRESS.md`, tick it in the same session.
- When a new milestone emerges worth tracking, add a line and tick it once shipped.
- If unsure whether a change is a "milestone" — a milestone is something the user would describe out loud as "a thing we finished". Not every commit counts.
- Never rewrite or reorder PROGRESS. Only tick boxes and append new lines.

## GitHub issues — I track them, the user doesn't have to
- The SessionStart hook injects `gh issue list --state open` so I see open issues at every session start.
- When CodeRabbit / Gemini / Codex / CodeQL flag a real pre-existing bug on a PR, I open a GitHub issue (don't bury it in the PR thread that vanishes after merge).
- After a PR merges, I review the issue list and tick anything the PR closed (use `Closes #N` in PR descriptions to auto-close).
- The user never has to look at the issue list — if they ask "what bugs do we have?", I read what the hook gave me.

## HANDOFF.md — the Stop hook keeps me honest
- HANDOFF is refreshed before session end (run `/handoff` or rewrite it directly).
- The Stop hook blocks session end if real work is uncommitted AND HANDOFF wasn't updated. Fix the paperwork, don't bypass the hook.

## Commit messages — Conventional Commits
- `feat(scope): add X`
- `fix(scope): correct Y`
- `chore: refresh handoff`
- `docs: ...`, `test: ...`, `refactor: ...`
