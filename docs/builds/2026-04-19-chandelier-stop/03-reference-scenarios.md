# 03 — Reference scenarios: `chandelier_stop`

> 7 scenarios with hand-calculated expected outcomes. Written before
> phase 4 code. If a row cannot be computed by hand, phase 1 is too
> vague.

## Setup notes

- Pair: **EURUSD**, pip_value = `0.0001`.
- TF: **H1**.
- Spread: 0 (single-series engine, unless row specifies a pip cost).
- ATR (held constant across bars for clarity): `atr_pips = 10` →
  `atr_price = 10 * 0.0001 = 0.0010`.
- Knob settings unless overridden: `chandelier_activate = 10.0 pips`,
  `chandelier_atr_mult = 3.0` → chandelier distance
  = `3.0 * 10 * 0.0001 = 0.0030` price units = 30 pips.
- Entry fill: exact `actual_entry` (engine convention). Spread already
  applied on entry where relevant.
- Engine reads sub-bar as O→H→L→C for long bias, O→L→H→C for short
  (Fire Forex convention per primer §6). All rows use this ordering.
- `sl_fixed_pips = 20` (so initial SL is 20 pips from entry, wider
  than a pre-arm chandelier stop would want — this is the scenario
  that exercises the activation gate).
- `tp_fixed_pips = 60` (wide — chandelier will exit before TP in
  rows 1 / 5).
- No other management knobs active (breakeven off, trailing off,
  partial off) except where row specifies.

## Scenario table

| # | Scenario                         | Side | Entry    | Knob                                  | Bar OHLC trace                                                                                  | Expected action                                | Expected exit price | Expected PnL (pips) |
|---|----------------------------------|------|----------|---------------------------------------|-------------------------------------------------------------------------------------------------|------------------------------------------------|---------------------|---------------------|
| 1 | Plain-vanilla long pass          | long | 1.10000  | `enabled=1, activate=10, mult=3`      | B1 O/H/L/C = 1.10000/1.10020/1.09990/1.10015. B2 = 1.10015/1.10080/1.10010/1.10070. B3 = 1.10070/1.10070/1.10025/1.10030. | Arm at B2 (peak=1.10080, +15p PnL ≥ 10). B3 low 1.10025 ≤ chand_sl 1.10050 → stop fills | 1.10050             | **+50 pips**        |
| 2 | Knob disabled (sentinel)         | long | 1.10000  | `enabled=0, activate=-1, mult=-1`     | Same as #1                                                                                      | Chandelier block skipped. Trade runs to baseline SL (1.09980, -20) or TP. No chand fire. | initial SL 1.09980 if price drops next bar; in this OHLC TP 1.10060 hits in B2 high 1.10080 → exit at TP | **+60 pips (TP hit in B2)** |
| 3 | Edge: just inside activation     | long | 1.10000  | `enabled=1, activate=10, mult=3`      | B1 O/H/L/C = 1.10000/1.10010/1.09995/1.10005. B2 = 1.10005/1.10010/1.10000/1.10008. B3 = 1.10008/1.10105/1.10007/1.10100. | B1+B2 float peak only +10p at high but ends +8p — peak < activate until B3. B3 high 1.10105 → +10.5p ≥ 10 → arm. chand_sl = 1.10105 − 0.0030 = 1.10075. B3 low 1.10007 < 1.10075 BUT L came before H in long bias? Actually O→H→L→C: H first, so arm fires at B3 H, then L=1.10007 ≤ 1.10075 → stop fills same bar at 1.10075. | 1.10075             | **+75 pips**        |
| 4 | Edge: just outside activation    | long | 1.10000  | `enabled=1, activate=10, mult=3`      | B1 O/H/L/C = 1.10000/1.10008/1.09990/1.10005. B2 = 1.10005/1.10009/1.09995/1.10007. Entire trade stays below +10p float. | Never arms. Chandelier block updates peak but never writes SL. Trade exits on baseline SL or TP as if knob off. | initial SL 1.09980 if price drops; else time-out | **baseline (no chandelier fire)** |
| 5 | Short symmetry of #1             | short| 1.10000  | `enabled=1, activate=10, mult=3`      | B1 O/L/H/C = 1.10000/1.09980/1.10010/1.09985. B2 = 1.09985/1.09920/1.09990/1.09930. B3 = 1.09930/1.09930/1.09975/1.09970. | Arm at B2 (trough=1.09920, float +70p ≥ 10). B3 high 1.09975 ≥ chand_sl 1.09950 → stop fills. | 1.09950             | **+50 pips**        |
| 6 | Side-of-price guard (long)       | long | 1.10000  | `enabled=1, activate=10, mult=3`      | B1 O/H/L/C = 1.10000/1.10040/1.09990/1.10030. B2 = 1.10030/1.10030/1.10028/1.10029. Spike then hover. | B1 float at H is +40p ≥ 10 → arm same bar (intrabar). chand_sl = 1.10040 − 0.0030 = 1.10010. sb_low B1 = 1.09990 → 1.10010 > 1.09990 is FALSE (raw_sl < sb_low TRUE) → SL legitimately tightens to 1.10010. B2 low 1.10028 > 1.10010 → no fire. Peak unchanged B2, no update. Trade lives. Confirms guard **accepts** valid tightenings. | (trade still open) | (trade still open — phase 4 test checks sl state, not fill) |
| 7 | Sentinel coexisting with TP      | long | 1.10000  | `enabled=0, activate=-1, mult=-1`     | Bar mass: small wander then TP touch at B4.                                                     | Identical to a no-chandelier baseline in every metric slot (pnl, trades, hold bars). This is the strict no-op assertion. | same as baseline    | baseline PnL        |

## Per-row arithmetic

### Row 1 — plain vanilla long pass
```
activate_pips = 10, atr_mult = 3, atr_pips = 10
chand_dist = 3 * 10 * 0.0001 = 0.0030 price units

Bar 1: peak_high = max(entry_high, 1.10020) = 1.10020
       float_pnl at B1 H = (1.10020 - 1.10000)/0.0001 = +20p ≥ 10 → arm
       Actually arm happens inside B1 at H. chand_sl = 1.10020 - 0.0030 = 1.09990
       Side-of-price guard: raw_sl 1.09990 < sb_low B1 1.09990? Strictly-less-than → no, equal.
       → Do NOT adopt. (This is the guard's protective move.)

Bar 2: peak_high = max(1.10020, 1.10080) = 1.10080
       chand_sl = 1.10080 - 0.0030 = 1.10050
       sb_low B2 = 1.10010. raw_sl 1.10050 < 1.10010? NO (1.10050 > 1.10010).
       Wait — that means the raw SL is ABOVE the bar's low, which means
       the guard should REJECT. But then no ratchet happens, and
       standard chandelier says this SL is valid because peak moved up.

       Resolution: the side-of-price guard should check raw_sl < sb_CURRENT_price
       (or sb_low of the sub-bar's REMAINING path) — **not** the bar's
       high-of-bar. Re-examining the trailing v2 fix at trade_full.rs:221-232:
       it uses `if new_sl < current_bid { adopt }` in the O→H→L→C replay,
       meaning "the SL is below the price we just reached".

       Corrected: after B2 H=1.10080, the sub-bar current is H. raw_sl
       1.10050 < 1.10080 → adopt. sl becomes 1.10050.

       Then L=1.10010 in same sub-bar ≤ 1.10050 → stop fills at 1.10050.

PnL = (1.10050 - 1.10000)/0.0001 = +50 pips ✓
```

> **Correction captured.** The side-of-price guard is "raw_sl <
> current sub-bar price", not "raw_sl < bar's overall low". This
> matters for phase 4 code — re-check `trade_full.rs:221-232` to
> confirm the exact comparator. Row 6 below exercises this.

### Row 2 — knob disabled
```
enabled = 0 → Rust skips the whole chandelier block.
Baseline: SL 1.09980, TP 1.10060 (entry + 60p).
B2 high = 1.10080 ≥ TP 1.10060 → TP hits.
Exit 1.10060. PnL = +60p.
```

### Row 3 — just inside activation (crosses threshold at B3 H)
```
B1 H = 1.10010, float at H = +10p. ≥10? Strict ≥ → YES, arms at B1 H.
Actually rewrite: float at B1 H = (1.10010 - 1.10000)/0.0001 = +10.0p.
Threshold is `>= 10.0` → arms.
peak_high = 1.10010. chand_sl = 1.10010 - 0.0030 = 1.09980.
sub-bar current after H is 1.10010 → raw_sl 1.09980 < 1.10010 → adopt.
sl = max(sl_prev 1.09980, 1.09980) = 1.09980 (no change from baseline SL).

B1 L = 1.09995 > 1.09980 → no stop.

B2 H = 1.10010 → peak unchanged. chand_sl still 1.09980. No progression.

B3 H = 1.10105 → peak = 1.10105. chand_sl = 1.10105 - 0.0030 = 1.10075.
raw_sl 1.10075 < current sub-bar 1.10105 → adopt. sl = max(1.09980, 1.10075)
 = 1.10075.
B3 L = 1.10007 ≤ 1.10075 → stop fills 1.10075.

PnL = (1.10075 - 1.10000)/0.0001 = +75p ✓
```

### Row 4 — just outside activation
```
Max float across B1+B2:
  B1 H = 1.10008 → +8.0p. Not ≥ 10.
  B2 H = 1.10009 → +9.0p. Not ≥ 10.
Never arms. Chandelier block updates peak_high to 1.10009 but never
writes SL. Trade exits exactly as baseline (no chandelier effect).
```

### Row 5 — short mirror
```
direction = DIR_SELL
chand_dist = 0.0030 price units

B1 L = 1.09980 → trough = 1.09980. float = (1.10000 - 1.09980)/0.0001 = +20p ≥ 10 → arm.
chand_sl = 1.09980 + 0.0030 = 1.10010.
sub-bar current at L is 1.09980 → raw_sl 1.10010 > 1.09980 → adopt.
sl = min(sl_prev 1.10020, 1.10010) = 1.10010.

B2 L = 1.09920 → trough = 1.09920. chand_sl = 1.09920 + 0.0030 = 1.09950.
raw_sl 1.09950 > sub-bar current 1.09920 → adopt. sl = min(1.10010, 1.09950) = 1.09950.

B3 H = 1.09975 ≥ sl 1.09950 → stop fills 1.09950.

PnL = (1.10000 - 1.09950)/0.0001 = +50p ✓
```

### Row 6 — side-of-price guard accepts valid tightening
```
B1 H = 1.10040 → peak = 1.10040. float at H = +40p ≥ 10 → arm at H.
chand_sl = 1.10040 - 0.0030 = 1.10010.
sub-bar current = H = 1.10040. raw_sl 1.10010 < 1.10040 → adopt.
sl = max(initial_sl 1.09980, 1.10010) = 1.10010.

B1 L = 1.09990. sl 1.10010 > 1.09990 — wait, the ratchet just RAISED
sl above the current L? The guard inspected raw_sl at H; then in the
same sub-bar we evaluate L. The SL was set from the H-side; at L the
check is bid ≤ sl, i.e. 1.09990 ≤ 1.10010 → stop fires at L in same sub-bar?

RESOLUTION. Re-read primer §4: if the engine uses O→H→L→C sub-bar
replay, then once sl has been raised to 1.10010 at H-time, the L=1.09990
trips the stop. Exit 1.10010, PnL +10p.

This is the classic "trail fires instantly for an unearned win" bug
the v2 trailing fix specifically guards against by **only adopting
the tighter SL if raw_sl is also below the sub-bar LOW**, not just
the current sub-bar price.

**Phase 4 implementation must match the v2 trailing guard exactly:**

```rust
if direction == DIR_BUY {
    let raw_sl = chandelier_peak_high - chand_dist;
    if raw_sl < sb_low {               // below bar's low, not just current price
        sl = sl.max(raw_sl);
    }
}
```

Re-run row 6: raw_sl 1.10010 < sb_low 1.09990? NO (1.10010 > 1.09990)
→ do NOT adopt. sl stays 1.09980. L=1.09990 > 1.09980 → no fire.
Trade lives. ✓

B2 O/H/L/C = 1.10030/1.10030/1.10028/1.10029. peak = 1.10040 unchanged.
chand_sl 1.10010. sb_low B2 = 1.10028. raw_sl 1.10010 < 1.10028 → adopt.
sl = max(1.09980, 1.10010) = 1.10010. No fills in B2 (L 1.10028 > 1.10010).
Trade continues.

Expected at phase 4 micro-test: after B2 close, `trade.sl == 1.10010`,
`trade.status == OPEN`, `exit_code == EXIT_NONE`.
```

### Row 7 — strict no-op sentinel
```
All chandelier params = sentinel. Rust block skipped entirely.
Peak / trough / active flags never touched.
Every trade metric identical to same sweep with the Group removed
from schema (modulo NPZ column order).
```

## Acceptance

When phase 4 + 5 complete, every row above must reproduce in the
micro-test that validate-forex-knob writes during its phase 4. Any
row that does not reproduce is a phase 6 verdict (b) or (c).

## Implementation notes captured during hand-calc

1. **Guard comparator.** The side-of-price guard must be
   `raw_sl < sb_low` (for a long) — the same pattern as trailing v2.
   Using `raw_sl < current_price` instead re-introduces the unearned-win
   bug. Row 6 pins this.
2. **Arm-at-H vs arm-at-C.** If arming fires at the sub-bar H rather
   than bar close, the protection lag is zero and the guard is the
   only defence against unearned fills. Keep arm-at-H (matches
   trailing) but rely on the `< sb_low` guard.
3. **Ratchet direction.** `sl.max(raw_sl)` for long, `sl.min(raw_sl)`
   for short. Tests row 5 to confirm the sign.
4. **Sentinel precedence.** `enabled=0` short-circuits before any
   float read. `mult<=0` is the secondary safety net. Row 7 pins.
