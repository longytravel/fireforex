<!-- Plain-English description of what this PR changes. -->

## Summary

## Self-review checklist

Paste the output of the three pre-PR reviewers (`/simplify`, `/code-review`, Codex mini) below, then tick each box.

- [ ] Every changed function has a test (unit, integration, reference, or parity).
- [ ] No `==` on floats — uses `pytest.approx` or tolerance comparison.
- [ ] No `print()` or debug logging left in.
- [ ] No silently-changed parameter defaults. If any default changed, it's called out here.
- [ ] No new `TODO` / `FIXME` comments — opened an issue instead.
- [ ] `CLAUDE.md` / rules / `PROGRESS.md` updated if the change touches them.
- [ ] `pytest tests/` passed locally.
- [ ] `cargo test` passed locally if Rust was touched, and `maturin develop --release` rebuilt the `.pyd`.
- [ ] `pre-commit run --all-files` passed locally.
- [ ] Parity harness still matches if live-runner or signal-variant code was touched.

## Review outputs

<!-- Paste /simplify, /code-review, and Codex mini outputs here. -->
