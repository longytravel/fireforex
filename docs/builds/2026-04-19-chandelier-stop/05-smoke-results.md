# 05 — Smoke results: `chandelier_stop`

## Build

```
.\.venv\Scripts\maturin.exe develop --release
```

Result: **Finished `release` profile [optimized] target(s) in 4.21s**.
`fire_forex-0.1.0` installed editable. Seven compiler warnings, all
pre-existing `#[warn(dead_code)]` on unrelated unused constants —
nothing new surfaced by the chandelier edits.

## Version bump

`ff/VERSION.py` → `VERSION = "v6 chandelier-stop"`. Live endpoint
verified:

```
GET /api/version → {"version":"v6 chandelier-stop"}
```

## Server restart

Three old uvicorn worker PIDs (9724, 31328, 18640) were orphaned from
pre-build sessions; killed via PowerShell `Stop-Process -Force` after
`taskkill //F` reported success on paper but the sockets hung in
`FinWait2`. Fresh reloader (25976) + worker (21456) started cleanly.
Port 8000 now serves the new `.pyd`.

## 20-trial smoke sweep

```
.\.venv\Scripts\python.exe run.py eas/complex01.py --trials 20 --seed 42
```

Stats:

| metric         | value       |
|----------------|-------------|
| backtests/sec  | 459         |
| total runtime  | 0.58 s      |
| trades (best)  | 1,197       |
| win rate       | 76.52 %     |
| total pips     | +226        |
| expectancy     | +0.19 pips  |
| profit factor  | 1.024       |

Best-trial summary line now prints `chandelier.test=False` alongside
the other Group toggles — schema registered, sampler respects
the gate, trial summary renders. Another smoke invariant: zero
panics logged in the run. NPZ saved to
`artifacts/runs/complex01_random_20260419_173135.npz` and the row
appended to `artifacts/history.csv` (now 9 runs).

One best-trial trace had `chandelier.test=False`; the rest of the 20
trials will include a mix of on / off sampled by the Group (on/off
groups reported as **7** in the preflight — chandelier correctly
bumped the count from 6 pre-build to 7). Phase 6 validate-forex-knob
will stress the on path directly.

## Test suite

```
.\.venv\Scripts\python.exe -m pytest tests/
```

Result:

```
82 passed, 1 skipped, 5 warnings in 2.66s
```

Skip is the existing `@pytest.mark.slow` on `test_golden_baseline.py`
(not a chandelier regression). All five `tests/validation/`
micro-tests (breakeven, partial, trailing, stale, signal-filters)
still green — the chandelier addition did not disturb any previous
validated knob.

## Readiness for phase 6

- Rust engine rebuilt, `.pyd` replaced.
- Python module re-imports `PL_CHANDELIER_*`, `EXIT_CHANDELIER`.
- Version pill live.
- No new test failures.
- Smoke sweep complete, zero panics.

Hand-off: `validate-forex-knob` skill, knob name
`engine.chandelier`. Expected phase 4 micro-test scenarios already
hand-calculated in `03-reference-scenarios.md` — the validate skill
can reuse them.
