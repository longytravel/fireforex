# Architecture Stocktake — Design

**Date:** 2026-04-25
**Status:** Approved (visual brainstorm with user, 4 screens)
**Owner:** Pillar 1 — must ship before any other pillar starts.

## Goal

A single living document that does three jobs:

1. **Map** every file in the repo to where it lives in the system (top-to-bottom flow).
2. **Audit** every shipped component against what it was supposed to do (verdict per line).
3. **Roadmap** the unbuilt work as ordered pillars (2–6) so future sessions know what's next.

Used as a checklist for the double-check pass, an enhancement queue, and the delivery list. One source of truth.

## In scope today

- Pillar 1 only: the map + audit + cleanup punch list + a one-paragraph entry for each of pillars 2–6.

## Out of scope today

- Designs for pillars 2 (multi-optimiser), 3 (safety), 4 (dashboards), 5 (drift loops), 6 (new-EA workflow). Each gets its own brainstorm → spec → plan → build cycle when its turn comes up.

## Deliverable

A single file: **`docs/ARCHITECTURE_MAP.md`**. Long, vertical. Top of file = first thing the system does. Bottom = unbuilt pillars at lower-level detail.

Plus one supporting script: `scripts/check_map.py` (completeness checker).

Plus one stop-hook entry that calls the checker before session end.

## Document structure

### Header — the 6-stage flow (Mermaid)

```text
DATA → EA → SWEEP → INSPECT → DEPLOY → RECONCILE
  ↑                                         |
  └── MT5 broker data round-trip from VPS ──┘
```

ASCII preview only; implement this as a Mermaid block in `docs/ARCHITECTURE_MAP.md` so GitHub renders it for free.

### Sections 1–6 — one per stage

Each stage section has the same shape:

- One-line "what this stage should do"
- A **component table** with columns: name / path / supposed to / verdict / notes
- A **flows-up / flows-down** callout naming the neighbouring stages and the connecting files

Verdict scheme (used throughout the document):

- ✅ working as intended
- ⚠️ partial — has a known gap or needs hardening
- ❌ broken — does not deliver what its name implies
- 🔘 not started — placeholder for unbuilt component

Every component listed must have a verdict. Exhaustive — no "see folder" hand-waves. If a stage has 12 files, the table has 12 rows.

### Appendices — things that don't fit a stage

- **A. Documentation** — every file in `docs/` listed with a verdict. Dated session journals (`SESSION-2026-04-21*.md` etc.) flagged for cleanup; canonical docs (ARCHITECTURE.md, ROADMAP.md, knob-explanations.md, validation/builds dirs) flagged keep.
- **B. PRs & GitHub issues** — current snapshot. Open issues #12 / #13 / #14 mapped onto the relevant stage. Dependabot PRs noted.
- **C. Tests** — every file in `tests/` with what it covers + verdict.
- **D. CI / GitHub workflows** — `.github/workflows/` with what each runs.
- **E. .claude/ rules and hooks** — what each rule covers, what each hook fires on.
- **F. Scripts** — `scripts/` exhaustive list. This is where most suspected cruft lives. Delete / keep / unsure verdict per file.
- **G. Root files** — README, CLAUDE.md, HANDOFF.md, PROGRESS.md, run.py, pyproject.toml, Cargo.toml, .gitignore, etc.
- **H. Configs** — settings.json, dependabot.yml, .pre-commit-config.yaml, etc.
- **I. Artifacts** — what's in `artifacts/` (mostly gitignored runtime), one-line description of each persistent file.

### Section 7 — Cleanup punch list

Files explicitly recommended for deletion. Columns: path / one-line reason / delete-by date or "unsure". Sourced from the audit pass — anything tagged ❌ that's not load-bearing, plus dated session journals, plus `_tmp_*.py` patterns, plus duplicates / superseded scripts.

### Section 8 — Roadmap (Pillars 2–6)

One subsection per pillar:

- **Name** in plain English
- **What it gives you** (one paragraph)
- **Rough size** (small / medium / large / big project)
- **Dependencies** (other pillars or shipped work)
- **Lower-level component sketch** — what new files / modules will exist when shipped, so the user can see the future shape (not a design — a sketch)

The 6 pillars in agreed order:

1. Honest map & cleanup (this document)
2. Multi-optimiser bench (Bayesian / CMA-ES / walk-forward + experiment tracker)
3. Safety & stability (Monte Carlo, paper-trade gate, stability checks)
4. Dashboards & insight (full UI build, equity / drawdown / sensitivity)
5. Drift detection & feedback loops (auto BT⇄live parity, alert/retune triggers)
6. New-EA development workflow (template, mandatory pipeline, side-by-side comparison)

## Tooling decisions

| Tool | Decision | Reason |
|---|---|---|
| Mermaid (GitHub markdown) | YES | Free flow diagram, no extra tooling. |
| `scripts/check_map.py` | YES | Walks repo, warns if any tracked file isn't mentioned in ARCHITECTURE_MAP.md. Returns non-zero on miss. |
| Stop-hook nag | YES | Fires when files in `app/`, `core/`, `ff/`, `scripts/`, `docs/`, `eas/`, `tests/`, `.claude/`, `.github/` were modified in-session but ARCHITECTURE_MAP.md wasn't. |
| Obsidian / vault tools | NO | Adds friction; graph view is for note-links not code. |
| pydeps / code2flow / auto-graph | NO | Over-detail for a human audit. |
| GitHub Projects for audit tracking | OPTIONAL | Cheap if helpful; doesn't change the doc. |

## Living-document rules

- The map updates in the same session as the code change (or in the same PR). Enforced by the stop-hook nag + the completeness-checker.
- Verdicts get re-checked at the start of any pillar work that touches a stage.
- The cleanup punch list shrinks; it should not be a long-running graveyard.

## Acceptance criteria

1. `docs/ARCHITECTURE_MAP.md` exists.
2. Every tracked file in the repo appears at least once on the map (completeness checker exits 0).
3. Every audit-table row has a verdict.
4. Cleanup punch list contains at least the obviously-dead files (dated `docs/live/SESSION-*` journals, `_tmp_*.py`, any superseded `scripts/`).
5. The 6-pillar roadmap section is present with the agreed entries and component sketches.
6. The Mermaid diagram renders on GitHub.
7. `scripts/check_map.py` exists and is runnable on Windows + Linux.
8. Stop-hook entry added that calls the checker; tested by modifying a source file without touching the map and confirming the hook fires.

## Implementation phasing (hint for writing-plans)

The implementation will not land in one PR. Break it into reviewable phases:

- **Phase A** — Inventory pass. Generate raw file list from `git ls-files`, categorise by stage (manual judgement, captured as a draft table).
- **Phase B** — Stages 1–6 audit tables (one PR per 2 stages, or one big PR with verdicts).
- **Phase C** — Appendices A–I.
- **Phase D** — Cleanup punch list (audit-driven, no deletions yet — this PR is just the list).
- **Phase E** — Pillars 2–6 roadmap section.
- **Phase F** — Mermaid diagram.
- **Phase G** — `scripts/check_map.py` + stop-hook wiring + green test pass.
- **Phase H** — First cleanup-execution PR (ticks items off the punch list).

Each phase is a separate PR through the standard pre-PR ritual (Codex / CodeRabbit / Gemini review, then squash-merge).

## Notes for the user

- This spec mostly restates what we agreed visually in the brainstorm. Skim if you want, but you've already approved the shape.
- The actual writing of `ARCHITECTURE_MAP.md` is the next step. The implementation plan (produced by `superpowers:writing-plans`) will turn the phases above into checkable tasks.
