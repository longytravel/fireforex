---
description: Python style rules for Fire Forex
paths: ["ff/**/*.py", "app/**/*.py", "eas/**/*.py", "tests/**/*.py", "scripts/**/*.py", "run.py"]
---

# Python style — Fire Forex

## Hard rules
- Never compare floats with `==`. Use `pytest.approx`, `math.isclose`, or round-and-compare integers.
- Never represent money or pip values as bare `float` in test assertions; prices in the engine use float for speed but comparisons in tests must use tolerance.
- `snake_case` for functions, vars, files. Dotted knob paths (e.g. `stop_loss.atr.mult`).
- Imports sorted by `ruff` (no manual reordering).
- Use the project venv: `.\.venv\Scripts\python.exe`. Never `uv`, never system Python.

## Soft preferences
- `pathlib.Path` over string paths.
- `dataclasses` over bare dicts for domain models.
- Prefer an explicit `if x is None` over truthy checks on optional values.
- Keep `ff/` side-effect-free; HTTP and jobs live in `app/`.

## Forbidden patterns
- `print(...)` left in shipped code — use `logging` or remove.
- `# TODO` / `# FIXME` without an issue number.
- Global mutable state in `ff/` modules.
- Shelling out to `uvicorn` or `python run.py web` from Claude sessions (see `.claude/rules/trading.md`).
