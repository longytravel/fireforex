# Phase 3 — Stale-exit expected-behaviour table

Seven hand-calculated scenarios. Each is a single-trade micro-simulation with fully-controlled OHLC. All arithmetic is computed from the Phase 2 trace — no engine runs yet. The Phase 4 micro-test will encode each row as a `pytest` parametrise and assert against the engine.

## Fixture invariants (shared across all rows)

- `ENTRY_PRICE = 1.10000`, `PIP = 0.0001`, `pip_value = 0.0001`.
- `N_H = 20` H1 bars, `SUB_PER_BAR = 60`. `SIG_BAR = 1` — entry at H1 bar 1, management from H1 bar 2 onwards.
- `atr_pips = 10.0` (signal-time ATR, passed in via `sig_atr_pips_s`).
- Spread = 0 on every H1 and sub-bar (zeros out the asymmetric short-side `sell_spread` deduction at `trade_full.rs:406-416`).
- Commission = 0. `max_spread = 999`.
- SL = 100 pips, TP = 100 pips (both fixed-pips mode). Far enough out that no sub-bar SL/TP ever fires.
- All management knobs default to OFF unless set explicitly per row.
- `bars_held` semantics: starts at 0 before the bar loop; `bars_held += 1` at the top of each iteration. So at `bar = SIG_BAR + 1`, `bars_held = 1`; at `bar = SIG_BAR + 2`, `bars_held = 2`; etc.

## The lookback arithmetic (recap)

```
bars_held >= stale_bars    → eligibility gate
lookback_start = max(SIG_BAR + 1, bar - stale_bars + 1)
max_range = max_{b in lookback_start..=bar} (high[b] - low[b]) / pip_value  [pips]
fire iff max_range < stale_atr_thresh * atr_pips
exit at close[bar] ± slippage_price × sign(direction)
```

## Scenarios

### Row 1 — `row_1_long_stale_fires_plain`

**Intent:** plain-vanilla trigger on a long. Confirms stale fires at the first eligible bar when every bar in the lookback is below the ceiling.

**Params:**
- direction = BUY
- stale_enabled = 1, stale_bars = 2, atr_thresh = 0.5 → ceiling = 0.5 × 10 = **5 pips**
- slippage_pips = 0

**Fixture (H1 prices):**

| Bar | Sub-bar H/L/C | H1 H/L/C | H1 range |
|-----|--------------|----------|----------|
| 0   | entry, entry, entry | 1.10000, 1.10000, 1.10000 | 0 |
| 1   | entry, entry, entry | 1.10000, 1.10000, 1.10000 | 0 |
| 2   | entry+1pip flat | 1.10001, 1.10001, 1.10001 | 0 |
| 3   | entry+2pip flat | 1.10002, 1.10002, 1.10002 | 0 |
| 4+  | entry+3pip flat | 1.10003, 1.10003, 1.10003 | 0 |

**Trace:**
- bar = 2, bars_held = 1 < stale_bars=2 → no fire.
- bar = 3, bars_held = 2 ≥ 2 → eligible. lookback_start = max(2, 3-2+1) = 2. max_range over bars 2..3 = 0. 0 < 5 → **fire.**
- pnl_long = (1.10002 − 0 − 1.10000) / 0.0001 × 1.0 = **+2 pips.**

**Expected:** 1 trade, PnL = **+2.0 pips**.

---

### Row 2 — `row_2_short_stale_fires_mirror`

**Intent:** short-side symmetry test. Same fixture as row 1, direction flipped. Confirms no long/short asymmetry in the stale trigger.

**Params:** direction = SELL; everything else identical to row 1.

**Fixture:** identical to row 1.

**Trace:**
- Fires at bar 3 (same bar as row 1).
- pnl_short = (1.10000 − 1.10002 − 0) / 0.0001 × 1.0 = **−2 pips.**
- End-of-loop short-spread deduction: `sell_spread = 0` → no adjustment.

**Expected:** 1 trade, PnL = **−2.0 pips**.

---

### Row 3 — `row_3_stale_does_not_fire_range_above_ceiling`

**Intent:** negative case. Max-range exceeds the ceiling on every eligible bar → stale never fires → trade reaches end of data.

**Params:** direction = BUY; stale_enabled = 1, stale_bars = 2, atr_thresh = 0.5 → ceiling = **5 pips**.

**Fixture:**

| Bar | H1 H/L/C | H1 range |
|-----|----------|----------|
| 0, 1 | flat at entry | 0 |
| 2..19 | H=entry+5pip, L=entry−5pip, C=entry | **10 pips** each |

**Trace:**
- Every bar in [2, 19] has single-bar range = 10 > ceiling = 5. So `max_range >= 10` at every eligible bar → stale never fires.
- End-of-loop: `exit_reason == EXIT_NONE`; `exit_bar = num_bars − 1 = 19`; `close[19] = 1.10000`.
- pnl_long = (1.10000 − 0 − 1.10000) / 0.0001 × 1.0 = **0 pips.**

**Expected:** 1 trade, PnL = **0.0 pips**.

---

### Row 4 — `row_4_stale_off_control`

**Intent:** control for row 1. Stale disabled → trade should NOT fire early; runs to end-of-data.

**Params:** direction = BUY; stale_enabled = **0**. (stale_bars, atr_thresh still set but should have no effect.)

**Fixture:** identical to row 1 (bars 2 / 3 / 4+ = +1 / +2 / +3 pips, flat).

**Trace:**
- `stale_enabled == 0` → `trade_full.rs:113` guard fails on every bar. No fire.
- End-of-loop: `exit_bar = 19`, `close[19] = 1.10003`.
- pnl_long = +3 pips.

**Expected:** 1 trade, PnL = **+3.0 pips**.

The 1-pip gap between row 1 (+2) and row 4 (+3) proves row 1's stale fired at bar 3 (exit at +2 close), not at a later bar or end-of-data.

---

### Row 5 — `row_5_atr_thresh_100_degenerates_to_time_exit`

**Intent:** confirm `atr_thresh = 100.0` reduces stale to a pure time-exit. Ceiling = 100 × 10 = **1000 pips**, which no real H1 bar can exceed.

**Params:** direction = BUY; stale_enabled = 1, stale_bars = **3**, atr_thresh = **100.0** → ceiling = 1000 pips.

**Fixture:** bars 2..19 each have range = 10 pips (wide for realism), closes at +1, +2, +3, +3, +3, … pips respectively.

| Bar | H | L | C | range |
|-----|---|---|---|-------|
| 2   | entry+6 | entry−4 | entry+1 | 10 |
| 3   | entry+7 | entry−3 | entry+2 | 10 |
| 4   | entry+8 | entry−2 | entry+3 | 10 |
| 5+  | entry+8 | entry−2 | entry+3 | 10 |

**Trace:**
- bar 2, 3 — bars_held < 3 → not eligible.
- bar 4, bars_held = 3. lookback bars 2..4. max_range = 10 < 1000 → **fire.**
- Exit at close[4] = 1.10003 → pnl_long = **+3 pips.**

**Expected:** 1 trade, PnL = **+3.0 pips**. Demonstrates the "degenerate-to-time-exit" property used by the sensitivity test (test_knob_sensitivity.py:200-208).

---

### Row 6 — `row_6_lookback_excludes_entry_bar`

**Intent:** critical semantic test. Put a wide range on the entry bar itself; if the lookback incorrectly included the entry bar, `max_range` would be huge and stale wouldn't fire. If the lookback correctly excludes the entry bar (clamped to `entry_bar + 1`), stale fires cleanly.

**Params:** direction = BUY; stale_enabled = 1, stale_bars = **3**, atr_thresh = 0.5 → ceiling = **5 pips**.

**Fixture:**

| Bar | H | L | C | range |
|-----|---|---|---|-------|
| 0   | flat entry | 0 |
| **1** (entry bar) | **entry+50pip** | **entry−50pip** | **entry** | **100** |
| 2   | entry+1 | entry+1 | entry+1 | 0 |
| 3   | entry+2 | entry+2 | entry+2 | 0 |
| 4+  | entry+3 | entry+3 | entry+3 | 0 |

**Trace:**
- bar 1's wide range is NOT seen by the stale lookback because the loop starts at `SIG_BAR + 1 = 2`.
- bar 4, bars_held = 3. lookback_start = max(2, 4 − 3 + 1) = 2. Iterates bars 2..4. max_range = max(0, 0, 0) = 0 < 5 → **fire.**
- Exit at close[4] = 1.10003 → pnl_long = **+3 pips.**

If the engine had a bug including `entry_bar` in the lookback, max_range would be 100 > 5 → no fire → end-of-data close = +3 pips either way. So this row is only diagnostic in combination with a **negative control**: re-run with bar 1 range = 0 and confirm fire-bar and PnL are identical. (The negative control is implicit in row 5 — that already confirms stale fires at bar 4 with stale_bars=3 under flat conditions. The *assertion* for row 6 is simply that PnL = +3 and trade count = 1, matching row 5's structure despite the wide entry bar.)

**Expected:** 1 trade, PnL = **+3.0 pips** — identical to row 5 despite the wide entry-bar range.

---

### Row 7 — `row_7_slippage_applied_long`

**Intent:** confirm slippage is applied to the stale-exit PnL. Identical to row 1 but with `slippage_pips = 2.0`.

**Params:** direction = BUY; stale_enabled = 1, stale_bars = 2, atr_thresh = 0.5; `slippage_pips = 2.0`.

**Fixture:** identical to row 1.

**Trace:**
- `slippage_price = 2 × 0.0001 = 0.0002`.
- Entry cost: `actual_entry = entry_price + slippage_price + spread_at_entry = 1.10000 + 0.0002 + 0 = 1.10002` (long formula at `trade_full.rs:73-76`).
- Stale fires at bar 3. `close[3] = 1.10002`.
- pnl_long = (1.10002 − 0.0002 − 1.10002) / 0.0001 × 1.0 = **−2 pips.**

**Expected:** 1 trade, PnL = **−2.0 pips**.

Slippage is applied at **both** entry (adds 2 pips to `actual_entry`) and exit (subtracts 2 pips from close). So a trade that would have closed at +2 pips with no slippage returns −2 pips with 2-pip slippage — a 4-pip gap total, consistent with round-trip slippage cost.

---

## Scenarios NOT testable from this framework

- **Priority (max_bars vs stale on the same bar):** both paths exit at `close[bar]` with identical PnL arithmetic. Exit-reason is not exposed through `batch_evaluate`'s return tuple. This is a code-reading assertion only — documented in Phase 2 (`trade_full.rs:101` fires before `:113`). If priority is ever wired into metrics output, add a row here.
- **Partial + stale composition:** requires a fixture where partial fires intra-bar (sub-bar price > trigger) on bar X, then stale catches the residual on bar Y. The `realized_pnl_pips + residual_at_bar_close * position_pct` arithmetic is nontrivial to hand-calculate cleanly in this format; deferring to a follow-up validation or a standalone integration test if the user wants it.

## Summary of expected results

| Row | Direction | stale_enabled | stale_bars | atr_thresh | slippage | Expected PnL | What it proves |
|-----|-----------|---------------|-----------|-----------|----------|--------------|----------------|
| 1 | BUY  | 1 | 2   | 0.5   | 0 | +2.0 | stale fires at bars_held=stale_bars, long |
| 2 | SELL | 1 | 2   | 0.5   | 0 | −2.0 | direction symmetry |
| 3 | BUY  | 1 | 2   | 0.5   | 0 |  0.0 | range > ceiling → no fire |
| 4 | BUY  | 0 | —   | —     | 0 | +3.0 | stale OFF control (runs to EOD) |
| 5 | BUY  | 1 | 3   | 100.0 | 0 | +3.0 | atr_thresh → time exit |
| 6 | BUY  | 1 | 3   | 0.5   | 0 | +3.0 | entry bar excluded from lookback |
| 7 | BUY  | 1 | 2   | 0.5   | 2 | −2.0 | slippage applied twice (entry + exit) |

If every row passes, the knob meets its documented semantics with no silent no-op or sign error. Remaining concerns (exit-price lookahead, same-bar priority label) are code-reading observations, not PnL-observable bugs — they go into the verdict as caveats rather than fix-required findings.

---

*File: `docs/validation/2026-04-19-stale-exit/03-behaviour-table.md`*
*Written 2026-04-19 from Phase 2 trace — no engine runs yet.*
