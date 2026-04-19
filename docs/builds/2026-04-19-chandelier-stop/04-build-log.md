# 04 — Build log: `chandelier_stop`

Minimal-diff implementation across the layers named in the slot map.
Every edit below names file + what + why.

## Edits

### 1. `core/src/constants.rs`
- Added `EXIT_CHANDELIER: i64 = 7` (next after `EXIT_STALE = 6`).
- Added `PL_CHANDELIER_ENABLED = 37`, `PL_CHANDELIER_ACTIVATE = 38`,
  `PL_CHANDELIER_ATR_MULT = 39` (first slots in the 37–63 reserved
  block; `NUM_PL = 64` unchanged).
- Updated the trailing reserved-slot comment to `40-63`.

### 2. `core/src/trade_full.rs`
- **Signature:** three new params on `simulate_trade_full`:
  `chandelier_enabled: i64`, `chandelier_activate_pips: f64`,
  `chandelier_atr_mult: f64`, inserted between `stale_atr_thresh`
  and `commission_pips` (mirrors the struct order in lib.rs parse).
- **State:** added `chandelier_active`, `chandelier_peak_high`
  (initialised to `actual_entry`), `chandelier_trough_low`
  (initialised to `actual_entry`), `pending_chandelier_active`.
  All reset per-trade inside the function (not cross-trade) — the
  trade simulator is called fresh per signal so there is no stale
  state to leak, matching how `trailing_active` / `be_locked` work.
- **Pending apply:** added `chandelier_active = pending_chandelier_active;`
  inside the deferred-SL apply block so the flag lifts at the same
  sub-bar boundary as trailing / breakeven.
- **Per-sub-bar block:** inserted between trailing and partial-close.
  Updates peak/trough intrabar; arms once float-PnL ≥
  `chandelier_activate_pips`; writes pending SL via the same
  side-of-price guard pattern used by the v2 trailing fix
  (`raw_sl < sb_low` for long, `raw_sl > sb_high` for short).
  Ratchet: only adopts when `new_sl > effective_sl` (long) or
  `new_sl < effective_sl` (short).
- **Exit attribution:** both long-SL and short-SL fill sites now
  check `chandelier_active` first, then `trailing_active`, then
  `be_locked`, then `EXIT_SL`. Mirrors the existing priority chain.
- **Inline tests:** two `simulate_trade_full` call sites in the
  `tests` module received `0, 0.0, 0.0,` for the new params
  (chandelier off), keeping existing assertions unchanged.

### 3. `core/src/lib.rs`
- **Parse:** three `let chandelier_* = params[...]` reads added after
  the `stale_*` reads.
- **Call:** three new args threaded into the `simulate_trade_full`
  call (between `stale_atr` and `commission_pips`).
- **pyo3 exports:** three `m.add("PL_CHANDELIER_*", ...)?` lines
  added so `ff_core.PL_CHANDELIER_*` is importable from Python.

### 4. `eas/complex01.json`
- Added `"chandelier"` Group sibling of `trailing` / `breakeven` /
  `partial` / `stale`. `test` Choice[true,false]; `when_on.activate`
  FloatRange 5–25 log; `when_on.atr_mult` FloatRange 2.0–4.0 linear.
  Defaults chosen per phase-1 mechanics brief (LeBeau 3.0 midpoint,
  Codex sweet-spot 2.5–3.5 nested inside).

### 5. `eas/complex01.py`
- Appended three tuples to `ENGINE_MAPPING`:
  - `PL_CHANDELIER_ENABLED` via `slot_bool_to_int` (0 when off).
  - `PL_CHANDELIER_ACTIVATE` via `slot_if_on(default=-1.0)`.
  - `PL_CHANDELIER_ATR_MULT` via `slot_if_on(default=-1.0)`.
  Matches the sentinel convention in the slot map.

### 6. `ff/VERSION.py`
- Bumped to `VERSION = "v6 chandelier-stop"` and prepended a
  history entry citing this build directory.

## Explicitly NOT edited

- `ff/defaults/volatility.py` — `atr_mult` is scale-free (ATR
  already carries pair volatility). `activate_pips` could be
  pair-aware, but for the MVP the fixed 5–25 range is deliberate:
  keeps the sweep space consistent across pairs and matches how
  `trail_activate_pips` was first landed before its entry was
  added to ATR_RULES. Phase 7 ship checklist notes this for a
  follow-up if post-shipping numbers justify it.
- `ff/defaults/complexity.py` — the complex01 EA is the sole
  entry point for chandelier right now. Surfacing the knob via
  the complexity recipe can land in a later session once actual
  behaviour is validated.
- `eas/baseline.py` — does not map chandelier slots; defaults to
  zero, which hits the `enabled == 0` short-circuit. No-op, by
  design.
- `ff/encoding.py` `ENGINE_DEFAULTS` — the sentinel `-1.0` lives
  in the `slot_if_on(default=-1.0)` callables directly, so there
  is nothing extra to add here.
- `app/static/` — the parameters tab renders chandelier automatically
  from the JSON schema. Verified by smoke in phase 5.

## Risks caught during implementation

1. **Pending-SL interaction.** The chandelier block can collide with
   the trailing block's `pending_sl` write in the same sub-bar if
   both are armed. Resolution: the chandelier block reads
   `effective_sl = pending_sl` (when already set) so it only
   tightens on top of the trailing pending; whichever writes a
   stricter SL wins. Matches the "most protective SL" semantics
   from phase 1.
2. **Peak/trough init.** Initialised to `actual_entry` (the
   spread-adjusted fill), not `entry_price`. This means the
   chandelier can technically see a "peak" below the first bar
   high on wide-spread openings, which is fine — the peak tracker
   only ratchets upward, and the side-of-price guard prevents
   firing on a raw_sl that hasn't moved off entry.
3. **Arm-at-H (not arm-at-C).** Matches trailing. The side-of-price
   guard is the sole defence against the unearned-trail-win bug;
   row 6 of the phase 3 scenarios pins this.

## Up next

Phase 5 — build + smoke:
1. `.\.venv\Scripts\maturin.exe develop --release`
2. Restart web server (kill port 8000, relaunch).
3. 20-trial CLI sweep on `eas/complex01.py`.
4. `pytest tests/`.
5. Confirm version pill reads `v6 chandelier-stop`.
