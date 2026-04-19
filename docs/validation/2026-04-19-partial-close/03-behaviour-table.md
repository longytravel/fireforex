# Phase 3 — Partial close behaviour table

Six hand-calculated scenarios. Each uses the same fixture geometry as the
existing breakeven and trailing micro-tests (5 H1 bars × 60 M1 sub-bars,
signal at H1 bar 1, trigger sub = 130, next sub = 131, fixed SL by pips,
fixed TP by pips, entry price 1.10000, pip value 0.0001, slippage 0,
spread 0).

For each row, the "PRE-FIX expected" column is what the engine **currently**
reports given the code-trace findings (Phase 2). The "POST-FIX expected"
column is what the engine **should** report once the ordering bug at
lines 285-310 is repaired. The gap between these two columns is the size
of the bug on that scenario.

Two distinct bugs surface in these scenarios:

  **Bug A — realise-at-sb_close.** The partial realises at the sub-bar
  close rather than at the limit price. Reachable in production: any
  trending sub-bar where sb_close > trigger over-states pnl by
  `(sb_close − trigger) × pct`. Hits rows 2, 3, 6.

  **Bug B — trigger-over-tp ordering.** Partial fires before TP within
  the sub-bar loop, so on a bar where sb_high crosses both trigger and
  tp with `trigger > tp`, partial realises "phantom" pnl. Unreachable in
  production because `sl_tp::compute_sl_tp` clamps `tp_distance >= sl_distance`
  and the schema limits `trigger ≤ 0.8 × sl_max` — we force it by setting
  SL=12,TP=12 in rows 4 and 5. Defence-in-depth fix regardless.

| # | Name                                             | Dir | SL pip | TP pip | Partial   | Trigger sub (H/L/C, pips) | Next sub (H/L/C, pips) | PRE-FIX | POST-FIX | Why |
|---|--------------------------------------------------|-----|--------|--------|-----------|---------------------------|------------------------|---------|----------|-----|
| 1 | Partial OFF, long → TP                           | BUY | 30     | 60     | off       | (+20, 0, +10)             | (+65, +30, +60)        | +60     | +60      | Baseline. Partial disabled; TP fires on sub 131 at +60. |
| 2 | Partial ON, trigger=10 < TP=60, long             | BUY | 30     | 60     | 10 / 50%  | (+15, 0, +12)             | (+65, +10, +60)        | **+36** | +35      | Pre-fix: partial realised at sb_close=+12 → 6 pips, TP=30 → 36. Post-fix: realised at trigger=+10 → 5 pips, TP=30 → 35. Bug A = 1 pip over-realisation. |
| 3 | Partial ON, trigger=10, short                    | SELL| 30     | 60     | 10 / 50%  | (0, −15, −12)             | (−10, −65, −60)        | **+36** | +35      | Mirror of row 2. Same Bug A. |
| 4 | **Bug B: trigger=44 > TP=12, long**              | BUY | 12     | 12     | 44 / 50%  | (+50, 0, +40)             | (+1, 0, 0)             | **+26** | **+12**  | sb_high=+50 satisfies both trigger (≥44) and TP (≥+12). Pre-fix: partial fires at sb_close=+40 → 20; TP fires on remainder at +12 → 6; total 26. Post-fix: TP closer to entry, fires first on full position → 12. |
| 5 | **Bug B: trigger=44 > TP=12, short**             | SELL| 12     | 12     | 44 / 50%  | (0, −50, −40)             | (0, −1, 0)             | **+26** | **+12**  | Mirror of row 4. |
| 6 | Partial ON, scratches to win through remainder SL| BUY | 20     | 60     | 15 / 70%  | (+20, 0, +18)             | (+18, −25, −22)        | **+6.6**| +4.5     | Pre-fix: partial at sb_close=+18 → 12.6; remainder SL −20 × 0.3 = −6; total 6.6. Post-fix: partial at trigger=+15 → 10.5; remainder SL −6; total 4.5. Bug A again. |

## Arithmetic in detail

### Row 1 (Partial OFF, baseline)

- Enter long at 1.10000.
- Trigger sub 130: (h=+20, l=0, c=+10). No partial. sb_high=+20 ≥ tp=+60?
  No. sb_low=0 ≤ sl=−30? No. No action.
- Next sub 131: (h=+65, l=+30, c=+60). sb_high=+65 ≥ tp=+60 → TP fires
  at tp_price=1.10060. pnl = 60 × 1.0 = **+60 pips**.
- Expected: +60.

### Row 2 (Partial ON, trigger < TP, long)

- Enter long at 1.10000.
- Trigger sub 130: (h=+15, l=0, c=+12). float_pnl_pips = (sb_high − entry)
  / pip = 15. 15 ≥ 10 (trigger) → partial fires. close_pct = 0.5.
  realised_partial = (sb_close − entry)/pip × 0.5 = 12 × 0.5 = **6**.
  position_pct becomes 0.5.
- SL check: sb_low=0 > sl=−30. No. TP check: sb_high=+15 < tp=+60. No.
- Next sub 131: (h=+65, l=+10, c=+60). TP check: sb_high=+65 ≥ tp=+60 →
  TP fires. pnl = (tp − entry)/pip × 0.5 = 60 × 0.5 = **30**.
- Total = 6 + 30 = **+36 pips**.

### Row 3 (Row 2 mirror, short)

- Enter short at 1.10000. TP_price = entry − 60 pip = 1.09940. SL_price
  = entry + 30 pip = 1.10030.
- Trigger sub 130: (h=0, l=−15, c=−12) → absolute prices
  (1.10000, 1.09985, 1.09988). float_pnl = (entry − sb_low)/pip = 15.
  15 ≥ 10 → partial fires. partial_pnl = (entry − sb_close)/pip × 0.5 =
  12 × 0.5 = 6. Spread cost = 0 × 0.5 = 0. Realised = 6.
- Next sub 131: (h=−10, l=−65, c=−60). TP check: sb_low = 1.09935 ≤
  tp_price = 1.09940 → TP fires. pnl = (entry − tp)/pip × 0.5 = 60 × 0.5
  = 30.
- Total = 6 + 30 = **+36 pips**.

### Row 4 (BUG — long, trigger > TP)

- Enter long at 1.10000. tp_price = 1.10012. sl_price = 1.09970.
- Trigger sub 130: (h=+50, l=0, c=+40). Sub-bar spans entry up to +50 pip
  and closes at +40 pip. In real trading the TP limit at 1.10012 would
  have filled *first*, on the way up.
- Partial check (engine order): float_pnl = 50 ≥ 44 → fires at sb_close
  = 1.10040. realised = (1.10040 − 1.10000)/0.0001 × 0.5 = 20.
  position_pct = 0.5.
- SL check: sb_low=0 > sl=−30. No.
- TP check (same sub): sb_high=+50 ≥ tp=+12 → TP fires at tp_price.
  pnl = 12 × 0.5 = 6.
- Total pre-fix = 20 + 6 = **+26 pips**.
- Post-fix (TP priority when tp closer to entry than trigger): TP fires
  alone on full position → 12 × 1.0 = **+12 pips**.
- Bug inflation = **14 pips per affected trade**.

### Row 5 (BUG — short, trigger > TP)

Exact mirror of row 4 on the short side. Same arithmetic, same inflation.
Pre-fix = +26, post-fix = +12.

### Row 6 (partial rescues a losing trade)

- Enter long at 1.10000. tp_price = 1.10060. sl_price = 1.09980.
- Trigger sub 130: (h=+20, l=0, c=+18). float=20 ≥ 15 → partial fires at
  sb_close = 1.10018. realised = 18 × 0.7 = 12.6. position_pct = 0.3.
- SL check: sb_low=0 > sl=−20. No. TP check: sb_high=+20 < tp=+60. No.
- Next sub 131: (h=+18, l=−25, c=−22). SL check: sb_low=−25 ≤ sl=−20
  → SL fires at sl_price = 1.09980. pnl = (1.09980 − 1.10000)/0.0001 ×
  0.3 = −20 × 0.3 = −6.
- Total = 12.6 − 6 = **+6.6 pips**.
- This is the legitimate, intended use of partial — scratches a trade
  to small win where a non-partial run would have been a −20 pip loss.

## What rows 4 and 5 mean for the 74.5 % win rate mystery

The user's suspicious trial: 12.5 pip TP, 58.75 pip SL, partial pct=72.75,
trigger=+44. Any trade where the **first** sub-bar after entry carries
sb_high from below +12.5 to above +44 (a ≥31-pip single-sub-bar range)
hits the bug. The partial realises ~40 pip × 72.75 % = 29.1 pips of
phantom profit that a real broker would never have printed, because the
TP order would have filled at +12.5 pips first and closed the whole
trade.

On USDJPY / GBPJPY during news, 30+ pip M1 ranges occur several times a
day. On majors in quiet hours, they are rare. The more volatile the
pair / session, the more trades are corrupted and the more the reported
win-rate and expectancy drift away from reality.

## Implications for Phase 4

The micro-test encodes these six rows directly. Rows 4 and 5 are
**expected to fail** against the post-fix expected values — i.e. with
the current engine they will produce +26 pips, disagreeing with the
"correct" +12. This is the whole point: the test is the forensic
evidence that the bug is live.

After the fix ships (Phase 6.5), the test's expected values flip to the
post-fix column and rows 4 and 5 start passing.
