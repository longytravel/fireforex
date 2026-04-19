# Fire Forex code review — 2026-04-19

## TL;DR

The web UI is a clean, well-scoped wrapper around the existing `ff/` package: recipe-driven EA construction on the server, a single-slot job runner, persisted baselines, and a plain-ES-modules frontend — no bundler, no framework, easy to reason about. However there are two real bugs (a **NameError in `/api/inspect`** and a persistent **XSS sink** in the history table), a handful of unused imports / dead request models, a subtle **race between concurrent `/api/baseline` writers**, and several UX rough edges for a non-coder (cryptic 503s, no cancel button, free-text `layer_name` with no feedback on validity). Nothing architectural is broken — fix the NameError before demo, the XSS before anyone else touches `artifacts/history.csv`, and the rest can be cleaned up as polish.

---

## Critical issues (fix before next push)

- **`C:\Users\ROG\Projects\Fire Forex\app\routes.py:119`** — `post_inspect(req: DefaultsRequest)` references `DefaultsRequest` but it is **never imported**. `app/models.py` defines it (line 27) but `routes.py` has no `from .models import DefaultsRequest`. Calling `POST /api/inspect` will fail at import/startup-time under FastAPI schema resolution. Either import it (`from .models import DefaultsRequest`) or — consistent with every other endpoint in this file — accept `body: dict[str, Any]` and validate manually.

- **`C:\Users\ROG\Projects\Fire Forex\app\static\app.js:871-880`** — `refreshHistory()` interpolates `r.layer`, `r.pair`, `r.main_tf`, `r.datetime` straight into a template-literal `tr.innerHTML` with **no HTML escaping**. Users control `layer_name` via the Run tab, and that value round-trips through `artifacts/history.csv` into this sink. A `layer_name` of `<img src=x onerror=alert(1)>` runs arbitrary JS in the local page. Also at `app.js:757` (KPI tiles) and `app.js:599` (knob rows) — any string from `/api/defaults` or `/api/jobs` fed into `${…}`. Fix: add an `escapeHtml(s)` helper (replace `&<>"'`) and wrap every user-derived interpolation. The current `escapeAttr` at line 82 only escapes `"` — that's attribute-safe but not text-safe and the name is misleading.

- **`C:\Users\ROG\Projects\Fire Forex\app\baselines.py:31-33`** + **`app/routes.py:261-296`** — `save()` does `BASELINE_PATH.write_text(...)` with no lock and no tmp-file+rename atomic swap. Two near-simultaneous `POST /api/baseline` calls (very plausible via double-click) can interleave and leave a corrupt JSON file; `load()` then silently returns `None` (line 27-28, broad `except Exception`) and the UI quietly loses the pinned baseline. Write to `baseline.json.tmp` then `Path.replace()`, and wrap the critical section in the same `threading.Lock` jobs.py uses — or a separate one.

- **`C:\Users\ROG\Projects\Fire Forex\app\jobs.py:94-146`** — `_lock.acquire(blocking=False)` is released inside `_worker`'s `finally`. If `threading.Thread(...).start()` at line 148 itself raises (OOM, OS limit), the lock is held forever and the whole API returns 409 until restart. Wrap the `Thread(...).start()` in a try/except that releases the lock and removes the half-created `_jobs[job_id]` entry.

## Medium issues (worth fixing soon)

- **`C:\Users\ROG\Projects\Fire Forex\app\routes.py:10`** — imports `JSONResponse` but never uses it. Remove.

- **`C:\Users\ROG\Projects\Fire Forex\app\models.py:9, 16`** — `RunRequest` and `JobProgress` are defined but **nothing imports them**. `POST /api/run` accepts raw dicts and validates inline at `routes.py:131-141`. Either delete the dead classes or switch `/api/run` to `RunRequest` for free validation + OpenAPI schema. Same question for `DefaultsRequest` once the NameError is fixed.

- **`C:\Users\ROG\Projects\Fire Forex\app\jobs.py:218-226, 222-224`** — `import json as _json` is done inside the `if path.exists()` branch twice on separate lines. Hoist the `import json` to module top (module already imports `csv`). Minor but jarring.

- **`C:\Users\ROG\Projects\Fire Forex\app\baselines.py:45, 72`** — `__import__("time").time()` is used instead of a top-level `import time`. Works, but obfuscates intent for no reason.

- **`C:\Users\ROG\Projects\Fire Forex\app\pairs_scan.py:38-43`** — `@lru_cache(maxsize=1)` caches filesystem discovery **forever** for the life of the process. Drop a fresh parquet into `G:\My Drive\BackTestData` and the UI won't see it until restart. Either expose a `/api/pairs/refresh` endpoint that calls `scan_pairs_cached.cache_clear()`, or replace the cache with a 60-second TTL.

- **`C:\Users\ROG\Projects\Fire Forex\app\routes.py:37`** — 503 message hardcodes `G:\My Drive\BackTestData` but `pairs_scan.DATA_ROOTS` has two entries. If both are missing, the error points at only one. Render the actual attempted paths from `DATA_ROOTS`.

- **`C:\Users\ROG\Projects\Fire Forex\app\static\app.js:82`** — `const escapeAttr = (s) => String(s).replace(/"/g, '&quot;')` is not enough for attribute context either. A value containing `" onmouseover="alert(1)` gets `&quot;` but still breaks out via an unescaped `>` or backslash. Use a proper escape set or set attributes via `.setAttribute()` and text via `.textContent` instead of `innerHTML`.

- **`C:\Users\ROG\Projects\Fire Forex\ff\harness.py:401-414`** — history-CSV write path reads the whole CSV with `pd.read_csv`, concats, and rewrites. Not crash-safe (kill between read and write loses rows) and O(n²) as history grows. Acceptable short-term; flag for future fix — `csv.DictWriter` in append mode is simpler and atomic per row. Also no lock, though the single-slot job runner currently guarantees serialisation.

- **`C:\Users\ROG\Projects\Fire Forex\app\static\app.js:915` (near bottom)** — poll interval is a fixed `setTimeout(pollJob, 500)`. For a 2000-trial run that's ~60 harmless polls; fine for now but worth noting as the first thing to swap to an SSE or WebSocket stream when runs get longer.

- **`C:\Users\ROG\Projects\Fire Forex\app\routes.py:213-238`** — `_EXPLAIN_CACHE` is a module-global set once and never invalidated. Editing `docs/knob-explanations.md` while the server runs has no effect. Minor, but worth a comment or a `?refresh=1` query param.

- **`C:\Users\ROG\Projects\Fire Forex\app\routes.py:196-197`** — `save_ea` writes `eas/user_{name}.json` but the `_SAFE_NAME` regex (`routes.py:173`) permits `A-Za-z0-9_-`. Good. But there's no collision check — saving the same name silently overwrites. At minimum return `{"overwritten": true}` or 409 on existing.

- **`C:\Users\ROG\Projects\Fire Forex\app\routes.py:292-296`** — `DELETE /api/baseline` always returns 200 even if the file never existed. Fine, but differ from the POST convention for the pair and worth a comment.

- **`C:\Users\ROG\Projects\Fire Forex\app\jobs.py:152-153`** — `_jsonable` accepts `list` and `dict` but does **not** recurse into them. A dict with a numpy value inside (which `harness.run` does return in places) will fail at `json.dumps` later when FastAPI serialises. Exercise: run a Level-10 EA and check `result.raw` on the wire.

- **`C:\Users\ROG\Projects\Fire Forex\run.py:55-68`** — `import uvicorn` happens inside `run_web`; good. But `run_web` silently returns 0 after `uvicorn.run()` exits via Ctrl-C (line 73 bare `except Exception`) — this masks real startup errors (e.g. port in use). Narrow to `KeyboardInterrupt`.

- **`C:\Users\ROG\Projects\Fire Forex\app\api.py:35-40`** — `root()` falls back to `README.md` with `media_type="text/markdown"`. Most browsers will prompt a download. Either serve the README rendered, or return a 404 — the fallback is worse than both.

## Low-priority polish

- **`app/jobs.py:193-203`** — `_ENGLISH_LABELS` duplicates vocabulary from `app/static/app.js:371-393` (`GROUP_FRIENDLY_NAMES`). Pick one source of truth — probably the server — and expose via `/api/explain-bundle`.
- **`app/routes.py:131-147`** — `/api/run` duplicates the recipe-validation shape already expressed in `models.RunRequest`. Fold.
- **`app/static/app.js:9`** — `FULL_SCHEMA_LEVEL = 10` is a magic number duplicated in multiple server paths. Expose from `/api/timeframes` or a new `/api/capabilities` so the constant lives in one place.
- **`app/static/styles.css`** — uses Tailwind CDN (`index.html:7`) plus a local `styles.css`. Mixing arbitrary Tailwind classes with a growing custom stylesheet; pick one.
- **`app/static/index.html:7`** — `<script src="https://cdn.tailwindcss.com">` is a live CDN fetch on every page load. If the user is offline (and the data *is* local), the page style breaks. Vendor Tailwind or inline a compiled build.
- **`ff/harness.py:27`** — imports `webbrowser` unconditionally but only uses it when `open_browser=True`. Harmless but hints at long-lived design.
- **`docs/knob-explanations.md`** parsing at `app/routes.py:213-228` is fragile — any stray `## `/`- ` line breaks the map. Replace with YAML front-matter or an explicit JSON file.
- **`tests/test_complexity.py`** file count: 118 lines. Good start, but see next section.
- `demo_speed.py` is referenced extensively in README/handover but lives at project root; consider `scripts/demo_speed.py`.
- **`app/jobs.py:30-32`** redefines `ARTIFACTS_DIR` / `HISTORY_CSV` / `RUNS_DIR` that also exist in `ff/harness.py:112`. Factor into `ff/paths.py`.

## Tests — gaps and suggestions

**What's tested** (`tests/test_complexity.py`): happy-path `complexity_to_ea(level, pair, main_tf)` at levels 1, 3, 6, 10 — asserts schema shape and that `RandomSampler.sample(5)` returns 5 dicts. That's it.

**What isn't** (cheap to add):
- `app/routes.py` — no route is exercised. Spin up the app with `httpx.ASGITransport` (already in `requirements-web.txt` line 8) and smoke-test `GET /api/pairs`, `GET /api/timeframes`, `GET /api/defaults?pair=EUR_USD&main_tf=H1`, and `POST /api/inspect` — the last one will catch the `DefaultsRequest` NameError I flagged.
- `app/jobs.py` — no test for the 409-on-double-run path, the `start` lock, or `_kpi_block`.
- `app/baselines.py` — no test for `pin_from_job` / `pin_from_history_row` / `delta` — each is a tiny pure function, two-line tests each.
- `app/pairs_scan.py` — no test for the two-roots fallback. Parametrise with a `tmp_path` fixture full of fake `FOO_BAR_H1.parquet` files.
- `ff/defaults/overrides.py` — `apply_overrides` has several branches (per-knob, per-group, global step multiplier). Zero tests.
- **No XSS test.** Add one Playwright or jsdom test that posts a layer_name containing `<script>` and asserts the rendered DOM text is the literal string. (The user has Playwright via the MCP plugin.)

Concrete ask: add `tests/test_routes.py` (~60 LoC with FastAPI `TestClient`), `tests/test_overrides.py` (~40 LoC), and `tests/test_baselines.py` (~30 LoC). Would bump coverage of the new web surface from ~0% to ~60% for about 15 minutes of work.

## UX observations

The workflow is well thought out, but several rough edges will confuse the non-coder user described in `docs/next-session-handover.md`:

- **No cancel button.** Once a run is started the only way to abort is a hard restart. A big 2000-trial H1 run is several minutes — users *will* want to cancel when they realise they picked the wrong pair. Add `POST /api/jobs/{id}/cancel` and expose it next to the progress bar (`index.html` around line 160).
- **503 "no data roots found" is cryptic.** `app/routes.py:37` — the user won't know what `G:\My Drive\BackTestData` means. Render a friendly message on the Parameters tab ("No forex data found. Plug in the G: drive or install the ForexPipeline repo.") rather than a raw JSON error toast.
- **`layer_name` is free-text with no validation.** `app/static/index.html` shows a plain `<input type="text">`. Accepts `<`, quotes, 500-char garbage. Besides the XSS risk, the CSV rendering later truncates or breaks if a user pastes a newline. Mirror `_SAFE_NAME` regex client-side and show a hint: "Letters, numbers, `_` and `-` only, max 64."
- **"Complexity 1-10" slider has no units or examples.** The Parameters tab uses a numeric slider. Show a one-line legend next to the current value: "6 = EMA+MACD with SL/TP/trailing — the default for most users." The labels exist in `knob-explanations.md` but aren't surfaced near the slider.
- **Baseline delta is hidden until you pin.** Results tab (`index.html:190-250`) doesn't say "no baseline pinned yet" — the delta row just disappears. Add a soft hint: "Pin this run as baseline to compare future runs against it."
- **History table has no pair/tf filter.** After 20+ runs the user will want to filter. Cheap: a pair `<select>` above the table that filters client-side.
- **Equity curve is a single line with no y-axis scale.** Add min/max labels (`app/static/app.js` drawEquityCurve) — otherwise a flat curve looks the same as a moonshot.
- **Tab state is lost on reload.** `switchTab` doesn't write to `location.hash`. Users who refresh after a long run lose their place. One-line fix.
- **"Run" button says "Starting…" then "Run backtest" again.** While polling it stays disabled with text "Starting…" — progress bar is the only feedback. Set button text to match the progress message ("Sampling trials…", "Running engine…") for stronger feedback.
- **No "what's cooking" indicator when you've switched away.** If the user clicks to History mid-run, there's no indicator in the header that a job is running. Put a small orange dot next to the "Run" tab while `state.jobId` is set.

---

*End of review.*
