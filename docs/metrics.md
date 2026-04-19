# Fire Forex metrics catalogue

Every trial in a sweep is summarised into **25 metric columns** computed by
the Rust engine (`core/src/metrics.rs`) plus `total_pips` derived Python-side
in the scatter endpoint. All columns are selectable as the scatter Y-axis,
and the "Jump to best" button picks the argmax of the currently-selected
metric (tie-broken by `return_pct` then `quality`).

The single source of truth for the column order is
`ff/harness.py::METRIC_COLUMNS` — a list of `(key, label, group)` tuples that
mirrors the `M_*` index constants in `core/src/constants.rs`.

## Groups

| Group          | Metrics                                                       |
| -------------- | ------------------------------------------------------------- |
| Activity       | `trades`, `win_rate`                                          |
| Return         | `profit_factor`, `return_pct`, `expectancy_r`, `expectancy_pips`, `omega`, `total_pips` |
| Risk-Adjusted  | `sharpe`, `sortino`, `r_squared`, `sqn`, `calmar`, `recovery`, `upi`, `k_ratio` |
| Risk           | `max_dd_pct`, `ulcer`, `tail_ratio`, `max_consec_loss`        |
| Overfit-Aware  | `psr`, `dsr`                                                  |
| Composite      | `quality` (Codex-reviewed formula; `quality_v2` is a hidden alias) |
| Forex          | `avg_hold_bars`, `trades_per_day` (both NaN placeholders)     |

## Column reference

### Activity
- **trades** — total number of trades opened by the trial.
- **win_rate** — fraction of trades with `pnl > 0`.

### Return (edge)
- **profit_factor** — `Σ(pnl>0) / Σ|pnl<0|`. Capped at 10 when there are no losers.
- **return_pct** — `total_pnl / avg_sl_pips × 100`. NOT account return — it's a unit-free R-multiple expressed as a percentage. Misnomer preserved for back-compat.
- **expectancy_r** — `mean(pnl) / avg_sl_pips`. Mean per-trade R-multiple. Unit-free, comparable across pairs.
- **expectancy_pips** — `mean(pnl)`. Forex-intuitive raw value.
- **omega** (τ=0) — `Σmax(r,0) / Σmax(-r,0)`. At τ=0 this is mathematically identical to Profit Factor on the per-trade distribution. Slot reserved for future configurable τ; at τ≠0 Omega decouples from PF and measures reward above a threshold return.
- **total_pips** — Python-side sum of each trial's pnl buffer. Appears last in the column list.

### Risk-Adjusted
- **sharpe** — annualised `mean / std`. Uses sample std (divisor n-1). Lo (2002) warned about annualisation under serial correlation — forex strategies often have it, so interpret with care.
- **sortino** — annualised `mean / downside_std` where downside_std is RMS of negative returns only.
- **r_squared** — coefficient of determination of cumulative equity vs time. 1.0 = perfectly linear rise. High R² is NOT the same as high significance — a 3-trade line can score R²≈1.
- **sqn** (Van Tharp) — `√N · mean(R) / std(R)`. Same as sample-t-stat of per-trade returns. Catches the low-N lucky trials that Sharpe misses.
- **calmar** — `annualised_pnl / |max_dd|`. Catches single catastrophic drawdowns.
- **recovery** — `total_pnl / |max_dd|`. Non-annualised Calmar.
- **upi** (Ulcer Performance Index / Martin Ratio) — `return_pct / Ulcer`. Rewards smooth recoveries — penalises both depth AND duration of drawdowns.
- **k_ratio** (Kestner 2013) — `slope / (stderr_of_slope × √N)` from a linear regression of cumulative equity. **Complementary to R²**: R² measures linearity, K-Ratio measures statistical significance of the up-slope. A straight line of 3 trades has high R² but low K-Ratio.

### Risk
- **max_dd_pct** — deepest peak-to-trough equity decline, as a percentage of peak equity.
- **ulcer** (Ulcer Index) — RMS of percentage drawdowns across the full equity curve. Penalises both depth and duration.
- **tail_ratio** — `|P95(pnl)| / |P5(pnl)|`. Catches negative skew ("pennies before the steamroller"). Only computed when n ≥ 20.
- **max_consec_loss** — longest consecutive losing-trade streak. Psychological tradability signal.

### Overfit-Aware (the point of this batch)
- **psr** (Probabilistic Sharpe Ratio, Lopez de Prado 2012) — the probability that the true Sharpe exceeds 0 given observed N, skew, and kurtosis. Values are probabilities in [0, 1]. Computed in Rust using the per-trade Sharpe (un-annualised) + higher moments via a single-pass Welford-style accumulator.
- **dsr** (Deflated Sharpe Ratio, Bailey & Lopez de Prado 2014) — PSR deflated by the expected maximum of `n_trials` independent standard-normal Sharpe ratios:

  ```
  E[max_N] ≈ (1-γ) Φ⁻¹(1 - 1/N) + γ Φ⁻¹(1 - 1/(N·e))
  ```

  where γ ≈ 0.5772 is the Euler–Mascheroni constant. Computed Python-side in `ff.harness._finalise_dsr` because it needs the sweep-wide `n_trials` scalar. **DSR is the recommended default objective for random sweeps** — it directly corrects the best-of-N selection bias that a system like Fire Forex courts.

### Composite
- **quality** — Codex-reviewed composite, replaced the broken v1 formula on 2026-04-19:

  ```
              ln(1+Sortino) · clamp(K_Ratio/3, 0, 1) · min(PF,5) · trades_factor
  quality = ──────────────────────────────────────────────────────────────────
                                   Ulcer + 5
  ```

  The previous formula `(ln(1+Sortino) · R² · min(PF,5) · trades_f · ret_f) / (Ulcer + DD%/2 + 5)` is gone — pinned baselines that scored against it will produce different numbers on the new engine. The rewrite fixes:
    1. **Double-counted edge** — Sortino · PF · ret_factor all rewarded the same positive mean; `ret_factor` is dropped.
    2. **Double-counted drawdown** — Ulcer already captures depth + duration, so `DD%/2` is dropped.
    3. **Drift-biased R²** — replaced with normalised K-Ratio, which measures the *statistical significance* of the up-slope rather than mere linearity. A 3-trade line can score R²≈1 with K-Ratio near zero.

- **quality_v2** — retained in the NPZ/API schema as an alias for `quality` (identical values). Hidden from the UI dropdown. Kept only so code reading the 25-column schema doesn't break; can be removed in a future engine version bump.

### Forex-specific (placeholders)
- **avg_hold_bars** — mean trade duration in bars. Currently `NaN`. Wiring requires adding per-trial bar-count tracking to the Rust hot loop and the NPZ schema; scoped for a follow-up PR.
- **trades_per_day** — same story.

## Objective selection contract

The Python harness exposes `pick_best(metrics_out, objective, constraints, tie_break)` so any caller — including the future Optuna optimiser — can select the best trial under an arbitrary objective with optional hard constraints:

```python
from ff.harness import pick_best

# Default: identical to legacy argmax(quality) + tie-break on return_pct.
best = pick_best(metrics_out)

# Constraint-filtered DSR objective.
best = pick_best(
    metrics_out,
    objective="dsr",
    constraints={"trades": {">=": 100}, "max_dd_pct": {"<=": 30}},
    tie_break=("return_pct", "trades"),
)
```

If no row passes the constraints, the function falls back to unconstrained argmax so a run always returns *something*. NaN in tie-break columns is skipped (the next tie-break key is tried).

## Appendix: why DSR is the right default for this system

Fire Forex runs **random parameter sweeps** — it samples N independent configurations and reports the best one. Each independent sample is a draw from the Sharpe-ratio distribution of the underlying strategy family; picking the max of N is a *selection bias* that inflates the observed Sharpe even when the true edge is zero.

Lopez de Prado (2014) shows that the expected maximum Sharpe of N independent zero-edge trials grows as roughly `σ · Φ⁻¹(1 - 1/N)`. For N = 2000 trials and σ_SR ≈ 1, a best-of-N Sharpe of ~3 looks impressive but is no stronger than chance. DSR deflates the observed PSR by exactly this expected-maximum correction, yielding a probability-scale score that stays meaningful under multiple-testing.

**Caveats that DSR does NOT fix:**
- Look-ahead bias or bad fills — the data must already be clean.
- Serial correlation in trade outcomes — Sharpe annualisation still needs care (see Lo 2002).
- Researcher optionality — picking which metrics to report is itself a source of bias.
- Regime-dependent edges — validate on walk-forward / out-of-sample windows.

Use DSR as *one* signal, not the *only* one.

## References

- Bailey, D. H. & Lopez de Prado, M. (2014). **The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality.** SSRN 2460551.
- Lopez de Prado, M. (2012). **The Sharpe Ratio Efficient Frontier.** Journal of Risk.
- Lo, A. (2002). **The Statistics of Sharpe Ratios.** Financial Analysts Journal.
- Kestner, L. (2013). **Quantitative Trading Strategies: Harnessing the Power of Quantitative Techniques.** McGraw-Hill.
- Van Tharp, R. **System Quality Number** (`SQN = √N · mean(R) / std(R)`).
- Keating, C. & Shadwick, W. (2002). **A Universal Performance Measure.**
