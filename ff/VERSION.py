"""Engine version string, shown in the web UI header.

Bump whenever a Rust-engine change or meaningful behaviour shift lands —
so at a glance the user can see which engine their sweeps are using.
Keep the label short (≤32 chars).

History
-------
v6 chandelier-stop — 2026-04-19 (evening)
    New peak-anchored ATR trailing stop added as independent Group
    `engine.chandelier` in eas/complex01.json. Three new PL slots
    (PL_CHANDELIER_ENABLED/ACTIVATE/ATR_MULT = 37/38/39), three new
    TradeParams fields, one new EXIT_CHANDELIER=7 code. Classic
    LeBeau semantics: SL = peak_high - atr_mult*ATR (long),
    trough_low + atr_mult*ATR (short), ratchets one-way with a
    side-of-price guard mirroring the v2 trailing fix. Distinct from
    the existing misnamed TRAIL_ATR_CHANDELIER (which uses current
    bar high, not peak-since-entry) — that mode stays frozen. Built
    via the add-forex-knob skill; see docs/builds/2026-04-19-chandelier-stop/.

v5 signal-filter-fix — 2026-04-19 (evening)
    Four signal-filter fixes. (D2) ff/encoding.py ENGINE_DEFAULTS now
    seeds PL_SIGNAL_P0..P9 at -1.0 — closes a silent "filter active for
    value 0" trap for any EA adding a Pk slot. (D4) core/src/lib.rs
    Pk trial extraction uses .round() instead of `as i64` truncation,
    so a sampler drawing 2.9 rounds to 3. (D5) Buy/sell filter switches
    to tolerance compare (|a-b|<1e-9) — absorbs f64 arithmetic drift
    like 0.1+0.2. (D6) Buy/sell filter now honours signal-side -1 as a
    bilateral opt-out, matching variant and Pk semantics. Surfaced by
    the fifth validate-forex-knob run. See
    docs/validation/2026-04-19-signal-filters/.

v4 scatter — 2026-04-19 (afternoon)
    Per-trial metrics + packed PnL slices now persisted to every NPZ
    (ff/harness.py). New endpoints /api/runs/{file}/scatter and
    /api/runs/{file}/trial/{idx} surface the per-trial view. Results
    tab gains an MT5-style scatter below the equity chart — click a dot
    to see that trial's equity curve + stats, or jump straight to the
    best-scoring trial. Y-axis is selectable (Total pips default).

v3 partial-fix — 2026-04-19 (afternoon)
    Two guards added to core/src/trade_full.rs partial block (lines
    285-330). (a) Skip partial when TP sits closer to entry than the
    trigger and the TP is reachable this sub-bar, so the TP closes the
    full position — mirrors real limit-order priority. (b) Realise the
    partial at the trigger price rather than sb_close, matching how SL
    and TP fills are priced and matching real-world limit-order fills.
    Surfaced by the third validate-forex-knob run. See
    docs/validation/2026-04-19-partial-close/.

v2 trailing-fix — 2026-04-19 (afternoon)
    Side-of-price guard added to the four trailing-stop sites in
    core/src/trade_full.rs (activation long / short, ongoing long /
    short). Same shape as v1's BE fix. Surfaced by the second
    validate-forex-knob run. See docs/validation/2026-04-19-trailing/.

v1 breakeven-fix — 2026-04-19 (morning)
    Side-of-price guard added to core/src/trade_full.rs breakeven block.
    Rejects any BE move that would place the SL on the wrong side of
    sb_close. Fixes the 78%-win-rate false positive surfaced by the
    first validate-forex-knob run.

v0 exec-full-only — 2026-04-19 (earlier)
    EXEC_BASIC path deleted; single trade path. Not formally pinned at
    the time, recorded here retroactively.
"""

VERSION = "v6 chandelier-stop"
