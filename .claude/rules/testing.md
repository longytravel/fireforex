---
description: Testing rules for Fire Forex — integration and parity first, unit tests second
paths: ["tests/**/*.py"]
---

# Testing — Fire Forex

## Priority order (bugs in Fire Forex history justify this)

1. **Parity harness** — live artifacts reconciled against a BT re-run. This is the only test that catches:
   - Signal variant ID drift across deployed configs
   - Timezone bugs (broker vs UTC)
   - Forming-candle refires
   - MT5 deal history gaps
   - Schema-path changes that silently no-op
   Parity match rate is the CI gate.
2. **Integration tests** — end-to-end across at least 2 components (e.g. signal resolution → Rust engine → artifact writer).
3. **Reference tests** — pinned NPZ: re-run engine on fixed inputs, compare output to a frozen expected-values file. Catches Rust↔Python contract drift.
4. **Unit tests** — only for pure helpers (sampler, encoding, schema validation). Do not TDD-cult data structs.

## Rules
- New knobs follow `add-forex-knob` + `validate-forex-knob` skills. Silent-no-op bugs have shipped before; a knob without an on/off sensitivity test is not done.
- Never `==` on floats — use `pytest.approx`.
- Every new signal / exit / filter / data loader must have fixtures for: no-trade, one-trade, many-trade, bad-data, missing-candles. (Copied from Freedom; still valid here.)

## Don't
- Don't add unit tests for `ff/schema.py` field types unless you're testing a validation rule. Field types are enforced by the type system.
- Don't mock the Rust engine. If the engine changes, the test should see it.
- Don't delete failing tests to make CI green. Fix the test or fix the code.
