# 01 — Mechanics brief: `chandelier_stop`

> One-page brief written **before** any code. Cites standard retail
> practice (Chuck LeBeau, *New Concepts in Technical Trading Systems*
> lineage, and the common "Chandelier Exit" used by TradingView and
> MT5/MQL5 libraries). Where Fire Forex conventions diverge, flagged
> **[FF]**.

## Definition

A **chandelier stop** is a volatility-based trailing stop anchored to
the **highest high since entry** (for a long) or the **lowest low
since entry** (for a short). The stop hangs down from that extreme by
a multiple of Average True Range (ATR), like a chandelier hangs from
the ceiling. It ratchets one-way: once the stop has moved in favour
of the trade it never loosens, even if price pulls back and the
extreme does not refresh.

It is **not** the same as a distance-from-current-price ATR trail
(what Fire Forex currently calls `TRAIL_ATR_CHANDELIER` —
`sb_high - trail_atr_mult*atr` uses the *current bar's* high, not
the highest high since entry). That existing knob is misnamed; see
"Interaction with existing engine" below.

## Math

For a long opened at `actual_entry`, tracked across bars
`0 .. bars_since_entry`:

```
peak_high            = max(high[entry_bar .. current_bar])          # price units
raw_chandelier_long  = peak_high - atr_mult * atr                   # price units
chandelier_sl_long   = max(chandelier_sl_long_prev, raw_chandelier) # ratchet, price units
```

For a short:

```
trough_low            = min(low[entry_bar .. current_bar])           # price units
raw_chandelier_short  = trough_low + atr_mult * atr                  # price units
chandelier_sl_short   = min(chandelier_sl_short_prev, raw_chandelier)# ratchet, price units
```

Where `atr` is the ATR-in-price-units the engine uses for the
current bar (Fire Forex already tracks `atr_pips * pip_value` in
`trade_full.rs`; reuse that value).

The stop only *replaces* the working SL when it is more protective
than the current SL (higher for a long, lower for a short). This is
the ratchet.

## Units & typical range

| Aspect            | Value                                               |
|-------------------|-----------------------------------------------------|
| Knob unit         | `atr_mult` is a pure float multiplier (dimensionless)|
| Typical retail    | `atr_mult ∈ [2.0, 4.0]`; LeBeau's original was 3.0  |
| Pair sensitivity  | Self-scaling: ATR already carries pair volatility.  |
|                   | No per-pair override needed.                        |
| Activation        | Two variants in practice:                           |
|                   |   (a) Active from bar 0 (classic LeBeau).           |
|                   |   (b) Armed only after `activate_pips` float-PnL    |
|                   |       (Fire Forex breakeven/trail convention).      |
|                   | Recommend **(b)** for Fire Forex to match the       |
|                   | existing `trail_activate_pips` idiom and avoid      |
|                   | firing a tighter-than-SL stop on first bar.         |

## Spread interaction

- **Long side.** The chandelier SL is evaluated against **bid**
  (SL hit when `bid ≤ sl`). `peak_high` itself is the sub-bar high,
  which in Fire Forex's single-series engine is bid-side high.
  Asymmetry: entry was at ask, exit at bid — the first pip of peak
  is already eaten by spread. No separate adjustment needed *inside*
  the chandelier formula; it is inherited from the SL fill path.

- **Short side.** The chandelier SL is evaluated against **ask**
  (SL hit when `ask ≥ sl`). `trough_low` is bid-side low in a
  single-series engine; add `spread` to get the ask-low, but the
  existing SL fill path already accounts for this — do not
  double-count.

**[FF]** Fire Forex uses a single-series OHLC stream with a modeled
spread cost on fills, not a two-book simulation. The chandelier
implementation reads the same `sb_high` / `sb_low` as the existing
trailing logic, so spread semantics are inherited automatically.

## Slippage interaction

Same as any other SL-moving knob: when `bid` gaps through the
chandelier SL, Fire Forex fills at the SL price (exact-stop
convention used by the trailing code at `trade_full.rs:339-367`).
No chandelier-specific slippage model required.

## Long / short asymmetry

| Quantity              | Long                              | Short                              |
|-----------------------|-----------------------------------|------------------------------------|
| Anchor                | `peak_high = max(high)`           | `trough_low = min(low)`            |
| Raw stop              | `peak_high - atr_mult*atr`        | `trough_low + atr_mult*atr`        |
| Ratchet direction     | `sl = max(sl_prev, raw)`          | `sl = min(sl_prev, raw)`           |
| Fill condition        | `bid ≤ sl`                        | `ask ≥ sl`                         |
| Activation PnL        | `(bid - entry)/pip_value ≥ arm`   | `(entry - ask)/pip_value ≥ arm`    |

**[FF]** Every sign flip must appear in `trade_full.rs` both inside
and outside the activation guard. The trailing-stop validation found
side-of-price bugs at four sites — chandelier must not repeat that.

## Edge cases

- **Raw stop above current price on a long (no spike yet).**
  Before any new high prints, `peak_high = entry_high`. If
  `atr_mult*atr < (entry_high - entry_price)` the raw chandelier
  could land above entry on bar 0. **Rule:** use side-of-price
  guard — only adopt the chandelier SL if it is strictly below
  current `sb_low` for a long (strictly above `sb_high` for a
  short). Mirror the trailing stop's v2 guards
  (`trade_full.rs:221-232, 256-265`). This prevents the "fire
  instantly for an unearned +dist win" bug.

- **Gap through chandelier SL on first bar armed.** Same as
  trailing: fill at the chandelier SL price, exit-code
  `EXIT_CHANDELIER` (new — or reuse `EXIT_TRAILING` if we choose
  to fold chandelier into the trailing family; decision deferred
  to phase 2).

- **Knob disabled (gate off).** Sentinel `-1` in the `atr_mult`
  slot. Rust side reads the sentinel as "off" and skips the whole
  chandelier block. Float-PnL, peak tracking, SL — all untouched.
  Trade runs identically to a no-chandelier baseline. (This is the
  defect class the signal-filter validation found: sentinel `0`
  is too permissive; `-1` is the right pick.)

- **Coexistence with trailing stop / breakeven.** User may enable
  both. Resolution: each knob's block writes `new_sl` independently
  and the engine picks the *tightest* (most protective) stop —
  `max(sl_breakeven, sl_trailing, sl_chandelier)` for a long,
  `min(...)` for a short. Already how breakeven+trailing compose
  in the existing engine; chandelier slots into the same pattern.

- **Pair-specific quirk — JPY.** ATR on USDJPY is in JPY price
  units (2 decimals). `atr_mult * atr` is automatically correct
  because the whole calc stays in price units. No pip conversion
  needed inside the chandelier formula. (This is the unit trap
  from primer §8 — chandelier avoids it by working in price space,
  not pip space.)

## Interaction with existing engine

**Important.** `core/src/trade_full.rs:212` has a constant called
`TRAIL_ATR_CHANDELIER` whose arithmetic is `sb_high - trail_atr_mult
* atr`. That is **not** a chandelier — it is an ATR-scaled
*distance-from-current-high* trail. Classic chandelier anchors to
the *highest high since entry*, which requires per-trial peak
tracking the current code does not do.

Two ways to resolve (decided in phase 2, slot map):

1. **Rename existing + add new.** Rename
   `TRAIL_ATR_CHANDELIER → TRAIL_ATR_DISTANCE` and add a genuine
   `CHANDELIER_STOP` group with peak tracking. Cleanest semantically,
   biggest diff.
2. **Add new, leave old.** Introduce a new `chandelier_stop` Group
   (independent of trailing, stackable) with its own activation
   flag, peak tracker, ATR mult, and SL-writer. Old
   `TRAIL_ATR_CHANDELIER` stays misnamed but functional.

Recommend **(2)** — smaller blast radius, keeps trailing semantics
frozen (and already validated in v2 trailing fix), treats chandelier
as a peer management rule. Open to user override at phase 2.

## Recommended default range

```python
chandelier_stop = Group(
    name="chandelier_stop",
    when_on=Branch(
        activate_pips = FloatRange(5.0, 25.0, step=1.0),   # arm once this much in profit
        atr_mult      = FloatRange(2.0, 4.0,  step=0.25),  # LeBeau 3.0 is the midpoint
    ),
)
```

- `activate_pips` mirrors `trail_activate_pips` to reuse the tested
  arming pattern.
- `atr_mult` defaulted to the 2.0 – 4.0 band from retail practice.
  Tighter than 2 is a de-facto breakeven stop; looser than 4 rarely
  differs from no-trail.

## Codex independent brief

*(Codex `gpt-5.4 high`, 2026-04-19, verbatim)*

The retail-forex meaning is: `chandelier_stop` is an ATR-volatility
trailing stop, not a fixed pip trail. For a long, the candidate stop is:

`highest_high_since_entry - atr_mult * ATR`

For a short:

`lowest_low_since_entry + atr_mult * ATR`

This matches the Chandelier Exit attributed to Chuck LeBeau and described
by TradingView as highest price minus ATR multiple for longs, lowest
price plus ATR multiple for shorts. The classic/default reference is
often 22 periods and 3 ATR, though "since entry" anchoring is a
trade-management variant rather than a rolling 22-bar indicator variant.

Units: `atr_mult` is dimensionless, a multiple of ATR, not pips or
percent. ATR itself is in price units, so on EURUSD an ATR of `0.0010`
is 10 pips. `activate_pips` is in pips of floating profit. ATR length
and any high/low lookback are bar counts. The resulting SL is a price
level.

Bid/ask matters. A long is opened at Ask and closed by selling at Bid,
so a long SL should be triggered/evaluated against Bid. A short is
opened at Bid and closed by buying at Ask, so a short SL should be
triggered/evaluated against Ask. MQL5's algo book explicitly notes
protective levels close via the opposite side: buy positions use Bid,
sell positions use Ask. If the chart data is Bid-only, short-stop
testing is optimistic unless Ask or spread is modeled.

Slippage: the chandelier value is only the stop trigger level, not a
guaranteed fill. Once triggered, the broker closes at the available
executable price. Gaps, fast ticks, news, thin liquidity, and stop
cascades can produce worse fills. Treat backtest fills at exact SL as
optimistic unless slippage is simulated.

Long/short asymmetry is just the sign and price side. Long: anchor
rises with highest high, subtract ATR multiple, ratchet upward only.
Short: anchor falls with lowest low, add ATR multiple, ratchet downward
only. Trigger inequality flips: long exits when Bid <= SL; short exits
when Ask >= SL.

Edge cases: if the computed long SL is above current Bid, or short SL
below current Ask, it is no longer a valid protective stop.
Mechanically, the stop has already been crossed; the correct behavior
is immediate exit at market or next modeled tick, not placing an
invalid stop. If broker minimum stop distance prevents placement,
either keep an internal stop or use the nearest valid broker-side
level only as execution plumbing, without changing strategy semantics.

With coexisting breakeven or fixed-pip trailing stops, use the most
protective valid stop: for longs, the highest SL; for shorts, the
lowest SL. All stops must still ratchet only in favor of the trade.

For EURUSD H1, reasonable defaults: `atr_mult` 2.5-3.5, with 3.0 as
the classic baseline; `activate_pips` about 10-25 pips, or roughly
1.0-2.0x recent H1 ATR. More aggressive: 1.8-2.5 ATR and 5-15 pips.
More patient trend-following: 3.5-4.5 ATR and 20-40 pips.

## Diff: my brief vs Codex

**Agreements (high confidence):**

- Formula: `peak_high - atr_mult*ATR` long, `trough_low + atr_mult*ATR`
  short. Ratchet one-way.
- `atr_mult` dimensionless; ATR carries price units so the formula
  stays in price space automatically (no pip conversion inside).
- Bid/ask side of SL: long checked against bid, short against ask.
- Slippage: SL is a trigger, not a fill guarantee. Backtest-at-SL is
  optimistic in reality.
- Long/short sign flips everywhere (anchor, add-vs-subtract, trigger
  inequality).
- Most-protective-SL-wins when stacked with breakeven / fixed trail.
- LeBeau 3.0 is the classic midpoint; 2.0 – 4.0 is the retail band.

**Disagreements (resolved before phase 3):**

1. **Edge case: chandelier SL computed above current bid for a long.**
   - *My brief:* use a **side-of-price guard** — refuse to adopt the
     new SL unless it is strictly below `sb_low` for a long (above
     `sb_high` for a short). Matches the v2 trailing fix already in
     `trade_full.rs:221-232,256-265`.
   - *Codex:* "the stop has already been crossed; the correct
     behaviour is immediate exit at market." I.e. primer §4 behaviour
     (2) — accept the move and fire instantly.
   - **Resolution:** adopt my position (side-of-price guard). Reasons:
     (a) Fire Forex's v2 trailing fix explicitly chose guard-not-fire
     after the 2026-04-19 unearned-trail-win incident;
     (b) consistency across the trailing / breakeven / chandelier
     family makes the validation story uniform;
     (c) Codex's "fire immediately" is the more standard retail
     interpretation, but it reintroduces the exact bug pattern the
     trailing validation just fixed.
     Flag this explicitly in phase 3 scenario (3b) so the micro-test
     pins the chosen behaviour.

2. **Default band for `atr_mult`.**
   - *My brief:* `[2.0, 4.0]`, step 0.25 — wider band for optimiser.
   - *Codex:* `[2.5, 3.5]` as the sweet spot, with broader variants
     for aggressive / patient modes.
   - **Resolution:** keep `[2.0, 4.0]`. The optimiser's job is to
     explore; a tight prior would preempt that. Narrower band is a
     post-facto finding for future session, not a default.

3. **ATR period.** Codex calls out 22-period ATR as the classic
   reference. Fire Forex already tracks `atr_pips` in
   `trade_full.rs`; reuse that (engine-default period, currently
   14). This is a non-issue so long as we reuse the existing
   `atr_pips` variable — no new lookback to configure.

**Net:** one load-bearing disagreement (edge-case behaviour on
side-of-price). Locked to guard-not-fire. Pinned in phase 3 scenario
(3b).
