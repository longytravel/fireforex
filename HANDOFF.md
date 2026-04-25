# Handoff — 2026-04-24 20:50Z

**Branch:** main
**Status:** Workbench port in progress. Live↔BT parity work paused while we install the review/discipline tooling (this session).

## Goal
Install the development harness (session-state hooks, deny list, path-scoped rules, GitHub PR workflow with three automated reviewers) so the user is not the quality gate. Then resume live↔BT parity work from a clean slate.

## Completed this session
- Ported Freedom's SessionStart + Stop hooks, adapted to Fire Forex paths.
- Wrote `.claude/settings.json` with deny list (no --force, no --amend, no direct uvicorn spawn, no `git checkout main --`, no force-push, etc.).
- Wrote path-scoped rules under `.claude/rules/`: python-style, rust-style, testing (integration-first for Fire Forex), trading (live-runner discipline), workflow (PR cycle + PROGRESS maintenance).
- Wrote `/handoff` slash command.
- Created `HANDOFF.md` and `PROGRESS.md` files at repo root.

## Not yet done
- Slim root `CLAUDE.md` to <150 lines; move detail into `.claude/rules/*.md`.
- `.github/` — PR template, issue templates, CODEOWNERS, dependabot, workflows (ci, pr-checklist, gitleaks, codeql).
- `.coderabbit.yaml` + `.pre-commit-config.yaml`.
- `scripts/pre-pr.ps1` — the three-reviewer pre-PR ritual (simplify + code-review + codex mini).
- GitHub-side (needs user on the laptop): branch protection, install CodeRabbit app, install Gemini Code Assist app.

## Failed approaches — DON'T REPEAT
- Planning this as a spec document for the user to sign off on. User is non-technical — formal sign-off becomes rubber-stamping. See memory `feedback_no_rubber_stamp_process.md`.

## Live↔BT parity work (paused, resumes after workbench lands)
- Current plan: `docs/live-bt-parity-plan.md` (committed in 622c9c0).
- Three-tier data architecture decided (obs 5703).
- Last MT5 BT run produced zero overlap with live pairs (obs 5681, 5682) — needs investigation when parity work resumes.
- VPS signal fingerprint patch deployment status unverified (obs 5524).

## Exact resume steps for next session
1. SessionStart hook will inject HANDOFF + PROGRESS + recent commits.
2. If this workbench install is still not finished, continue from "Not yet done" above.
3. Once workbench is landed, return to the parity plan in `docs/live-bt-parity-plan.md`.
4. Next live reconciliation target: 100% trade match on the next 10 live closes.
