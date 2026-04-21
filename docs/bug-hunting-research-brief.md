# Research Brief — Bug-Hunting Toolchain for Fire Forex

**Audience:** research team (no code access required)
**Requester:** project owner, single-user, non-technical
**Objective:** recommend a minimal, high-leverage toolchain for finding bugs — both the ones already in the code and the ones about to be introduced — given the project's specific constraints.

---

## 1. Project snapshot

**Fire Forex** is a local-only forex strategy backtesting and parameter-optimisation system running on a single Windows 11 workstation.

- **Stack:** Python 3.12 (FastAPI backend + vanilla JS/HTML single-page frontend) + Rust engine (`ff_core`, built via `pyo3` + `maturin`).
- **Workflow:** user describes a trading strategy as a schema of "knobs"; backend generates random trial combinations; Rust engine evaluates each trial against historical tick / M1 data; UI displays metrics and compares against a pinned baseline.
- **Data:** Dukascopy tick and M1 bar data stored as Parquet files on local disk and Google Drive.
- **Users:** one (the project owner).
- **Deployment:** bound to `127.0.0.1`, never hosted.

Roughly 10k lines Python, 3k lines Rust, 2k lines frontend JS, 130 passing tests.

## 2. Current state — the facts research needs to know

- **Repository is private, single-commit.** One initial commit on `main`. All subsequent work is uncommitted / staged.
- **No CI.** No GitHub Actions, no remote test runs, no status checks.
- **No linter or formatter configured.** No `ruff`, `black`, `mypy`, `clippy` invocations wired in.
- **Test suite:** 131 passing, 1 failing (`test_golden_baseline` — expected 181 trades, engine now produces 33; real drift, not flake).
- **Known bug surfaces:**
  - **Python ↔ Rust encoding contract.** Python hand-mirrors Rust struct slot indices in `ff/encoding.py`. No codegen, no guard test. A mismatch produces garbage trial results silently (no exception).
  - **Data pipeline.** Four parallel downloader modules with lazy imports scattered across the backend. File-write operations have no transactional guarantees; a crash mid-merge has already overwritten 19 years of historical bars once.
  - **Stale `.pyc` / stale `uvicorn` processes.** Editing Python code while a background web server runs has repeatedly caused sessions to serve old compiled code. Mitigated by a manual restart script.
- **Memory / knowledge-base stack in use:** project `CLAUDE.md`, session memory indexed in markdown, a `claude-mem` vector DB of past-session observations, and a `context-mode` MCP server for context-window savings. Extensive markdown docs in `docs/`.
- **AI-assisted tooling available:** Claude Code (primary), plus installed skills for Gemini and OpenAI Codex second-opinion reviews.

## 3. Research questions

Please answer the following. For each, prefer **concrete tool names, free/paid tier info, and why-it-matters for this specific project** over generic advice.

### 3.1 Local static analysis
Given a Python 3.12 + Rust + vanilla JS codebase on Windows, what is the **minimum viable** static-analysis stack that catches the most common bug classes (dead imports, unused vars, type errors, undefined behaviour, panics) with the least configuration overhead? Specifically:
- Python: `ruff` vs `pylint` vs `flake8` — current state of the art in 2026?
- Python typing: `mypy` vs `pyright` vs `pyrefly` — which for a codebase without prior type hints?
- Rust: beyond `cargo clippy`, is `cargo-deny`, `cargo-geiger`, or `miri` worth wiring in for a pyo3 extension module?
- JS: is `eslint` alone enough for a 2k-line no-framework SPA, or is it overkill?

### 3.2 Property-based and mutation testing
The backtest engine does heavy arithmetic. Traditional unit tests check specific values; property tests check invariants ("a profitable buy trade must have exit price ≥ entry price + spread"). What is the current best tooling in the Python + Rust ecosystem? Specifically:
- Is `hypothesis` still the standard for Python property tests in 2026?
- Is `proptest` (Rust) recommended over `quickcheck` for `pyo3` modules?
- Is mutation testing (`mutmut`, `cosmic-ray`) worth the CPU cost for a ~10k-line codebase, or is it overhead?

### 3.3 Contract / schema guard testing
The highest-risk bug class is **silent drift between the Rust struct and the Python encoder that fills it**. Research:
- Are there existing tools that auto-generate Python bindings from Rust structs (`pyo3-stub-gen`, `maturin` generators) that would eliminate the hand-mirroring entirely?
- If not, what is the recommended pattern for a single "contract guard" test that fails loudly when Rust adds a field and Python hasn't caught up?

### 3.4 GitHub-native tooling (if the repo goes public or gains CI)
Assume the project owner later decides to push the repo public or add a collaborator. Which GitHub-native tools offer the most bug-hunting value for a mixed Python/Rust/JS repo, and what is the onboarding cost?
- **CodeQL** — free for public repos. Realistic false-positive rate? Is it worth enabling for a small codebase?
- **Dependabot** — setup cost vs alert noise for this scale.
- **GitHub Actions** — minimum viable CI pipeline (`ruff`, `pytest`, `cargo test`, `cargo clippy`) — estimated runtime and monthly minutes on free tier?
- **Third-party bots** — `CodeRabbit`, `Codium PR-Agent`, `Sweep`, `Sourcery` — which are worth their subscription cost for a one-person project?

### 3.5 AI-assisted bug hunting
The owner already uses Claude Code, Gemini, and Codex interactively. Research:
- Are there workflows where running **two AI models in parallel on the same diff** reliably surfaces bugs one alone would miss? Cite studies or benchmarks if any exist.
- Is `superpowers:systematic-debugging` (a skills framework, structured prompt) qualitatively better than ad-hoc prompting for hard bugs? Any published evidence?
- Are there AI-native bug hunters specifically for backtesting / quant finance code that understand financial-correctness invariants (e.g. pip unit consistency, long-vs-short pnl sign)?

### 3.6 Indicator and backtest math correctness

This is a top priority. The backtest pipeline relies on technical indicators (RSI, EMA, SMA, MACD, ATR, Bollinger, Donchian, Keltner, etc.) and on trade-accounting arithmetic (pip conversion, pnl sign, spread/slippage application, stop-loss / take-profit hit detection at sub-bar resolution). A subtle formula bug silently invalidates thousands of trial results and looks indistinguishable from a valid strategy.

Specific failure modes the team should research:

- **Indicator formula variants.** RSI has at least three common variants (Wilder's smoothing, Cutler's SMA variant, exponential). EMA has ambiguous initialisation (SMA seed vs first value vs zero). Different variants produce materially different signals — especially during the warm-up window. How should a small team validate that their implementation matches the variant they *think* they are using?
- **Look-ahead bias.** An indicator computed at bar *t* must only use data from bars ≤ *t*. A common bug: using `close[t]` when computing a signal that is supposed to fire *at the open* of bar *t*, or using a rolling window that accidentally includes the current bar's high/low before the bar closes.
- **Warm-up / NaN handling.** The first *N* bars of any rolling indicator are undefined. Treating them as zero, propagating NaN silently, or trimming inconsistently across indicators can corrupt signals near the start of every dataset.
- **Pip-unit consistency.** Forex pip size differs by pair (JPY quotes: 0.01; most majors: 0.0001; metals and indices: varied). Bugs appear when pnl is accumulated in one unit and stops are set in another. How is this typically caught?
- **Long-vs-short sign errors.** Pnl for a short trade is `entry − exit`, for a long trade it is `exit − entry`. A single flipped sign inverts half of every backtest. Research suggests property-based tests ("inverting all directions and all prices should yield identical aggregate pnl") catch this class reliably — please confirm and recommend framework.
- **Fill assumptions.** Is a signal generated at bar close filled at that close, at the next bar's open, or at some modelled slippage? Different engines do different things. Research: what is the reference "correct" behaviour, and how do teams test that their engine matches it?
- **Spread and slippage modelling.** Static spread vs time-of-day spread vs tick-level spread produce different backtest outcomes. How do teams validate their cost model is not over- or under-optimistic?
- **Timezone / session boundary bugs.** Dukascopy data is UTC; MT5 servers are typically UTC+2/+3 (broker server time); a session-hours filter that mixes the two will fire on the wrong bars. How do teams catch this?

Specific research deliverables for this section:

1. **Reference libraries for indicator validation.** Survey `TA-Lib`, `pandas-ta`, `finta`, `bt`, `backtrader`, `vectorbt`, `QuantConnect/LEAN`, `NautilusTrader`, and the MetaTrader built-ins. For each, report: (a) which indicators it implements, (b) which variant (Wilder, Cutler, etc.), (c) license, (d) Python-usable as a cross-check library.
2. **Recommended cross-validation pattern.** Given the owner has a custom Rust engine, what is the recommended pattern for running the same indicator through (a) the custom engine, (b) a reference library, (c) TradingView or MT5 exported values — and asserting agreement within a tolerance?
3. **Property-test catalogue.** List 10 concrete property tests that every indicator / trade-accounting module should pass. Examples: RSI ∈ [0, 100]; ATR ≥ 0; sum of long pnl + short pnl on mirror dataset = 0; etc.
4. **Golden-fixture recommendations.** What is the recommended format for a "known input → known output" fixture file that survives engine refactors? CSV? Parquet? JSON with hashes?
5. **Differential / fuzz testing.** Given random OHLC data, is it useful to run the Rust engine and a Python reference implementation side-by-side and assert identical results? Recommend tooling and a practical pipeline.
6. **Backtest-specific correctness tools.** Are there open-source "backtest validator" libraries that sniff for look-ahead bias, survivorship bias, or unrealistic fills? Report on projects like `zipline`, `backtesting.py` validators, `pyfolio`, `quantstats`, and any newer 2025–2026 entrants.

### 3.7 Runtime / observability
The project runs on localhost. Traditional APM (Datadog, Sentry, New Relic) is overkill. Research lightweight options:
- Structured logging libraries (`structlog`, `loguru`, `rich`) — which gives the best "print-debugging-but-searchable" experience with near-zero setup?
- Local trace collectors that don't require a cloud account (e.g. self-hosted OpenTelemetry, `Jaeger` in Docker) — is this worth it for a single-user local app?

## 4. Constraints and scope

- **Budget:** prefer free tools. Paid tools acceptable if they save >2 hours / month.
- **Platform:** Windows 11. Tools must work either native or via the existing `.venv`. Docker is installed but avoided.
- **User profile:** owner is non-technical. Commands that require tweaking YAML for an hour are a hard cost. Zero-config or one-line-install tools are strongly preferred.
- **Scale:** ~15k total lines, one developer, no team workflow pressure.
- **Out of scope:** security scanning (this is a local-only tool handling no user data, no secrets, no authentication).

## 5. Deliverables

Please return:

1. **One-page "recommended starter stack"** — tool names, install command, what it catches, why for this project specifically. Maximum 8 tools. Rank-ordered by value-per-effort.
2. **One-page "defer / reject" list** — tools commonly recommended that are **not** a fit here, with one-line reason each. Helps the owner say no without re-researching.
3. **Three scenarios with recommended tool choices:**
   - *Scenario A:* "Test suite passes but a number on the UI looks wrong." — which tool first?
   - *Scenario B:* "A new parameter was added and silently has no effect." — which tool catches this?
   - *Scenario C:* "Engine produces different results after Rust edit, no test catches it." — which tool catches this?
   - *Scenario D:* "RSI values coming out of the engine don't match TradingView on the same candles." — which tool / pattern catches this?
   - *Scenario E:* "Short trades are systematically producing the wrong pnl sign." — which property test would have caught this?
4. **One-paragraph opinion on CI / GitHub public-repo decision.** Given that the project is private and single-user, is the effort of adding GitHub Actions + CodeQL + Dependabot worth it now, or a distraction? Include the tripwire (what event would change the answer).

Target length: **under 2,000 words total**. Prefer bulleted lists over prose. Cite specific version numbers and URLs where possible.

---

## Context appendix — likely questions

- **Why is this a brief and not just "use ruff and pytest"?** Because the owner has been burned repeatedly by silent-bug classes that generic linters don't catch: (a) dukascopy library returning null rows, (b) historical data overwritten during merge, (c) stale `.pyd` serving old Rust code, (d) indicator formula or sign errors that leave the test suite green but invalidate every trial result. The research needs to address those patterns, not just surface-level style issues. **Math / indicator correctness is co-equal priority to contract-drift bugs** — both are silent and both destroy trust in the backtest output.
- **Why no security focus?** The app is bound to `127.0.0.1`, has no auth, handles no PII, and never reaches the internet except to download public Dukascopy bars. Security tooling is real cost, zero benefit here.
- **Why no performance focus?** Performance work is tracked elsewhere (`docs/ROADMAP.md`). This brief is strictly about *correctness bugs*.
