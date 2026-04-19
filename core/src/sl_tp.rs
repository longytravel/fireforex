/// SL/TP price computation — mirrors _compute_sl_tp() from jit_loop.py.

use crate::constants::*;

/// Computed SL/TP result.
pub struct SlTpResult {
    pub sl_price: f64,
    pub tp_price: f64,
    pub sl_pips: f64,
    pub tp_pips: f64,
}

/// Compute SL and TP prices from parameters.
#[inline(always)]
pub fn compute_sl_tp(
    direction: i64,
    entry_price: f64,
    atr_pips: f64,
    pip_value: f64,
    sl_mode: i64,
    sl_fixed_pips: f64,
    sl_atr_mult: f64,
    swing_sl_price: f64, // NaN if not available
    tp_mode: i64,
    tp_rr_ratio: f64,
    tp_atr_mult: f64,
    tp_fixed_pips: f64,
) -> SlTpResult {
    let atr_price = atr_pips * pip_value;
    let is_buy = direction == DIR_BUY;

    // --- Stop Loss ---
    let sl_distance = if sl_mode == SL_ATR_BASED {
        atr_price * sl_atr_mult
    } else if sl_mode == SL_SWING {
        if !swing_sl_price.is_nan() {
            let dist = (entry_price - swing_sl_price).abs();
            let min_sl = 5.0 * pip_value;
            if dist < min_sl { min_sl } else { dist }
        } else {
            atr_price * 1.5 // Fallback
        }
    } else {
        // SL_FIXED_PIPS
        sl_fixed_pips * pip_value
    };

    let sl_pips = sl_distance / pip_value;

    let sl_price = if is_buy {
        entry_price - sl_distance
    } else {
        entry_price + sl_distance
    };

    // --- Take Profit ---
    let mut tp_distance = if tp_mode == TP_ATR_BASED {
        atr_price * tp_atr_mult
    } else if tp_mode == TP_FIXED_PIPS {
        tp_fixed_pips * pip_value
    } else {
        // TP_RR_RATIO
        sl_distance * tp_rr_ratio
    };

    // Enforce TP >= SL
    if tp_distance < sl_distance {
        tp_distance = sl_distance;
    }

    let tp_pips = tp_distance / pip_value;

    let tp_price = if is_buy {
        entry_price + tp_distance
    } else {
        entry_price - tp_distance
    };

    SlTpResult {
        sl_price,
        tp_price,
        sl_pips,
        tp_pips,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fixed_sl_tp_buy() {
        let r = compute_sl_tp(
            DIR_BUY, 1.10000, 10.0, 0.0001,
            SL_FIXED_PIPS, 20.0, 0.0, f64::NAN,
            TP_FIXED_PIPS, 0.0, 0.0, 40.0,
        );
        assert!((r.sl_price - 1.09800).abs() < 1e-10);
        assert!((r.tp_price - 1.10400).abs() < 1e-10);
        assert!((r.sl_pips - 20.0).abs() < 1e-10);
        assert!((r.tp_pips - 40.0).abs() < 1e-10);
    }

    #[test]
    fn test_atr_sl_rr_tp_sell() {
        let r = compute_sl_tp(
            DIR_SELL, 1.10000, 10.0, 0.0001,
            SL_ATR_BASED, 0.0, 1.5, f64::NAN,
            TP_RR_RATIO, 2.0, 0.0, 0.0,
        );
        // SL = 10 * 1.5 = 15 pips → sell SL = 1.10000 + 0.0015 = 1.10150
        assert!((r.sl_price - 1.10150).abs() < 1e-10);
        assert!((r.sl_pips - 15.0).abs() < 1e-10);
        // TP = 15 * 2.0 = 30 pips → sell TP = 1.10000 - 0.0030 = 1.09700
        assert!((r.tp_price - 1.09700).abs() < 1e-10);
        assert!((r.tp_pips - 30.0).abs() < 1e-10);
    }

    #[test]
    fn test_tp_enforced_gte_sl() {
        // TP ratio of 0.5 would give TP < SL, should be clamped to SL
        let r = compute_sl_tp(
            DIR_BUY, 1.10000, 10.0, 0.0001,
            SL_FIXED_PIPS, 20.0, 0.0, f64::NAN,
            TP_RR_RATIO, 0.5, 0.0, 0.0,
        );
        assert!((r.tp_pips - r.sl_pips).abs() < 1e-10);
    }

    #[test]
    fn test_swing_sl_with_min() {
        // Swing SL very close to entry → min 5 pips
        let r = compute_sl_tp(
            DIR_BUY, 1.10000, 10.0, 0.0001,
            SL_SWING, 0.0, 0.0, 1.09998, // 0.2 pips from entry
            TP_RR_RATIO, 2.0, 0.0, 0.0,
        );
        assert!((r.sl_pips - 5.0).abs() < 1e-10); // clamped to min
    }
}
