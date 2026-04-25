**Must fix**
- [`.github/workflows/pr-checklist.yml`](C:/Users/ROG/Projects/Fire Forex/.github/workflows/pr-checklist.yml#L11): `dependabot[bot]` is the right bot name, but `github.actor` is the wrong identity if the intent is “skip Dependabot-authored PRs.” On `edited` or rerun paths a human can be the actor. Use `github.event.pull_request.user.login`.
- [`pyproject.toml`](C:/Users/ROG/Projects/Fire Forex/pyproject.toml#L37): global `F841` is too lax. It hides dead code repo-wide, not just intentional scratch variables. `E402`/`E701`/`E702` are also broad enough that I’d only keep them per-file.
- [`.pre-commit-config.yaml`](C:/Users/ROG/Projects/Fire Forex/.pre-commit-config.yaml#L14): removing `trailing-whitespace`, `end-of-file-fixer`, and `mixed-line-ending` drops cheap hygiene guarantees. If pre-commit must be check-only, add non-mutating equivalents elsewhere.

**Should fix**
- [`tests/test_signal_cache.py`](C:/Users/ROG/Projects/Fire Forex/tests/test_signal_cache.py#L7): the import fixes F821; unquote `-> Path` for clarity.
- [`pyproject.toml`](C:/Users/ROG/Projects/Fire Forex/pyproject.toml#L21): `line-length = 140` is workable but wide.

**Nitpicks**
- [`.github/workflows/ci.yml`](C:/Users/ROG/Projects/Fire Forex/.github/workflows/ci.yml#L19): the Rust/Python setup looks correct. Maturin explicitly supports `pip install -e .` for editable installs, and PyO3 looks for an active venv, then `python`, then `python3`. Sources: [maturin local development](https://www.maturin.rs/local_development), [PyO3 build docs](https://docs.rs/crate/pyo3/0.22.4/source/guide/src/building-and-distribution.md).
- `E741` is fine for OHLC naming.

**Verdict**
- changes-requested
