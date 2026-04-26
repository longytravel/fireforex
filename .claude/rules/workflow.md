---
description: Agent-owned PR workflow and lightweight paperwork
---

# Workflow

## Operating Principle

The user sets direction; the agent owns execution. Do not ask the user to watch CI, press merge, tick checkboxes, resolve review threads, or read reviewer output. The agent should run the workflow end to end: code, test, PR, review fixes, merge, sync, and update paperwork.

## PR Cycle

1. Branch from fresh `main` for non-trivial work: `feat/<slug>`, `fix/<slug>`, or `docs/<slug>`.
2. Implement the change and run focused verification. Use broader tests when the blast radius is wide.
3. Open the PR with a plain-English summary and the important verification commands. PR-body checklist text is advisory; CI, reviewers, and the paperwork audit are the real gates.
4. Let CodeRabbit, Gemini Code Assist, and GitHub CI run.
5. The agent reads reviewer output, fixes real findings, replies/resolves threads, and ignores or documents non-actionable nits.
6. Merge with the Windows-native helper:
   - `powershell -ExecutionPolicy Bypass -File scripts/merge_pr.ps1 <PR#>`
7. If PowerShell is unavailable, use:
   - `bash scripts/merge_pr.sh <PR#>`

Both merge helpers resolve review threads, wait for checks, squash-merge, queue auto-merge if direct merge is blocked, wait for the merge to land, delete leftover branches, and sync local `main`.

## Reviewer Priority

- **CodeRabbit is primary.** Treat actionable CodeRabbit findings as real until proven otherwise.
- **Gemini Code Assist is secondary.** Fix concrete bugs. Resolve stale/outdated threads once the code has moved past them.
- If the reviewers disagree, follow the stronger technical reasoning and leave a short PR comment if needed.

## Keep Friction Low

- Do not run the old three-reviewer pre-PR ritual by default. Use it only for risky or ambiguous diffs.
- Do not block on PR-body formatting. The checklist workflow warns on hygiene issues but should not fail for missing ritual text.
- Do not use Stop hooks for paperwork. PR-time paperwork audit enforces durable updates without interrupting local work. Update `HANDOFF.md`, `PROGRESS.md`, and `ARCHITECTURE_MAP.md` directly when the task changes durable project state.
- Prefer PowerShell scripts on this Windows repo. Shell scripts remain available for Git Bash, but they are not the primary path.

## Before Writing New Scripts

Check `docs/ARCHITECTURE_MAP.md` first. If a script already does the job, use or improve it. If a new script is genuinely needed, add it to the map in the same PR.

## Useful Scripts

- `powershell -ExecutionPolicy Bypass -File scripts/merge_pr.ps1 <PR#>` — preferred PR merge path on Windows.
- `bash scripts/merge_pr.sh <PR#>` — same PR merge flow for Git Bash.
- `bash scripts/finalize_pr.sh "<commit-message>"` — format + commit + push helper for Git Bash sessions.
- `bash scripts/sync_main.sh [--force-reset]` — sync local `main`; use only after preserving local dirty runtime artifacts.

## MT5 Direct-Query Conventions

- Trade history: `scripts/import_mt5_report.py` or `scripts/desktop/Import MT5 Report.bat`.
- Live state: `scripts/mt5_status.py` or `scripts/desktop/Show MT5 Status.bat`.
- Output goes under `artifacts/live/incoming/` and is runtime data, not PR content.
- MT5 timestamps are broker-local. Scripts compute broker-to-UTC offset; never trust raw MT5 `time` fields as UTC.

## Paperwork

Paperwork updates are CI-enforced by the PR-checklist workflow on any PR that touches durable paths (`ff/`, `core/`, `app/`, `tests/`, `scripts/`, `.claude/`, `.github/`, etc.). Forgetting a required update blocks merge.

- `PROGRESS.md`: append/tick only for real milestones. Required on every durable PR.
- `HANDOFF.md`: refresh before ending a substantial session. Required on every durable PR.
- `docs/ARCHITECTURE_MAP.md`: update when tracked files, ownership, or durable behavior changes. Required on map-sensitive durable PRs (most of them).

## Commit Messages

- `feat(scope): add X`
- `fix(scope): correct Y`
- `docs: refresh workflow`
- `chore: refresh handoff`
