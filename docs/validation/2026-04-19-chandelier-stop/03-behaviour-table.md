# 03 — Expected-behaviour table: `engine.chandelier`

> The behaviour table was hand-calculated during the `add-forex-knob`
> phase 3 earlier in the session. Reused as-is for validation phase 4.

**Primary table:** [`docs/builds/2026-04-19-chandelier-stop/03-reference-scenarios.md`](../../builds/2026-04-19-chandelier-stop/03-reference-scenarios.md)

Seven scenarios cover:

1. Plain-vanilla long pass → +50 pips at chandelier fill.
2. Knob disabled (sentinel) → TP hits first (baseline), +60 pips.
3. Edge: just inside activation → arms at B3 H, fills +75 pips.
4. Edge: just outside activation → never arms, baseline exit.
5. Short symmetry of row 1 → +50 pips (mirror).
6. Side-of-price guard (long) → guard rejects early raw_sl, trade
   lives; on B2 raw_sl 1.10010 is below sb_low 1.10028 so SL
   tightens to 1.10010. No exit. Asserts `sl == 1.10010`, open.
7. Strict no-op sentinel → every trade metric identical to a
   no-chandelier baseline.

Per-row arithmetic (pip-by-pip) is in the linked file. Phase 4
micro-test encodes these as seven pytest cases.

## Acceptance criteria

Every scenario in the primary table must reproduce to within
rounding in the micro-test below. A near-miss is a failure — the
brief is wrong or the engine is wrong; either answer matters.
