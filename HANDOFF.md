# Handoff — 2026-04-25 (MT5 direct ingest shipped; ready for Pillar 5)

**Branch:** `feat/refresh-handoff` (this PR); `main` synced through PR #26.
**Status:** Today landed Pillar 1 (architecture stocktake), cleanup pass 2, PR-system refinements, MT5 direct-query toolkit, and 3 of 10 dependabot bumps. The next move is Pillar 5 (live↔backtest parity) using the new MT5 toolkit.

## What landed today (12 PRs merged)

### Stocktake (Pillar 1) — DONE
- **#16, #18, #19** — Phases A/B/C: file inventory, per-stage audit tables, 9 appendices via parallel agents.
- **#20** — Phases D/E/F: cleanup punch list, Pillars 2–6 roadmap, mermaid flow diagram.
- **#21** — Phase G: `scripts/check_map.py` + 9 tests + `.claude/hooks/check-architecture-map.sh` stop-hook nag.
- **#22** — Phase H: 9 high-confidence stale-doc deletions executed.
- **#23** — Phase I: PROGRESS ticked, HANDOFF refreshed, CLAUDE.md links to map.

### Cleanup + PR system
- **#24** — cleanup pass 2: deleted 10 more stale docs (`ROADMAP.md`, `rust-wishlist.md`, `CHANGES.md`, `REVIEW.md`, `exec-full-fix-plan.md`, `bug-hunting-research-brief.md`, 3× dated `docs/live/` files, `snapshot-home.md`).
- **#25** — PR-system refinements (5 changes from the stocktake retrospective):
  1. CLAUDE.md "Do" — explicit batching rule for tool calls
  2. `pr-checklist.yml` — auto-skip on docs-only PRs
  3. `workflow.md` — CodeRabbit named primary, Gemini = second opinion; combine-related-phases policy
  4. `settings.json` — drop the local force-push deny rules (branch protection on `main` is the real gate)
  5. `.gitignore` — patterns for `_pre_pr_diff.patch`, `review-*.md`, `_pr_*.md`

### MT5 direct toolkit
- **#26** — `scripts/import_mt5_report.py` + `scripts/mt5_status.py` + 2 desktop shortcuts.
  - Both hit the running MT5 terminal directly via `MetaTrader5` Python package — **no manual HTML export needed**.
  - Broker→UTC offset applied on connect (probe live EURUSD tick vs wall-clock UTC, same pattern as `ff/live/broker_mt5.py`). Avoids the broker-local timezone bug from 2026-04-22.
  - SL/TP enriched via `history_orders_get` (deals don't carry SL/TP).
  - Spread calc digit-aware (`info.digits`), works for FX + Gold + Index symbols.
  - `mt5_status.py` shows: account balance / equity / floating P&L, every open position with unrealised P&L + SL + TP, every pending order, live spread + swap per symbol.

### Dependabot (3 of 10)
- **#3** actions/checkout v6 · **#6** fastapi · **#7** pytest — all merged.
- 5 stale (need `@dependabot rebase`): #1 rayon, #2 codeql-action v4, #4 actions/cache v5, #5 dukascopy-python, #8 maturin.
- 2 with merge conflicts after siblings landed: #9 httpx, #10 pyyaml.

## Live state RIGHT NOW (per `scripts/mt5_status.py` against ICMarkets demo terminal)

- **Account #52754648** (ICMarketsSC-Demo): £2,918 balance, £2,910 equity, **-£8 floating P&L**.
- **67 currently open positions** across ~20 currency pairs.
- **14-day actual: 457 closed trades, 41% wins, net -£38**.
- Mix of `fireforex` (legacy) + per-strategy comments (`ff_ema_cross`, `ff_macd_cross`, `ff_donchian`).
- The user's earlier 18-trade HTML report (17/18 losses on 2026-04-23/24) was a slice; the broader 14-day picture is bad-but-not-catastrophic.

## What's next — concrete priority order

1. **Pillar 5 — live↔backtest parity (start here):** flow the 14-day MT5 trade history through reconciliation against backtest replay. The active deploy is `complexity_L10_EUR_USD_M15_*` × 3 instances trading 20+ pairs in portfolio mode. Compare each closed trade vs what backtest says should have happened. The 41% WR is the gap to diagnose.
2. **Live trade management toolkit (extends MT5 work):** `mt5.order_send(action=TRADE_ACTION_SLTP, ...)` to adjust SL/TP on open positions; emergency close-all from laptop; live diff "config says trade X pairs, MT5 has positions on Y pairs"; real-time spread monitor. Today the live runner only PLACES orders — it never re-touches them.
3. **Triage remaining dependabot PRs:** comment `@dependabot rebase` on #1, #2, #4, #5, #8, #9, #10. (Mass-commenting on PRs needs explicit user OK per agent-permission policy.)
4. **Open issues:** #12 (path-traversal in `app/routes.py`), #13 (sig_bar_index OOB in `core/src/trade_full.rs`), #14 (`win_rate` vs `win_rate_pct` mismatch).
5. **Pillar 2 (Multi-optimiser bench):** Optuna / CMA-ES / walk-forward — only after parity is healthy.

## Where to look

- **The map:** `docs/ARCHITECTURE_MAP.md` — top-of-file Mermaid + 6 stage tables + 9 appendices + Section 7 (cleanup) + Section 8 (Pillars 2–6 roadmap).
- **The MT5 toolkit:** `scripts/import_mt5_report.py` (history) + `scripts/mt5_status.py` (live state) + `scripts/desktop/{Import MT5 Report,Show MT5 Status}.bat` (one-click).
- **Workflow rules:** `.claude/rules/workflow.md` — MT5 conventions are codified in the "MT5 — direct-query conventions" section.
- **Completeness checker:** `python scripts/check_map.py` — exits 0 when every tracked file is referenced. The Stop-hook nag fires automatically if you change mapped files but not the map.

## Failed approaches — DON'T REPEAT

- **Initial PR-system pattern: 7 PRs for one logical task.** The stocktake split into 7 PRs cost ~5–10 min CI/review wait per cycle. New rule (in `workflow.md`): bundle related phases when same file / docs-only / under ~300 lines.
- **HTML fallback in MT5 importer.** Built it first; user pushed back ("why are we not going direct?"). Removed in PR #26. Lesson: lead with the canonical mechanism, don't ship "and also a fallback" by default.
- **Forgot broker→UTC offset on first MT5 importer pass.** Same trap as the 2026-04-22 deal-history bug (`MT5 Deal History Query Timezone Corrected to Broker Time` memory). Now codified in `workflow.md`: never trust raw MT5 `time` fields as UTC.
- **MT5 status script crashed on Windows cp1252 stdout** (used `→` arrow). Now reconfigures stdout to UTF-8 at script start; same pattern in both new scripts.
- **Phase C audit reported CLAUDE.md as 181 lines** — that was reading the local working tree (with uncommitted session-start mods). Always check `git show origin/main:<path>` for canonical line counts.
- **Initial cleanup list flagged ALL 6 dated `deploy/instances/*` bundles** for deletion. CodeRabbit caught: 3 of them (the 04-24 set) are listed in `active.json` as live trading instances. Per-file verification against `active.json` is mandatory before flagging deploy bundles for deletion.
- **Stacked PRs (#17 stacked on Phase A's branch)** auto-closed when Phase A merged via squash. Worked around with a fresh branch (PR #18). Never stack on a branch that's about to merge.
- **Serial single-tool-call turns** wasted user-visible cycles ("you keep stopping"). Now codified in CLAUDE.md "Do": batch independent edits / reads / bash into ONE response.

## Resume steps for next session

1. SessionStart hook injects HANDOFF + PROGRESS + recent commits + open issues.
2. Run `python scripts/mt5_status.py` to see current live state at session start.
3. Run `python scripts/import_mt5_report.py --days 14` to get fresh trade history into `artifacts/live/incoming/`.
4. Start Pillar 5 work: build a comparison script that takes that fresh history + replays backtest for the same window with the same EA config + classifies each trade as match/better/worse/missing/extra.
5. The completeness checker keeps the map honest — don't add a tracked file without a row.
