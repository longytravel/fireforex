# 06 — Audit link: `chandelier_stop`

## Validation directory

[`docs/validation/2026-04-19-chandelier-stop/`](../../validation/2026-04-19-chandelier-stop/)

Six artifacts:
- `01-mechanics-brief.md` — points back to build phase 1.
- `02-code-trace.md` — my trace + verbatim Codex `gpt-5.4 high`
  independent trace + diff (100 % agreement, zero hazards).
- `03-behaviour-table.md` — points back to build phase 3.
- `04-micro-test.py` — five hand-calculated scenarios (long fires,
  short fires, guard rejects, activation gate, sentinel no-op).
  Copied into `tests/validation/test_chandelier_mechanics.py`.
- `05-sensitivity-results.md` — per-knob sensitivity test pass +
  500-trial A/B on real EUR_USD H1 data.
- `06-verdict.md` — **verdict (a): works as advertised.**

## One-line verdict

> Works as advertised. Phase 4 micro-test green (5/5); phase 5
> sensitivity shows chandelier materially changes 500-trial real-data
> sweep outcomes (+392 pip diff in best trial between chandelier-off
> vs chandelier-on runs). Zero hazards flagged in the code trace;
> Codex independent trace confirms all seven layers of wiring.
