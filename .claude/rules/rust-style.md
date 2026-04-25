---
description: Rust style rules for the ff_core engine
paths: ["core/**/*.rs"]
---

# Rust style — ff_core

## Hard rules
- After any change in `core/src/`, rebuild with `.\.venv\Scripts\maturin.exe develop --release` from repo root.
- `cargo fmt --check` and `cargo clippy --all-targets -- -D warnings` must pass before opening a PR.
- No `unwrap()` or `expect()` in library code on user-supplied inputs. Return `Result<_, _>`.
- Float comparisons must respect tolerance (`(a - b).abs() < eps`), never `==`.

## Soft preferences
- Prefer `f64` for price math; `i64` for bar indices and trade counts.
- Keep the pyo3 boundary thin: `batch_evaluate` takes plain vectors in, returns plain vectors out — no borrow lifetime exotica.
- Shape changes to the Rust↔Python contract MUST be called out in the PR description AND tested against a pinned NPZ reference.
