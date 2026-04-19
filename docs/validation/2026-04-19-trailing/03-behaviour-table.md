# 03 — Expected-behaviour table: trailing stop

Phase 3 of validate-forex-knob, run 2026-04-19 (afternoon).

## Plain-English summary

Same shape as the breakeven investigation. Five trades, hand-
calculated. Two control rows prove normal trailing works; three
bug rows show the engine writing a trailing SL above current
price and firing it for an unearned win on the next sub-bar.

The bug manifests whenever the trailing distance is small enough
relative to the triggering sub-bar's intrabar range — i.e. when
`sb_high − distance > sb_close` (long) or `sb_low + distance
< sb_close` (short). This can happen in fixed mode with a small
`distance` value, or in ATR mode with a small `atr_mult × atr_pips`.

## Fixture assumptions

- **Pair:** EURUSD, `pip_value = 0.0001`.
- **Spread = 0, slippage = 0** for clarity.
- **Original SL:** 30 pips behind entry (never hit in these
  scenarios).
- **Original TP:** 60 pips ahead (never hit in these scenarios).
- **`atr_pips` per trade** — set per scenario to drive ATR-mode
  distance explicitly.

## Scenarios

### Row 1 — Long, fixed mode, safe distance (control)

- **Setup:** long, entry = 1.10000. activate = 5 pips,
  distance = 10 pips (fixed mode).
- **Story:** trade moves to +5 pips, trail activates. New SL =
  `sb_high − 10 = 1.10006 − 10 = 1.09996`. This is below
  `sb_close = 1.10003` — trail is safely below price. Next
  sub-bar retraces below the new SL and the trade exits at
  `1.09996` for −4 pips (controlled loss).
- **Expected PnL:** `−4.0 pips`, `EXIT_TRAILING`.
- **Verdict if matches:** normal trailing works.

### Row 2 — Long, fixed mode, BUG (distance too small)

- **Setup:** long, entry = 1.10000. activate = 5 pips,
  distance = 1 pip.
- **Story:** trade spikes to +6 pips (sb_high = 1.10006). Trail
  activates. New SL = `sb_high − 1 = 1.10005`, which is **above**
  `sb_close = 1.10002`. Pre-fix: monotonicity passes, SL is
  written anyway. Next sub-bar's `sb_low = 1.10001 ≤ 1.10005`
  fires the SL. Exit at 1.10005 → `+5 pips, EXIT_TRAILING`.
- **Post-fix expected:** guard rejects. Trail never activates on
  this sub-bar. Trade continues; flat thereafter; closes at
  end-of-data at entry → `0.0 pips`.
- **Pre-fix expected:** `+5 pips`.

### Row 3 — Short, fixed mode, BUG mirror

- **Setup:** short, entry = 1.10000, original SL = 1.10030.
  activate = 5, distance = 1.
- **Story:** mirror of Row 2. Trigger sub-bar has
  `sb_low = 1.09994` (float_pnl short = +6). Trail activates.
  New SL = `sb_low + 1 = 1.09995`, which is **below**
  `sb_close = 1.09998`. Pre-fix: monotonicity passes. Next sub-bar
  `sb_high = 1.09999 ≥ 1.09995` fires the SL. Exit at 1.09995
  → short PnL = +5 pips.
- **Post-fix expected:** guard rejects. 0 pips.
- **Pre-fix expected:** `+5 pips`.

### Row 4 — Long, ATR mode, BUG (tiny effective distance)

- **Setup:** long, entry = 1.10000. activate = 5. mode = ATR,
  `atr_mult = 0.3`, per-signal `atr_pips = 3`. Effective distance
  = `0.3 × 3 = 0.9 pips`.
- **Story:** trade spikes to +6. Trail activates. New SL =
  `sb_high − 0.9 pip = 1.10006 − 0.00009 = 1.100051`, above
  `sb_close = 1.10002`. Pre-fix: monotonicity passes. Next sub-bar
  fires it. Exit at 1.100051 → `+5.1 pips`.
- **Post-fix expected:** guard rejects. 0 pips.
- **Pre-fix expected:** `+5.1 pips` (±0.2 tolerance).

### Row 5 — Short, fixed mode, safe distance (control)

- **Setup:** short, entry = 1.10000, SL = 1.10030, TP = 1.09940.
  activate = 5, distance = 10.
- **Story:** trade moves to +5 short-side (sb_low = 1.09995).
  Trail activates. New SL = `sb_low + 10 pip = 1.09995 + 0.0010
  = 1.10005`. Below original SL (1.10030) → monotonicity passes
  (tighter). Above `sb_close = 1.09998` → safe. pending_sl =
  1.10005. Next sub-bar `sb_high = 1.10010 ≥ 1.10005` fires.
  Exit at 1.10005 → short PnL = (1.10000 − 1.10005) / 0.0001
  = −5 pips.
- **Expected PnL:** `−5.0 pips`, `EXIT_TRAILING`.
- **Verdict if matches:** normal short trailing works.

## Summary table

| # | Setup              | mode  | activate | distance / ATR | Expected PnL PRE-fix | Expected PnL POST-fix | Bug? |
|---|--------------------|-------|----------|----------------|----------------------|-----------------------|------|
| 1 | Long, safe         | fixed | 5        | 10 pips        | −4 pips              | −4 pips               | No (control) |
| 2 | Long, tiny         | fixed | 5        | 1 pip          | +5 pips              | 0 pips                | **Bug** |
| 3 | Short, tiny mirror | fixed | 5        | 1 pip          | +5 pips              | 0 pips                | **Bug (mirror)** |
| 4 | Long, ATR tiny     | ATR   | 5        | 0.3 × 3 pip    | +5.1 pips            | 0 pips                | **Bug (ATR)** |
| 5 | Short, safe        | fixed | 5        | 10 pips        | −5 pips              | −5 pips               | No (control) |

## What Phase 4 must test

Each row → one pytest parametrize case. The test is written once
to encode the PRE-fix expected PnL. After the Rust fix lands,
rows 2, 3, 4 flip their expected values to the POST-fix column
and the test continues to pass against the fixed engine.
