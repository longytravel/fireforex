# Phase 1 — Stale-exit mechanics brief

**Knob family:** `stale` (a `Group` with `test` on/off, plus `when_on.bars` and `when_on.atr_thresh`).
**Rust parameter slots:** `PL_STALE_ENABLED`, `PL_STALE_BARS`, `PL_STALE_ATR_THRESH`.
**Rust call-site:** `core/src/trade_full.rs:113-136` (H1-level, pre-intrabar block).

## Definition in plain English

A **stale exit** is a time-and-volatility cut-out. It closes a trade that has been held at least `stale_bars` H1 bars *and* has failed to show any single bar with a high-low range greater than `stale_atr_thresh * ATR_at_entry` pips across a rolling window of the last `stale_bars` bars. In standard retail-forex language: "if the market has gone quiet for the last N bars, stop waiting — close the position at the next bar's close and move on."

The intuition is that a trade that has not moved *either direction* is unlikely to reach TP, and continuing to hold it ties up margin and (in live trading) swap/rollover. Unlike a `max_bars` cut-out, which fires purely on time, stale checks *volatility* so that a trade still experiencing movement is allowed to run.

## Units

| Field | Unit | Typical retail range |
|-------|------|----------------------|
| `stale_bars` | integer count of H1 bars (schema range 20–200) | 20–100 for day-session strategies; 100–300 for swing |
| `stale_atr_thresh` | dimensionless multiplier of ATR-at-entry (schema range 0.3–2.5) | 0.5–1.5 is typical |
| Internal `max_range` | pips (already scaled by `pip_value`) | varies with pair volatility |
| `atr_pips` | pips | 5–30 for majors on H1 |

The test condition is therefore: *"no single bar in the lookback window had a high-low range greater than K × ATR-at-entry, where K is the user-chosen threshold"*. The threshold is **a range multiplier, not an absolute pip count**. Users setting `atr_thresh=100.0` are saying "exit if no bar in the window had range exceeding 100 × entry-ATR", which is trivially always true → the knob effectively becomes a time-exit at `stale_bars`. That is how the sensitivity test detects the feature (test_knob_sensitivity.py:200).

## Interaction with spread

In real forex, a long exits at the bid and a short at the ask. The stale logic computes the exit PnL from `bar_close` (an OHLC value — typically mid or last, not bid/ask). Slippage is subtracted from the exit side (long: `close - slippage_price`; short: `(entry - close - slippage_price)`), **but spread at exit time is not re-applied**. Entry spread is already baked into `actual_entry` upstream.

**Consequence:** a stale-triggered exit is priced on a single side of the book (close minus slippage), so it will systematically show slightly better exits than would occur at a real retail broker, where the long would hit `bid = close − spread/2`. This is not a stale-specific bug — it's a systemic engine choice — but it affects the *measured* performance of the knob.

## Interaction with slippage

Slippage is applied symmetrically via `slippage_price` (a price-scaled quantity). Formula in code (`trade_full.rs:101-107` for max-bars, mirrored at `:126-133` for stale):

```
pnl_long  = (bar_close - slippage_price - actual_entry) / pip_value * position_pct
pnl_short = (actual_entry - bar_close - slippage_price) / pip_value * position_pct
```

Both long and short pay the slippage — the sign convention means long exits lower than close, short exits "higher-than-close" effectively (which when shorted is worse). This is standard and correct.

## Long vs short asymmetry

**None.** The stale condition inspects only `bars_held`, `max_range` over the lookback, `stale_atr_thresh`, and `atr_pips`. Direction does not enter the test. That matches real-world convention — a sideways market is sideways for both longs and shorts. The *PnL* calculation branches on direction, but the *trigger* is symmetric.

## Edge cases and subtleties worth flagging

1. **Lookback clamp.** `lookback_start = max(entry_bar + 1, bar - stale_bars + 1)`. So on the first bar where `bars_held >= stale_bars`, the lookback window is exactly `stale_bars` bars long — but those bars start at `entry_bar + 1`, i.e. the trade's own entry bar is excluded. This is likely intentional (the entry bar's range is partly pre-entry) but means `stale_bars=2` inspects bars 2 and 3 of the trade, not bars 1 and 2.

2. **Max-range, not mean-range.** The trigger uses `max(H_i - L_i)` across the lookback, not average. A single spiky bar in an otherwise quiet stretch blocks the exit. This matches the "any meaningful activity resets the stall clock" intuition and is correct for the intended semantics.

3. **ATR snapshot frozen at entry.** `atr_pips` is captured at signal time and never updated. A trade entered during a volatile open will carry a high threshold for its entire life — even if volatility collapses after 20 bars, the stall test is still judged against the high ATR. In principle a user might expect a *rolling* ATR. The current behaviour is defensible (preserves cross-trade comparability) but worth documenting.

4. **Exit-price optimism.** `bar_close` is used, not "next-bar-open" or "next-bar-VWAP". A bar can close near its high after a late-session rally — stale would fire on this bar even though the inside activity was large, producing a mildly favourable exit. Not a bug, but a mild look-ahead within the bar.

5. **Fires at H1 bar close only.** Intrabar stalls are never detected — the sub-bar management loop (`sub_start..sub_end`) runs *after* the stale check. If the H1 bar shows a wide range but closes flat, stale won't fire that bar; it will only fire on the subsequent flat bar. This matches the `max_bars` resolution and is called out at `trade_full.rs:17`.

6. **Exit reason and accounting.** `EXIT_STALE` is a distinct reason code, and `final_pnl = realized_pnl_pips + pnl` correctly folds in any partial-close realization. Partial-close followed by stale will produce a PnL that is *partial realization + residual stale close*. Test this in Phase 3.

7. **What happens when all three knobs fire on the same bar?** The order in `trade_full.rs` is `max_bars` → `stale` → sub-bar management (trailing/BE/partial/SL/TP). So if bars_held >= max_bars *and* bars_held >= stale_bars *and* max_range < thresh, `max_bars` wins and `EXIT_MAX_BARS` is recorded. Priority is deterministic. Worth asserting.

## What a "correct" stale exit looks like (working-vs-broken signature)

**Expected direction of effect when toggled on:** fewer trades reach TP, average holding time drops, trade count per period may rise (because capital recycles faster), and — for a losing baseline — expectancy should **improve** (stale cuts wasted time in non-movers). For a winning baseline, expectancy may **drop** slightly (stale cuts trades that would have eventually hit TP). Either direction proves the knob is *alive*. A byte-for-byte metric match between on/off (the 2026-04-19 symptom) proves it is dead or silently clamped.

**Expected bar-level signature:** when `atr_thresh` is small (say 0.3) and `stale_bars` is 20, stale exits should be rare and mostly concentrate in range-bound pairs (GBPJPY can still trend; EURCHF is stalling material). When `atr_thresh` is 100 and `stale_bars` is 2, stale should fire on almost every trade after bar 3, converting the strategy into a two-bar time exit — this is the detection configuration used in the sensitivity test.

## Open questions to resolve in Phase 2 (code trace)

- **Q1.** Does `max_range` actually use the lookback I described, or is it a single-bar check? Confirm loop bounds.
- **Q2.** Is the exit priced at `bar_close` of the triggering bar, or at `open[bar+1]`? Confirm the bar index passed to exit-price computation.
- **Q3.** Does the sub-bar loop run *after* stale fires on the same bar, potentially stomping the exit reason? (Expected: no — stale uses `break 'bar_loop;`.)
- **Q4.** Does the encoding in `ff/encoding.py` / `ff/defaults/complexity.py` correctly wire all three slots (`enabled`, `bars`, `atr_thresh`), or does the off-branch leave garbage in the inactive slots?

## Codex independent brief

*(Verbatim from `codex exec --sandbox read-only -m gpt-5.4 --config model_reasoning_effort=high`, 2026-04-19.)*

> A stale exit is not a universally standardized retail-forex order type; it is a strategy rule. In ordinary practice it means "this trade has stopped justifying capital and risk": after a minimum holding time, price has failed to move enough to confirm continuation or resolution. The "time + volatility" definition is coherent: close only after enough H1 candles have elapsed and recent candle ranges have stayed below a volatility benchmark. ATR is a standard volatility measure, not a directional signal; Fidelity describes ATR as measuring volatility from true ranges, commonly over a chosen number of bars.
>
> `bars` should mean an integer count of completed H1 candles since entry. `atr_thresh` should be dimensionless: a multiplier applied to ATR measured at entry, then compared in the same price units, usually pips. Example: entry ATR = 12 pips, `atr_thresh = 0.8`, stall ceiling = 9.6 pips. Every H1 high-low range in the lookback must be at or below that ceiling for stale to trigger.
>
> Spread should not be ignored. The trigger may be evaluated on the strategy's bar series, often bid, mid, or broker candle data, but the exit fill is side-specific. A long closes by selling, so it exits at bid. A short closes by buying, so it exits at ask. OANDA states this explicitly: long trades open at ask and close at bid; shorts open at bid and close at ask. MetaTrader's position-close documentation follows the same convention for long/short stop or take-profit triggering.
>
> Slippage is separate from spread. If the stale condition is known only after the H1 candle completes, the clean backtest assumption is: detect at bar close, submit a market close, fill at the next executable quote, commonly modeled as next-bar open plus side-appropriate spread and slippage. Filling exactly at the same bar close is acceptable only if the engine explicitly models "market-on-close" or bar-close execution without lookahead. Otherwise it is optimistic. This is partly disputed because retail backtest engines differ, but live mechanics are clear: slippage is the difference between expected and actual execution price, as CFI and StoneX both define it.
>
> There is no mechanical reason for the stale trigger itself to differ for longs and shorts. Time held and realized range contraction are direction-neutral. Asymmetry enters through bid/ask execution, swaps, and possibly broker candle construction, not through the stale logic.
>
> Edge cases: `bars < 2` should be rejected or clamped; your documented 20-200 range is sensible. An absurd `atr_thresh`, e.g. `100.0`, makes the volatility test almost always pass, reducing stale to a time exit. The lookback must not inspect candles before the trade existed; truncate to entry onward, and do not evaluate stale until age >= `bars`. If `max_bars` and stale fire on the same bar, record `stale` if the volatility condition is also true, because it is the more specific economic reason; this priority is not market-standard, so document it. "ATR-at-entry" should freeze at entry; rolling ATR changes the rule into adaptive current-volatility logic, not the claimed entry benchmark. If stale fires after a partial close, record the remaining-position exit reason as `stale`; the partial close keeps its own earlier reason.

## Diff: my brief vs Codex

### Agreements (high-confidence)

- **Semantics:** "time + volatility" cut-out is a coherent definition; it closes trades that have stopped justifying capital.
- **Units:** `bars` = integer H1 count; `atr_thresh` = dimensionless multiplier of ATR-at-entry → stall ceiling in pips.
- **`atr_thresh=100.0` reduces the rule to a pure time-exit.** Matches the sensitivity-test detection strategy.
- **ATR should be frozen at entry**, not rolling — otherwise the rule changes meaning.
- **No long/short asymmetry in the trigger.** Direction asymmetry enters only through bid/ask fill conventions, which is a systemic engine concern, not a stale-specific one.
- **Lookback must not extend before entry.** Both briefs agree the entry bar or earlier must be excluded.
- **Stale after a partial close:** remaining-position exit reason = `stale`; partial keeps its own earlier reason.

### Disagreements — load-bearing

1. **Exit-price timing and lookahead.** I noted that pricing at `bar_close` is "mildly optimistic". Codex is stronger: calling it **lookahead** unless the engine explicitly models market-on-close, and the clean assumption should be **next-bar open + side-appropriate spread + slippage**.
   - *Why it matters:* Phase 3 scenarios need to assert what exit price the engine actually uses. If `ff_core` fills at `bar_close[bar]` on the triggering bar (not `open[bar+1]`), we need to flag this as a systemic lookahead artifact. Will confirm in Phase 2 trace.

2. **Priority when `max_bars` and stale both fire.** The skill's cited order-of-check (max_bars first, stale second) means **max_bars wins** in Fire Forex. Codex argues **stale should win** because it carries more economic information ("volatility AND time stalled" ⊃ "time elapsed"). Codex flags this as "not market-standard, so document it."
   - *Why it matters:* If we ever add audit-level reporting of exit reasons for strategy-quality attribution, `max_bars` absorbing all the stale-matching bars will bias the summary. Not a correctness bug, but a semantic labelling choice the user should be aware of.

3. **Spread at exit.** My brief said "spread not re-applied at exit" and treated that as a known systemic property. Codex calls it out as a real gap from live mechanics (long-close-at-bid, short-close-at-ask). Same fact, different framing — Codex's framing is more operationally honest.
   - *Why it matters:* For any measure of *realised* expectancy from stale exits, the engine understates cost by ~0.5 × spread per exit. If the user cares about realism-vs-optimiser-gamification, this is a downstream knob (max_spread_pips / slippage_pips might partly compensate).

4. **Clamping of `bars < 2`.** Codex suggests the engine should reject or clamp values below 2. Current schema lower bound is 20 — moot in practice but the Rust code does not validate, so a manual override via overrides-JSON could smuggle in `bars=1` or `bars=0`. Worth a cheap defence-in-depth.

### Where Codex adds a citation

- **ATR as "true range" measure:** Codex cites Fidelity.
- **Long/short close conventions:** Codex cites OANDA and MetaTrader.
- **Slippage definition:** Codex cites CFI and StoneX.

I did not verify these citations. They match my prior knowledge of standard retail conventions — no obvious hallucination, but user should trust-but-verify if any specific claim ends up load-bearing.

### Resolution plan

- Disagreements 1, 2, 3 are all **engine-behavior questions**, not mechanics disputes. Phase 2's code trace will answer them directly. I'll annotate Phase 3's scenario table with each behaviour resolved.
- Disagreement 4 is a schema-hardening recommendation, orthogonal to the validation. File as a "low-priority engine wish" if the verdict isn't already (c).

---

*File updated 2026-04-19 with Codex independent brief and diff.*

---

*File: `docs/validation/2026-04-19-stale-exit/01-mechanics-brief.md`*
*Written 2026-04-19 from cold read of `trade_full.rs:95-145`, `lib.rs:215-380`, `ff/defaults/complexity.py:220-255`, `docs/knob-explanations.md:139-145`, and `references/forex-mechanics-primer.md` sections 1-6.*
