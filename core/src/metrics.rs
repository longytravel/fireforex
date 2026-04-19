/// Metric computation — mirrors _compute_metrics_inline() from jit_loop.py.

use crate::constants::*;

/// Standard-normal CDF via Abramowitz & Stegun 7.1.26 erf approximation.
/// Max absolute error ~1.5e-7 — sufficient for PSR ranking.
#[inline]
fn norm_cdf(x: f64) -> f64 {
    let a1 =  0.254829592_f64;
    let a2 = -0.284496736_f64;
    let a3 =  1.421413741_f64;
    let a4 = -1.453152027_f64;
    let a5 =  1.061405429_f64;
    let p  =  0.3275911_f64;
    let z = x / std::f64::consts::SQRT_2;
    let sign = if z < 0.0 { -1.0 } else { 1.0 };
    let az = z.abs();
    let t = 1.0 / (1.0 + p * az);
    let y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * (-az * az).exp();
    0.5 * (1.0 + sign * y)
}

/// Compute all metrics inline for one trial. Writes to metrics_row (len = NUM_METRICS).
#[inline(always)]
pub fn compute_metrics_inline(
    pnl_arr: &[f64],
    trade_count: usize,
    avg_sl_pips: f64,
    n_bars: usize,
    bars_per_year: f64,
    metrics_row: &mut [f64],
) {
    let n = trade_count;
    metrics_row[M_TRADES] = n as f64;

    // Placeholders — data not yet tracked by engine.
    metrics_row[M_AVG_HOLD_BARS] = f64::NAN;
    metrics_row[M_TRADES_PER_DAY] = f64::NAN;
    // DSR is finalised in Python (needs sweep-wide n_trials); leave 0.0 here.

    if n == 0 {
        return;
    }

    // Win rate
    let mut wins = 0usize;
    for i in 0..n {
        if pnl_arr[i] > 0.0 {
            wins += 1;
        }
    }
    metrics_row[M_WIN_RATE] = wins as f64 / n as f64;

    // Profit factor
    let mut gross_profit = 0.0_f64;
    let mut gross_loss = 0.0_f64;
    let mut total_pnl = 0.0_f64;
    for i in 0..n {
        let p = pnl_arr[i];
        total_pnl += p;
        if p > 0.0 {
            gross_profit += p;
        } else if p < 0.0 {
            gross_loss -= p; // Make positive
        }
    }
    let pf = if gross_loss == 0.0 {
        if gross_profit > 0.0 { 10.0 } else { 0.0 }
    } else {
        gross_profit / gross_loss
    };
    metrics_row[M_PROFIT_FACTOR] = pf;

    // Mean and higher moments (second pass — also needed for skew/kurt used by PSR)
    let mean = total_pnl / n as f64;
    let mut var_sum = 0.0_f64;
    let mut m3_sum = 0.0_f64;
    let mut m4_sum = 0.0_f64;
    let mut down_sq_sum = 0.0_f64;
    let mut down_count = 0usize;
    // Streak tracking for Max Consecutive Losses.
    let mut cur_loss_streak: usize = 0;
    let mut max_loss_streak: usize = 0;
    for i in 0..n {
        let p = pnl_arr[i];
        let diff = p - mean;
        let d2 = diff * diff;
        var_sum += d2;
        m3_sum += d2 * diff;
        m4_sum += d2 * d2;
        if p < 0.0 {
            down_sq_sum += p * p;
            down_count += 1;
            cur_loss_streak += 1;
            if cur_loss_streak > max_loss_streak {
                max_loss_streak = cur_loss_streak;
            }
        } else {
            cur_loss_streak = 0;
        }
    }

    let std = if n > 1 {
        (var_sum / (n - 1) as f64).sqrt()
    } else {
        0.0
    };

    metrics_row[M_MAX_CONSEC_LOSS] = max_loss_streak as f64;

    // Annualization factor
    let ann_factor = if n_bars > 0 && bars_per_year > 0.0 {
        n as f64 * bars_per_year / n_bars as f64
    } else {
        (n as f64).min(252.0)
    };

    // Sharpe (annualized)
    metrics_row[M_SHARPE] = if std > 0.0 {
        (mean / std) * ann_factor.sqrt()
    } else {
        0.0
    };

    // Sortino (annualized)
    if down_count > 0 {
        let downside_std = (down_sq_sum / down_count as f64).sqrt();
        metrics_row[M_SORTINO] = if downside_std > 0.0 {
            (mean / downside_std) * ann_factor.sqrt()
        } else {
            0.0
        };
    } else {
        metrics_row[M_SORTINO] = if mean > 0.0 { 10.0 } else { 0.0 };
    }

    // Expectancy (pips = raw mean; R = mean / avg_sl_pips)
    metrics_row[M_EXPECTANCY_PIPS] = mean;
    metrics_row[M_EXPECTANCY_R] = if avg_sl_pips > 0.0 { mean / avg_sl_pips } else { 0.0 };

    // SQN (Van Tharp) — t-stat of per-trade returns. Scale-invariant, so equivalent
    // whether computed on raw pnl or R-multiples (avg_sl_pips cancels).
    metrics_row[M_SQN] = if std > 0.0 { (n as f64).sqrt() * mean / std } else { 0.0 };

    // Omega(τ=0): Σmax(r,0)/Σmax(-r,0) — on a per-trade distribution this equals PF.
    // Kept as a distinct column so τ can be plumbed through later without schema churn.
    metrics_row[M_OMEGA] = pf;

    // Equity curve for MaxDD, R², Ulcer
    let mut equity_peak = 0.0_f64;
    let mut max_dd = 0.0_f64;
    let mut equity = 0.0_f64;
    let mut base_val = 0.0_f64;
    let mut sum_sq_dd = 0.0_f64;

    // For R²
    let mut sum_x = 0.0_f64;
    let mut sum_y = 0.0_f64;
    let mut sum_xy = 0.0_f64;
    let mut sum_xx = 0.0_f64;

    for i in 0..n {
        equity += pnl_arr[i];
        if equity > equity_peak {
            equity_peak = equity;
        }
        let dd = equity_peak - equity;
        if dd > max_dd {
            max_dd = dd;
        }

        // Track peak for Ulcer base
        if equity.abs() > base_val {
            base_val = equity.abs();
        }
        if equity_peak > base_val {
            base_val = equity_peak;
        }

        // R² accumulators
        let x = i as f64;
        sum_x += x;
        sum_y += equity;
        sum_xy += x * equity;
        sum_xx += x * x;

        // Ulcer: percentage drawdown squared
        let pct_dd = if base_val > 0.0 {
            (dd / base_val) * 100.0
        } else {
            0.0
        };
        sum_sq_dd += pct_dd * pct_dd;
    }

    // Max DD %
    if base_val <= 0.0 {
        base_val = 1.0;
    }
    metrics_row[M_MAX_DD_PCT] = (max_dd / base_val) * 100.0;

    // Return %
    metrics_row[M_RETURN_PCT] = if avg_sl_pips > 0.0 {
        (total_pnl / avg_sl_pips) * 100.0
    } else {
        0.0
    };

    // R² + K-Ratio (reuse same regression accumulators).
    // R² measures linearity of cumulative equity; K-Ratio measures the statistical
    // significance of the upward slope. Complementary: a straight line of 3 trades
    // can score R²≈1 with K-Ratio tiny.
    let mut k_ratio = 0.0_f64;
    if n >= 2 {
        let x_mean = sum_x / n as f64;
        let y_mean = sum_y / n as f64;
        let ss_xy = sum_xy - n as f64 * x_mean * y_mean;
        let ss_xx = sum_xx - n as f64 * x_mean * x_mean;
        if ss_xx > 0.0 {
            let slope = ss_xy / ss_xx;
            let intercept = y_mean - slope * x_mean;
            let mut ss_res = 0.0_f64;
            let mut ss_tot = 0.0_f64;
            let mut eq2 = 0.0_f64;
            for i in 0..n {
                eq2 += pnl_arr[i];
                let y_pred = slope * i as f64 + intercept;
                ss_res += (eq2 - y_pred).powi(2);
                ss_tot += (eq2 - y_mean).powi(2);
            }
            if ss_tot > 0.0 {
                let rsq = 1.0 - ss_res / ss_tot;
                metrics_row[M_R_SQUARED] = rsq.max(0.0);
            }
            // Kestner 2013 K-Ratio: slope / (stderr_slope * sqrt(n)).
            if n > 2 {
                let residual_var = ss_res / (n - 2) as f64;
                let stderr_slope = (residual_var / ss_xx).sqrt();
                if stderr_slope > 0.0 {
                    k_ratio = slope / (stderr_slope * (n as f64).sqrt());
                }
            }
        }
    }
    metrics_row[M_KRATIO] = k_ratio;

    // Ulcer Index
    metrics_row[M_ULCER] = (sum_sq_dd / n as f64).sqrt();

    // Calmar — annualised return in pips / |max_dd|. max_dd here is in raw pnl units.
    metrics_row[M_CALMAR] = if max_dd > 0.0 && n_bars > 0 && bars_per_year > 0.0 {
        let annual_pnl = total_pnl * bars_per_year / n_bars as f64;
        annual_pnl / max_dd
    } else { 0.0 };

    // Recovery Factor — non-annualised Calmar. Intuitive UI value.
    metrics_row[M_RECOVERY] = if max_dd > 0.0 { total_pnl / max_dd } else { 0.0 };

    // UPI / Martin Ratio — return per unit of ulcer pain.
    let ulc_val = metrics_row[M_ULCER];
    metrics_row[M_UPI] = if ulc_val > 0.0 { metrics_row[M_RETURN_PCT] / ulc_val } else { 0.0 };

    // Tail Ratio — |P95| / |P5|. Catches negative skew.
    // Needs a partial sort; allocate a local copy to avoid mutating the caller's slice.
    if n >= 20 {
        let mut sorted: Vec<f64> = pnl_arr[..n].to_vec();
        let p5_idx = ((n as f64 - 1.0) * 0.05).round() as usize;
        let p95_idx = ((n as f64 - 1.0) * 0.95).round() as usize;
        sorted.select_nth_unstable_by(p5_idx, |a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let p5 = sorted[p5_idx];
        sorted.select_nth_unstable_by(p95_idx, |a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let p95 = sorted[p95_idx];
        metrics_row[M_TAIL_RATIO] = if p5.abs() > 0.0 { p95.abs() / p5.abs() } else { 0.0 };
    }

    // PSR — Lopez de Prado Probabilistic Sharpe Ratio, threshold SR*=0.
    // Uses unannualised Sharpe + skew + excess kurtosis of per-trade returns.
    if n >= 4 && std > 0.0 {
        let sr = mean / std; // non-annualised
        let m2 = var_sum / n as f64;
        if m2 > 0.0 {
            let m2_15 = m2.sqrt() * m2; // m2^1.5
            let gamma3 = (m3_sum / n as f64) / m2_15; // skew
            let gamma4 = (m4_sum / n as f64) / (m2 * m2); // kurtosis (raw, not excess)
            let denom = (1.0 - gamma3 * sr + ((gamma4 - 1.0) / 4.0) * sr * sr).max(1e-12);
            let z = sr * ((n as f64 - 1.0).sqrt()) / denom.sqrt();
            metrics_row[M_PSR] = norm_cdf(z);
        }
    }

    // Quality — Codex-reviewed composite, replaces the legacy v1 formula
    // (which double-counted edge via Sortino·PF·ret_factor and drawdown via
    // Ulcer+DD%/2, and used R² which is drift-biased on cumulative equity).
    //
    //   quality = ln(1+Sortino) · clamp(K_Ratio/3, 0, 1) · min(PF,5) · trades_f
    //             ───────────────────────────────────────────────────────────
    //                                   Ulcer + 5
    //
    // ln(1+Sortino) compresses extreme ratios (near-zero-loss trials can hit
    // 100+); K-Ratio replaces R² so we reward significance of the up-slope,
    // not mere linearity; dropping ret_factor and DD%/2 removes the
    // double-counting Codex flagged in review (2026-04-19).
    if metrics_row[M_SORTINO] > 0.0 {
        let so_scaled = (1.0 + metrics_row[M_SORTINO]).ln();
        let k_norm = (k_ratio / 3.0).clamp(0.0, 1.0);
        let pf_c = pf.min(5.0);
        let trades_f = (n as f64).min(300.0) / 300.0 * 14.14;
        let denom = metrics_row[M_ULCER] + 5.0;
        if denom > 0.0 {
            let q = (so_scaled * k_norm * pf_c * trades_f) / denom;
            metrics_row[M_QUALITY] = q;
            metrics_row[M_QUALITY_V2] = q;  // alias slot — kept for NPZ schema stability.
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_zero_trades() {
        let pnl = vec![];
        let mut metrics = vec![0.0; NUM_METRICS];
        compute_metrics_inline(&pnl, 0, 30.0, 1000, 6048.0, &mut metrics);
        assert_eq!(metrics[M_TRADES], 0.0);
        assert_eq!(metrics[M_WIN_RATE], 0.0);
    }

    #[test]
    fn test_all_winners() {
        let pnl = vec![10.0, 20.0, 15.0];
        let mut metrics = vec![0.0; NUM_METRICS];
        compute_metrics_inline(&pnl, 3, 20.0, 1000, 6048.0, &mut metrics);
        assert_eq!(metrics[M_TRADES], 3.0);
        assert_eq!(metrics[M_WIN_RATE], 1.0);
        assert_eq!(metrics[M_PROFIT_FACTOR], 10.0); // no losses → capped at 10
        assert!(metrics[M_MAX_DD_PCT] == 0.0);
    }

    #[test]
    fn test_mixed_trades() {
        let pnl = vec![10.0, -5.0, 20.0, -3.0];
        let mut metrics = vec![0.0; NUM_METRICS];
        compute_metrics_inline(&pnl, 4, 15.0, 1000, 6048.0, &mut metrics);
        assert_eq!(metrics[M_TRADES], 4.0);
        assert_eq!(metrics[M_WIN_RATE], 0.5);
        // PF = 30 / 8 = 3.75
        assert!((metrics[M_PROFIT_FACTOR] - 3.75).abs() < 1e-10);
    }
}
