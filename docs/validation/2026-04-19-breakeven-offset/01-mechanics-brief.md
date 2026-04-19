# 01 — Mechanics brief: breakeven.offset

Phase 1 of validate-forex-knob, run 2026-04-19. See
`validate-forex-knob/references/forex-mechanics-primer.md` for shared
ground truth.

## The knob

- **Path:** `breakeven.when_on.offset` (commonly `breakeven.offset`)
- **Claimed semantics:** When a trade's floating profit reaches
  `trigger` pips, move the stop-loss to `entry + offset_pips` for a
  long, or `entry - offset_pips` for a short. A positive offset is
  intended to lock a small profit in the direction of the trade.
- **Companion knob:** `breakeven.when_on.trigger` — the pip-distance
  above entry (long) or below entry (short) at which the move fires.
- **Units:** pips.
- **Typical retail range:** 0 – 10 pips. The conventional default is
  `offset = 0` (lock the exit at entry price, i.e. "true" breakeven).
  Small positives (1 – 3) lock a few pips of profit. Values greater
  than the trigger are unusual — they imply the locked profit exceeds
  the amount that has been moved, which is only physically consistent
  in the presence of spread and intra-bar motion (see edge case below).

## What it means in standard forex practice

A **break-even stop** is a money-management pattern in which, once a
position is "in the money" by at least some threshold, the trader
moves the stop-loss to near the entry price. The effect is to turn
any remaining adverse movement into at-worst-flat (or a small win).
The `offset` parameter controls where the new stop sits relative to
entry:

- `offset = 0` → SL at entry exactly. If price retraces fully, exit is
  0 gross (a small net loss after spread + commission).
- `offset > 0` → SL above entry (for a long) / below entry (for a
  short). A retrace still exits in profit. This is the "lock in a
  few pips" pattern.
- `offset < 0` (if supported) → SL further from entry than the
  original distance, giving the trade room. Unusual in retail
  platforms; normally BE only tightens the stop, never loosens it.

The move is one-shot per trade. Once BE has fired, the trade has a
new SL and the BE logic stands down.

## Units and ranges

- Pip-denominated. 1 pip = 0.0001 for most pairs; 0.01 for JPY
  quotes. The engine's `pip_value` constant must be pair-aware or
  every pip-scaled knob silently scales wrong for JPY pairs.
- Typical bounds enforced by the schema should be non-negative and
  less than any sensible take-profit distance. If the schema permits
  `offset > trigger`, the knob can be rolled into physically odd
  configurations (see edge case).

## Interaction with spread

SL fills hit the **opposite side** of the book from entry:

- Long trade entered at the ask. SL triggers when **bid ≤ SL**.
- Short trade entered at the bid. SL triggers when **ask ≥ SL**.

If the engine models a two-sided book, then at the moment the BE
move is evaluated:

- For a long, `float_pnl_pips = (bid - entry) / pip_value`. BE
  fires when `float_pnl_pips ≥ trigger`. The new SL is placed at
  `entry + offset_pips * pip_value`.
- For a short, `float_pnl_pips = (entry - ask) / pip_value`. BE fires
  when `float_pnl_pips ≥ trigger`. The new SL is placed at `entry -
  offset_pips * pip_value`.

Because the SL fires on the bid (long) or ask (short), wide spreads
delay when a newly-tightened SL actually triggers. For tight majors
in liquid hours, the delay is ~0.5 pips; for exotics or news, it can
exceed the offset entirely, converting an intended "+2 pip lock"
into an effective break-even or small loss after spread.

## Interaction with slippage

On a bar-based engine, SL slippage is a modelling choice:

- **Exact-stop (optimistic):** SL fills at the SL price. Simplest.
- **Next-bar-open (pessimistic):** SL fills at the open of the next
  bar; large gaps hurt.
- **Worst-of-open-and-stop:** SL fills at `min(open, stop)` for a
  long (long SL is below price; worst = lower). Closer to realistic
  retail.

Live retail fills are closer to the third in fast markets, the first
in placid ones. "Exact-stop" is the default simplification in most
backtest literature (Pardo, *The Evaluation and Optimization of
Trading Strategies*, ch. 7) but is known to inflate expectancy on
strategies with tight SL / BE logic.

## Long vs short asymmetry

Symmetric in mathematical form, with a sign flip:

| Direction | `float_pnl_pips`              | New SL after BE                        |
|-----------|-------------------------------|----------------------------------------|
| Long      | `(bid − entry) / pip_value`   | `entry + offset_pips * pip_value`      |
| Short     | `(entry − ask) / pip_value`   | `entry − offset_pips * pip_value`      |

Sign-flip bugs in engines typically appear when the author copies
the long formula and forgets to invert `+` to `−` for short, or vice
versa. Any trace of a BE implementation must check both branches.

## Edge case — SL above current price for a long (the 78 % question)

The case that motivates this skill. Take:

- Pair: EURUSD, `pip_value = 0.0001`.
- Entry: 1.10000 long.
- Trigger: 5 pips. Offset: 10 pips.

Trade progresses until bid = 1.10005 (float PnL = +5 pips). BE fires.
Under the claimed semantics:

- New SL = `entry + offset * pip_value = 1.10000 + 10 * 0.0001 =
  1.10010`.
- Bid at that moment is 1.10005; ask is ~1.10007 (for a ~2-pip
  spread).
- The new SL is **above** the current bid.

Three behaviours the engine might exhibit:

1. **Reject the move.** Engine refuses to write an SL that would fire
   immediately; old SL stays in place. The trade continues and
   resolves by normal TP / trailing / time rules.
2. **Accept, and fire on the same bar.** Engine writes 1.10010 to the
   SL slot. The intrabar check then trips (`bid ≤ SL`) and the trade
   exits at 1.10010, banking **+10 pips**. This is "free money" from
   the engine's perspective and looks like a very high-win-rate
   strategy.
3. **Accept, and silently clamp** to `max(new_SL, current_bid)` for a
   long. The SL lands at ~1.10005, effectively a tight trail. The
   trade exits at +5 pips on any retrace.

Behaviours (2) and (3) both produce near-100 % win-rates on this
configuration. Behaviour (1) produces outcomes dictated by the
underlying entry / exit rules, typically with a much lower win-rate.

**The 78 % win-rate reported on 2026-04-19 under `trigger=5, offset=10`
is consistent with (2) or (3), not with (1).** Hence the skill's
conclusion depends on which behaviour the code implements — a Phase 2
question.

A physically realistic retail broker does **not** do (2) — most
platforms either reject the move or clamp it. The `exact-stop` fill
model in backtests is what makes (2) possible and is exactly the
kind of silent bug the micro-test (Phase 4) is designed to detect.

## My recommendation for expected behaviour

A correct engine should do (1) — reject any BE move that would place
the SL on the wrong side of current price. If the engine does (2),
then either the knob's name is misleading (offset is *not* really
measured from entry; it is effectively an "immediate exit if the
trade is in profit by trigger pips") or the logic is buggy. Either
way the 78 % win-rate reported in the sweep is partially fake — it
is not from a strategy that made 78 % winning trades, it is from
the engine's fill semantics.

Phase 2 will tell us which of the three is in the Rust code. My
prior is behaviour (2), because `trade_full.rs:176` reads
`actual_entry + breakeven_offset_pips * pip_value` with no guard
clause on the relationship to current price that I could see in the
`grep` results.

## Open questions for Codex

- Does Codex reach the same three behaviours, or does it propose a
  fourth?
- Does Codex's forex-mechanics knowledge say anything about how
  MT4 / MT5 handle this edge case in practice?
- Does Codex judge (2) to be a bug, a feature, or a semantic
  mismatch?

---

## Codex independent brief

*Verbatim from Codex (gpt-5.4, reasoning-effort high, read-only
sandbox, no project files visible). Run 2026-04-19.*

> **Mechanics Brief**
>
> `breakeven.offset` is a stop-management rule. After floating profit
> reaches a trigger, the engine modifies the protective stop from its
> original level to near entry. For a long, new SL = `entry + offset`;
> for a short, new SL = `entry - offset`. A positive offset means the
> stop is moved past entry in the profitable direction, so if it is
> later hit, the trade should close with a small gross profit before
> spread, slippage, commission, or swap.
>
> The unit should be pips, not bars, price fractions, or percentages.
> In spot FX, one pip is usually `0.0001` for most pairs and `0.01`
> for JPY pairs; many retail platforms quote fractional pips as
> "points" or "pipettes." Typical retail breakeven offsets are small:
> `0` to about `5` pips is common, sometimes `10+` on wider-spread or
> higher-volatility symbols. Larger offsets are closer to a
> trailing-stop behavior than ordinary breakeven. References:
> MetaTrader order mechanics distinguish Bid/Ask execution sides;
> BabyPips and broker education docs commonly define pip and spread
> conventions.
>
> Spread matters. A long position is closed by selling, so its SL
> fires against the **Bid**: long SL triggers when `Bid <= stop`. A
> short position is closed by buying, so its SL fires against the
> **Ask**: short SL triggers when `Ask >= stop`. This is a common
> source of backtest error because many candle feeds are bid-only. If
> only bid OHLC is available, a realistic short-stop test needs an
> ask approximation using spread. When spread widens, a short SL can
> be hit even if the bid chart never visibly touches the stop. For
> longs, the bid chart directly represents the stop trigger side, but
> spread still affected the entry and therefore the true P/L.
>
> SL fills should not be modeled as guaranteed exact-stop fills
> unless the system is explicitly assuming idealized execution. A
> stop-loss is normally a stop market order: once triggered, it
> becomes a market order and can slip. "Exact-stop" is optimistic.
> "Next-bar-open" is crude and can be either optimistic or
> pessimistic depending on gap path assumptions. For OHLC backtests,
> a conservative common model is fill at the worse of the stop and
> the next available/open price after a gap: long SL fill `min(stop,
> open)` if gapped through; short SL fill `max(stop, open)`. Intrabar
> sequencing remains disputed unless tick data is available.
>
> Long/short formulas must mirror each other. Long trigger uses
> favorable movement from entry to Bid or trade valuation price;
> short trigger uses entry to Ask or equivalent close price. New long
> SL: `entry + offset_pips`. New short SL: `entry - offset_pips`. Do
> not reuse the same sign for shorts.
>
> Critical edge case: if `trigger=5` and `offset=10`, then at the
> instant a long has only +5 pips floating profit, setting SL to
> `entry + 10` puts the stop **above current Bid**. A correct engine
> should reject or defer that modification because a sell stop above
> current Bid is not a valid protective stop; it is effectively
> marketable or nonsensical as an SL. Broker-like behavior would
> require the SL to be below current Bid by at least the minimum
> stop distance. Correct choices are: do not move yet, clamp to a
> valid level only if the strategy explicitly allows clamping, or
> treat the order modification as rejected.
>
> Common incorrect engines blindly assign the SL above market, then
> later mark it as an immediate profitable exit, creating impossible
> profits. Others compare stops to mid or close prices and miss the
> invalidity. The clean rule: breakeven offset must be executable on
> the correct side of the book at modification time.

---

## Diff — where my brief and Codex agree / disagree

### Agreements (high confidence — these are the load-bearing facts)

- **Unit is pips** (not bars, fractions, or percentages). Pip
  conventions: 0.0001 for most, 0.01 for JPY.
- **Long / short formulas**: `entry + offset_pips` for long,
  `entry − offset_pips` for short. Sign flip required.
- **Which side of the book the SL fires on**: bid for long
  (`bid ≤ stop`), ask for short (`ask ≥ stop`). Not mid. Not close.
- **Exact-stop fill is optimistic**; realistic is worst-of-stop-and-
  gap-open: long SL fills at `min(stop, open)` after a gap-through,
  short SL fills at `max(stop, open)`.
- **The 78 %-win-rate edge case is an engine bug pattern, not a
  strategy win.** Both briefs independently arrive at: a long SL
  written above current bid should be rejected or deferred. An engine
  that accepts it and exits on the same bar is "creating impossible
  profits" (Codex's phrase). My behaviour (2) = Codex's "common
  incorrect engines blindly assign the SL above market".

### Agreements that strengthen the investigation

- Both briefs name (2) — accept-and-immediately-fire — as the classic
  incorrect engine behaviour.
- Both briefs say a *correct* engine should prefer (1) — reject or
  defer the move when it would place the SL on the wrong side of
  current price.
- Both briefs agree the reported 78 % win-rate is consistent with the
  incorrect behaviour, not with a genuinely winning strategy.

### Disagreements / nuances

- **Typical retail offset range.** I said "0 – 10, usually 0". Codex
  says "0 to about 5, sometimes 10+ on wider-spread or higher-vol
  symbols." Codex's range is slightly tighter. Not load-bearing.
- **Stop-distance minimum.** Codex adds: "broker-like behavior would
  require the SL to be below current Bid by at least the minimum
  stop distance." I did not mention this. If `ff_core` has no
  minimum-stop-distance concept, the difference is academic for
  validation; if it does, row 2 of the Phase 3 table needs a
  specific scenario for it.
- **Bid-only candle feed risk.** Codex flags this explicitly —
  backtests that use bid-only OHLC and do not approximate ask can be
  silently wrong on short-side stops. I did not raise this. **Phase
  2 action:** verify whether `ff_core.batch_evaluate` accepts a
  two-sided book or a single-series OHLC. If single-series, short-SL
  arithmetic is suspect even before we look at breakeven.

### Load-bearing for Phase 2 (the code trace)

Both briefs converge on three questions the code trace must answer:

1. Does `trade_full.rs` guard the new SL against the current price
   side-of-book?
2. Does the engine model two-sided spread or a single OHLC series?
3. When the engine writes an SL above current price for a long, does
   the subsequent bar/tick check fire immediately (behaviour 2),
   clamp (behaviour 3), or reject retroactively (behaviour 1)?

### Open questions to raise with the user

- If Phase 2 finds behaviour (2), do we want to *fix* the engine
  (reject invalid SL writes) or *rename* the knob (accept that it
  behaves as "exit-with-profit" rather than "move SL")? The former
  is more principled; the latter is less invasive. The `trigger=5,
  offset=10` trial would produce very different results under a fix.
- If the engine uses single-series OHLC (no spread), the BE trigger
  condition `float_pnl ≥ trigger` fires earlier than it would in
  production. Worth quantifying.
