# Fire Forex core engine — Rust engine wishlist (driven by Fire Forex)

Features Fire Forex would benefit from that require a change in the `ff_core`
Rust engine. Collect until the list hits ~3 items, then batch into a CBT PR.

**Rule:** Fire Forex does not fork Rust. Every item here lives in CBT proper.

---

## Wanted

### 1. Per-hour slippage (or per-signal slippage column)

**Why:** real slippage varies by session (Asian session is thin, news moments
explode). Today's engine takes a single scalar for the whole run.

**Proposed shape:** either
- New batch_evaluate param: `slippage_by_hour: np.ndarray(shape=(24,))` indexed by H1
  hour; engine looks up per-trade.
- OR a new signal column `slippage_pips: np.ndarray(shape=(n_signals,))` pre-computed
  in Python using any model the EA wants.

Second option is more flexible (EAs define the slippage model) and avoids baking
session definitions into Rust.

**Impact on Fire Forex:** lets EAs declare `slippage_model: "per_session" | "per_bar" | "scalar"` and have honest execution.

### 2. New exit mechanisms (as needed)

Placeholder — specific items TBD when we encounter them. Likely candidates:

- **Volatility-regime exit.** Close when realised vol drops below a threshold
  (mean-reverted back to calm).
- **Drawdown-adaptive exit.** Tighten SL if the trade has been underwater > N bars.
- **ML-predicted exit.** A per-trial "exit probability" array, engine exits when
  probability crosses a threshold.

**Proposed shape:** each becomes a new `PL_*` block with an `_ENABLED` switch and
its own sub-slots.

**Impact:** unblocks later EAs that want more sophisticated exits than
SL/TP/trail/BE/partial/stale/max_bars.

### 3. Per-trial commission (if/when we need to tune or regime-switch)

**Why:** same as slippage — commission can shift with session or trade size.

**Proposed shape:** a commission column or per-hour array, symmetric with the
slippage design.

**Priority:** low. Single-scalar commission is already close enough for in-sample
testing.

### 4. Extra PL_SIGNAL_P0..P9 exposure (if not already)

Audit noted these slots referenced in code (indexes 27-36) but our Python wheel's
`NUM_PL` reports 27. If these are intended-but-unexposed, exposing them gives us
ten more filter slots for free.

**Proposed shape:** confirm wheel export matches engine capability; bump `NUM_PL`.

**Priority:** medium — more exact-match filter slots = more ways to gate signals
by session / quality / regime without rewriting signal pools.

---

## Deliberately deferred (won't pursue yet)

- Multi-asset / multi-instrument engines in one call.
- Walk-forward wrapper in Rust (do it in Python).
- Position sizing / compounding (deferred for Fire Forex too).
