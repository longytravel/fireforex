# The trailing stop had the same bug in four places

**Date:** 2026-04-19 (afternoon, second run of the skill)
**Engine version after fix:** `v2 trailing-fix`
**Authors:** You + Claude (claude-opus-4-7[1m]) + GPT-5.4 high via Codex.

## TL;DR

After the v1 breakeven-fix, the next question was whether the
trailing stop had the same shape of bug. It did — in four places
instead of two. One invocation of the `validate-forex-knob`
skill later, the six artifacts, a pytest guardrail, and a
ten-line Rust patch shipped.

## Prediction

The v1 write-up explicitly flagged the trailing block as the
probable next target: *"Uses a similar monotonicity-only guard
pattern at `trade_full.rs:195 – 266`. Might have the same bug,
might not. Next validation target."*

## The skill run, in one paragraph

Phase 1 (mechanics brief): I wrote a brief from first principles;
Codex wrote one blind. Both independently predicted that a
trailing SL with `distance < intrabar-range` would land past
current price and fire for +`distance` pips on the next sub-bar.
Phase 2 (code trace): I walked every hop, Codex walked the same
hops blind. Both landed at `core/src/trade_full.rs:222, 232, 255,
268` — four monotonicity-only guards, no side-of-price check at
any of them. Phase 3 (behaviour table): five scenarios — two safe
controls, three bug scenarios (tiny-distance fixed long, mirror
short, tiny-mult ATR long). Phase 4 (micro-test): all five
scenarios matched the live engine exactly pre-fix. Phase 5
(sensitivity): the bug configuration `(activate=5, distance=1)`
was inflating pips at sweep scale. Phase 6 (verdict): (c) broken,
fix is the same shape as v1 applied to four sites.

## The fix

Four guards in `core/src/trade_full.rs` — activation long / short
and ongoing long / short — each gained one condition:

```rust
// Long:  if new_sl > effective_sl && new_sl < sb_close { ... }
// Short: if new_sl < effective_sl && new_sl > sb_close { ... }
```

Rebuilt with `maturin develop --release`. `ff/VERSION.py` bumped
to `v2 trailing-fix`.

## Numbers

Sensitivity on the seeded 800-bar fixture (56 trades per config):

| Config                               | Trades | Wins | Win % | Total pips |
|--------------------------------------|--------|------|-------|------------|
| A — trail off                        | 56     | 17   | 30.4  | −206       |
| B — fixed, activate=20, distance=20  | 56     | 32   | 57.1  | **+379**   |
| C — fixed, activate=5, distance=1    | 56     | 17   | 30.4  | −206       |
| D — ATR, activate=5, mult=0.3        | 56     | 30   | 53.6  | −246       |
| E — ATR, activate=20, mult=2.0       | 56     | 24   | 42.9  | +56        |

The key row is **C**: pre-fix it would have matched the
breakeven-bug signature (same win cadence as a legitimate trail
but higher PnL per exit). Post-fix it is **bit-for-bit identical
to A (trail off)** — the guard rejects every single move in a
pathological configuration, exactly as intended. Legitimate
configurations (B, E) continue to produce real edge.

## Guardrails that landed

- `tests/validation/test_trailing_mechanics.py` — 5 / 5 green
  against the v2 engine. Rows 2, 3, 4 assert `0 pips` (bug
  rejected); rows 1, 5 assert the expected real PnL from the
  trail tracking correctly.
- Engine version visible on the web UI header. v2 is showing.

## What's still open

- **ATR mode, small `atr_mult × atr_pips`.** The fix kills the
  impossible-profit mechanism but not the "trail is tighter than
  a broker would ever allow" regime. Sensitivity config D shows a
  legitimate-but-too-aggressive ATR trail. A minimum-stop-distance
  check would suppress that. Not urgent.
- **Short SL trigger still uses raw `sb_high`, not ask.** Flagged
  in both breakeven and trailing traces. Own validation run
  material.
- **Long SL exits do not deduct spread.** Standing asymmetry.
  Same status.

## What we learned

1. **The skill paid for itself on the second run.** Phase 1
   prediction matched Phase 5 result. We shipped a second fix
   inside a session because the process makes the fix obvious —
   brief + trace + behaviour table = the patch writes itself.
2. **One bug shape rarely lives in one place.** The breakeven bug
   lived at two sites; the trailing bug lived at four; both had
   the same pattern. Next time a monotonicity-only guard shows up
   anywhere in the engine, we look for the sibling guards before
   shipping.
3. **The Chandelier stop decision is simpler now.** If Chandelier
   is implemented as another mode inside the existing trailing
   block, it inherits the four guards for free. If it is a
   separate block, it needs its own side-of-price check from day
   one — and we know exactly what that check looks like.

## Where the artifacts live

- `docs/validation/2026-04-19-trailing/` — six numbered
  artifacts + sensitivity runner + verdict.
- `tests/validation/test_trailing_mechanics.py` — live guardrail.
- `core/src/trade_full.rs:222,232,255,268` — the four patched
  guard sites.
- `ff/VERSION.py` — v2 trailing-fix.
