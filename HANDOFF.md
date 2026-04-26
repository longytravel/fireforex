# Handoff — 2026-04-26 evening (cost-realism UI shipped + merge guardrails)

**Branch:** `docs/pr35-handoff-automerge-guardrails` while this refresh is in review. If this file is on `main`, the docs/tooling refresh has merged.
**Main status:** `origin/main` includes PR #35 as squash commit `c6c66da` — History tab cost-realism decomposition columns are shipped.
**Local sync note:** pre-sync local edits to `HANDOFF.md` / `artifacts/history.csv` were protected in stash `pre-sync local handoff/history before PR35 docs refresh` before fast-forwarding local `main`.

## Goal
Make Dukascopy backtests show what live IC Markets would actually have made — cost-realism overlay (3-pip spread cap, 3-pip slippage cap, 21:00–24:00 UTC rollover skip, MT5 session-median spreads, per-pair commission, telemetry-fed slippage), with one source of truth (`gate_rules.py`) shared between the backtest gate and live runner. The UI now decomposes adjusted P&L so users can see *why* adjusted differs from raw.

## Completed this session
- **PR #40 opened** — mega brute-force sweep foundation:
  - Signal libraries now cache contiguous per-variant slices (`variant_start` / `variant_end`).
  - Rust `batch_evaluate` auto-detects variant-contiguous arrays and evaluates only the chosen variant slice instead of scanning the whole pooled library.
  - Lean chunked sweep mode now handles large runs with compact `.npy` metrics sidecars and retained best-by-metric PnL/trial details.
  - Web/API trial cap is raised to 50M; rich artifacts remain capped at 50k and `auto` switches large runs to lean mode.
  - Run page scatter/trial APIs read lean metrics sidecars and retained trial curves.
  - Verification: focused pytest suite `34 passed, 6 skipped`, Rust `cargo test` `12 passed`, lean smoke run on temp parquet/artifacts, synthetic scan-heavy benchmark about `7.7x` faster for variant-sorted vs interleaved fallback.
- **PR #31 merged** as commit `63a3faa` — full cost-realism subsystem (5 PRs bundled): BT post-pass gate/overlay, cost table, telemetry-fed slippage, live execution guard sharing `ff/cost_realism/gate_rules.py`.
- **PR #35 merged** as commit `c6c66da` — History tab now shows:
  - `Adj. pips` — `adjusted_total_pips` after overlay
  - `Gate save` — pips saved by dropping bad-cost trades
  - `Cost` — spread/commission/slippage overhead on surviving trades
  - `Gated` — dropped-trade count
  - `CR` — cost-realism status pill (`ok` / `empty` / `failed`)
- **Harness decomposition persisted** to NPZ + `artifacts/history.csv`: `adjusted = total + gate_save + cost_overhead`. Unit test enforces the identity.
- **End-to-end smoke verified** with the real cost table: raw `278.6`, adjusted `340.5`, gate save `101.9`, cost overhead `-40.0`, gated `7`, status `ok`.
- **Repo merge settings fixed**: auto-merge and delete-branch-on-merge are enabled. PR #35 initially stayed blocked because two stale Gemini review threads were unresolved; resolving them triggered auto-merge immediately.
- **Merge guardrail improved**: `scripts/merge_pr.ps1` is now the preferred Windows-native closer; `scripts/merge_pr.sh` remains the Git Bash equivalent. They resolve review threads, wait for CI, fall back to auto-merge when direct merge is blocked, wait for the PR to actually merge, and delete the remote branch if GitHub leaves it behind.
- **Stop-hook alternative installed**: PR-body ritual text is advisory, but the PR checklist workflow now has a paperwork audit. Durable code/tooling changes must update `HANDOFF.md`; architecture-map-sensitive changes must update `docs/ARCHITECTURE_MAP.md`. This enforces paperwork at PR time, where the agent can fix it, instead of interrupting local work with Stop hooks.
- **Stop hooks remain retired**. `.claude/settings.json` is `{}`; no Stop-hook paperwork gate. Update paperwork directly before finishing.

## Not Yet Done
- **Issue #32** — `MatchedRow` propagation in `ff/live/reconcile.py:82`. This is the smallest cost-realism follow-up: matched live-vs-BT rows still need the new overlay/gate columns carried into the reconcile headline report.
- **Issue #33** — live guard reads stale closed-bar spread, not the submit-time tick. Needs fresh `mt5.symbol_info_tick` immediately before broker submit.
- **Issue #34** — post-fill slippage cap is documented but not enforced after order fill.
- **Mega brute-force follow-ups** — lean mode currently retains detailed PnL for best-by-metric candidates, but arbitrary non-retained trial clicks show metrics with an empty equity curve. Next speed layer is a metrics-only Rust kernel plus deterministic trial reconstruction for exact replay of any trial ID.
- **Three older scanner findings:** #12 path traversal in `app/routes.py`, #13 out-of-bounds `sig_bar_index`, #14 metric key mismatch (`win_rate` vs `win_rate_pct`).
- **Backport the live runner's forming-candle fix** into the BT engine.
- **Docs sweep** — markdownlint MD040 in `docs/superpowers/specs/*` and `docs/superpowers/plans/*`, stale `is_news_window` placeholder reference, session-name canonicalisation.

## Failed Approaches — Don't Repeat
- Don't replace `pnl_pips` with adjusted P&L as the headline metric. Keep raw and adjusted side-by-side.
- Don't store enriched best-trial trades as a numpy recarray in NPZ. JSON-encoded `uint8` bytes avoid pickle and round-trip cleanly.
- Don't insert the live execution guard at the top of `_poll_one_pair`; it belongs immediately before broker submit.
- Don't leave UI work on an unmerged branch after backend work lands. If a user says "I can't see it in the UI", check `git branch -vv`, `gh pr list --head <branch>`, and whether local `main` includes the UI commit.
- Don't assume green checks mean mergeable. Resolve outdated review threads too; branch protection can remain blocked until every thread is resolved.

## Exact Resume Steps
1. Make sure any docs/tooling refresh PR containing this handoff is merged.
2. Run `git checkout main && git pull --ff-only origin main` before new work. If local runtime artifacts are dirty, stash them first.
3. Pick up issue #32 (`MatchedRow` cost-realism column propagation).
4. Then issues #33 and #34.
5. If PR #40 has merged, smoke a real Level 10 run from the Run page with `n_trials > 50_000` so `artifact_mode=auto` exercises lean mode on real data.
6. Then the deferred docs sweep.

## Useful Commands
- Merge a green PR safely: `bash scripts/merge_pr.sh <PR#>`
- Check unresolved review threads: `gh pr view <PR#> --json mergeStateStatus,autoMergeRequest,url`
- Sync main safely: `bash scripts/sync_main.sh`

## In Flight
- PR #40 (`feat/mega-brute-sweep-engine`) — mega brute-force implementation branch, separate from Claude's cost-table/live-reconcile worktree.
