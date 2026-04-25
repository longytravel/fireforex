/// Full trade simulation — mirrors _simulate_trade_full() from jit_loop.py.
///
/// Includes trailing stop, breakeven, partial close, stale exit, max bars.
use crate::constants::*;
/// Result of simulating one trade through the engine.
/// Moved here from the (now-deleted) trade_basic module.
///
/// Extended for the live-parity validator: `pnl_pips` / `exit_reason` retained
/// for aggregate-metric bit-identity; the additional fields feed the per-trade
/// log that the reconciler joins against MT5 deals.
pub struct TradeResult {
    pub pnl_pips: f64,
    pub exit_reason: i64,
    pub direction: i64,
    pub entry_bar_index: i64,
    pub entry_sub_bar_index: i64,
    pub entry_price: f64,
    pub exit_bar_index: i64,
    pub exit_sub_bar_index: i64,
    pub exit_price: f64,
}

/// Simulate a single trade with full management features.
///
/// Returns (pnl_pips, exit_reason).
/// Uses sub-bar (M1) data for all price-sensitive management checks.
/// H1-level checks (max_bars, stale exit) remain at H1 bar resolution.
#[inline(always)]
#[allow(clippy::too_many_arguments)]
pub fn simulate_trade_full(
    direction: i64,
    entry_bar: usize,
    entry_price: f64,
    sl_price: f64,
    tp_price: f64,
    atr_pips: f64,
    high: &[f64],
    low: &[f64],
    close: &[f64],
    spread_arr: &[f64],
    pip_value: f64,
    slippage_pips: f64,
    num_bars: usize,
    // Management params
    trailing_mode: i64,
    trail_activate_pips: f64,
    trail_distance_pips: f64,
    trail_atr_mult: f64,
    breakeven_enabled: i64,
    breakeven_trigger_pips: f64,
    breakeven_offset_pips: f64,
    partial_enabled: i64,
    partial_pct: f64,
    partial_trigger_pips: f64,
    max_bars: i64,
    stale_enabled: i64,
    stale_bars: i64,
    stale_atr_thresh: f64,
    chandelier_enabled: i64,
    chandelier_activate_pips: f64,
    chandelier_atr_mult: f64,
    commission_pips: f64,
    // Sub-bar arrays
    sub_high: &[f64],
    sub_low: &[f64],
    sub_close: &[f64],
    sub_spread: &[f64],
    h1_to_sub_start: &[i64],
    h1_to_sub_end: &[i64],
) -> TradeResult {
    let is_buy = direction == DIR_BUY;
    let slippage_price = slippage_pips * pip_value;

    // Sub-bar index of the entry fill — first M1 bar after the signal bar close.
    // Needed universally (not only for buy) so the reconciler can look up a
    // live fill timestamp.
    let entry_sub_bar_idx: i64 = h1_to_sub_start[entry_bar];

    // Apply entry costs
    let actual_entry = if is_buy {
        let sub_entry_start = entry_sub_bar_idx as usize;
        let spread_at_entry = if sub_entry_start < sub_spread.len() {
            let s = sub_spread[sub_entry_start];
            if s.is_nan() {
                0.0
            } else {
                s
            }
        } else {
            0.0
        };
        entry_price + slippage_price + spread_at_entry
    } else {
        entry_price - slippage_price
    };

    let mut current_sl = sl_price;
    let mut position_pct = 1.0_f64;
    let mut partial_done = false;
    let mut be_locked = false;
    let mut trailing_active = false;
    let mut chandelier_active = false;
    let mut chandelier_peak_high = actual_entry;
    let mut chandelier_trough_low = actual_entry;
    let mut realized_pnl_pips = 0.0_f64;

    // Deferred SL pattern
    let mut pending_sl = -1.0_f64;
    let mut pending_be_locked = false;
    let mut pending_trailing_active = false;
    let mut pending_chandelier_active = false;
    let mut has_pending_update = false;

    let mut bars_held: i64 = 0;
    let mut exit_reason = EXIT_NONE;
    let mut exit_bar = num_bars - 1;
    let mut exit_sub_idx: i64 = -1;
    let mut exit_price: f64 = 0.0;
    let mut final_pnl = 0.0_f64;

    'bar_loop: for bar in (entry_bar + 1)..num_bars {
        let _bar_high = high[bar];
        let _bar_low = low[bar];
        let bar_close = close[bar];
        bars_held += 1;

        // --- H1-level checks (max_bars, stale) ---
        if max_bars > 0 && bars_held >= max_bars {
            let pnl = if is_buy {
                (bar_close - slippage_price - actual_entry) / pip_value * position_pct
            } else {
                (actual_entry - bar_close - slippage_price) / pip_value * position_pct
            };
            final_pnl = realized_pnl_pips + pnl;
            exit_reason = EXIT_MAX_BARS;
            exit_bar = bar;
            exit_price = bar_close;
            break 'bar_loop;
        }

        if stale_enabled > 0 && bars_held >= stale_bars {
            let lookback_start = if (entry_bar as i64 + 1) > (bar as i64 - stale_bars + 1) {
                entry_bar + 1
            } else {
                (bar as i64 - stale_bars + 1) as usize
            };
            let mut max_range = 0.0_f64;
            for b in lookback_start..=bar {
                let r = (high[b] - low[b]) / pip_value;
                if r > max_range {
                    max_range = r;
                }
            }
            if max_range < stale_atr_thresh * atr_pips {
                let pnl = if is_buy {
                    (bar_close - slippage_price - actual_entry) / pip_value * position_pct
                } else {
                    (actual_entry - bar_close - slippage_price) / pip_value * position_pct
                };
                final_pnl = realized_pnl_pips + pnl;
                exit_reason = EXIT_STALE;
                exit_bar = bar;
                exit_price = bar_close;
                break 'bar_loop;
            }
        }

        // --- Sub-bar trade management ---
        let sub_start = h1_to_sub_start[bar] as usize;
        let sub_end = (h1_to_sub_end[bar] as usize).min(sub_high.len());

        for sb in sub_start..sub_end {
            let sb_high = sub_high[sb];
            let sb_low = sub_low[sb];
            let sb_close = sub_close[sb];

            // Apply any pending SL modification from the PREVIOUS sub-bar
            if has_pending_update {
                if pending_sl > 0.0 {
                    current_sl = pending_sl;
                }
                be_locked = pending_be_locked;
                trailing_active = pending_trailing_active;
                chandelier_active = pending_chandelier_active;
                pending_sl = -1.0;
                has_pending_update = false;
            }

            // Current floating PnL on this sub-bar
            let (float_pnl_pips, _worst_pnl_pips) = if is_buy {
                (
                    (sb_high - actual_entry) / pip_value,
                    (sb_low - actual_entry) / pip_value,
                )
            } else {
                (
                    (actual_entry - sb_low) / pip_value,
                    (actual_entry - sb_high) / pip_value,
                )
            };

            // --- Breakeven lock (deferred) ---
            // Two guards must hold for the BE move to be accepted:
            //   1. Monotonicity: the new SL must be tighter than the current
            //      SL (move forward, never backward).
            //   2. Side-of-price: the new SL must be on the correct side of
            //      the current price. For a long, SL must be strictly below
            //      the confirmed close (sb_close); for a short, strictly
            //      above. Without this, `offset > trigger` writes an SL past
            //      current price and the next sub-bar exits for `+offset`
            //      pips — producing unearned wins (see
            //      docs/validation/2026-04-19-breakeven-offset/).
            if breakeven_enabled > 0 && !be_locked && !pending_be_locked {
                if float_pnl_pips >= breakeven_trigger_pips {
                    let be_price = if is_buy {
                        actual_entry + breakeven_offset_pips * pip_value
                    } else {
                        actual_entry - breakeven_offset_pips * pip_value
                    };
                    let accept = if is_buy {
                        be_price > current_sl && be_price < sb_close
                    } else {
                        be_price < current_sl && be_price > sb_close
                    };
                    if accept {
                        pending_sl = be_price;
                        pending_be_locked = true;
                        pending_trailing_active = trailing_active;
                        has_pending_update = true;
                    }
                }
            }

            // --- Trailing stop (deferred) ---
            if trailing_mode != TRAIL_OFF {
                if !trailing_active && !pending_trailing_active {
                    if float_pnl_pips >= trail_activate_pips {
                        pending_trailing_active = true;
                        let trail_dist = if trailing_mode == TRAIL_FIXED_PIP {
                            trail_distance_pips * pip_value
                        } else {
                            // TRAIL_ATR_CHANDELIER
                            trail_atr_mult * atr_pips * pip_value
                        };
                        // Side-of-price guard (activation). Same rationale as
                        // the breakeven fix: a trailing SL written on the wrong
                        // side of sb_close fires immediately on the next
                        // sub-bar, producing an unearned +trail_dist pip win.
                        // See docs/validation/2026-04-19-trailing/.
                        if is_buy {
                            let new_sl = sb_high - trail_dist;
                            let effective_sl = if has_pending_update && pending_sl > 0.0 {
                                pending_sl
                            } else {
                                current_sl
                            };
                            if new_sl > effective_sl && new_sl < sb_close {
                                pending_sl = new_sl;
                            }
                        } else {
                            let new_sl = sb_low + trail_dist;
                            let effective_sl = if has_pending_update && pending_sl > 0.0 {
                                pending_sl
                            } else {
                                current_sl
                            };
                            if new_sl < effective_sl && new_sl > sb_close {
                                pending_sl = new_sl;
                            }
                        }
                        pending_be_locked = if !has_pending_update {
                            be_locked
                        } else {
                            pending_be_locked
                        };
                        has_pending_update = true;
                    }
                }

                if trailing_active {
                    let trail_dist = if trailing_mode == TRAIL_FIXED_PIP {
                        trail_distance_pips * pip_value
                    } else {
                        trail_atr_mult * atr_pips * pip_value
                    };

                    // Side-of-price guard (ongoing trail). Mirrors the
                    // activation guard above.
                    if is_buy {
                        let new_sl = sb_high - trail_dist;
                        let effective_sl = if has_pending_update && pending_sl > 0.0 {
                            pending_sl
                        } else {
                            current_sl
                        };
                        if new_sl > effective_sl && new_sl < sb_close {
                            pending_sl = new_sl;
                            pending_be_locked = if !has_pending_update {
                                be_locked
                            } else {
                                pending_be_locked
                            };
                            pending_trailing_active = true;
                            has_pending_update = true;
                        }
                    } else {
                        let new_sl = sb_low + trail_dist;
                        let effective_sl = if has_pending_update && pending_sl > 0.0 {
                            pending_sl
                        } else {
                            current_sl
                        };
                        if new_sl < effective_sl && new_sl > sb_close {
                            pending_sl = new_sl;
                            pending_be_locked = if !has_pending_update {
                                be_locked
                            } else {
                                pending_be_locked
                            };
                            pending_trailing_active = true;
                            has_pending_update = true;
                        }
                    }
                }
            }

            // --- Chandelier stop (deferred) ---
            // Peak-anchored ATR trailing distinct from the trailing block
            // above. SL = peak_high - atr_mult*atr (long) or
            // trough_low + atr_mult*atr (short), ratcheting one-way.
            // Side-of-price guard mirrors the v2 trailing fix:
            //   raw_sl < sb_low (long) / raw_sl > sb_high (short) before
            //   the SL is adopted — without it, arming on a spike bar
            //   fills the trade for an unearned +atr_mult*atr pip win on
            //   the same sub-bar. See docs/builds/2026-04-19-chandelier-stop/.
            if chandelier_enabled != 0
                && chandelier_atr_mult > 0.0
                && chandelier_activate_pips >= 0.0
            {
                // Track peak / trough every sub-bar regardless of arming.
                if is_buy {
                    if sb_high > chandelier_peak_high {
                        chandelier_peak_high = sb_high;
                    }
                } else {
                    if sb_low < chandelier_trough_low {
                        chandelier_trough_low = sb_low;
                    }
                }

                let armed_now = chandelier_active
                    || pending_chandelier_active
                    || float_pnl_pips >= chandelier_activate_pips;

                if armed_now {
                    let chand_dist = chandelier_atr_mult * atr_pips * pip_value;
                    if is_buy {
                        let new_sl = chandelier_peak_high - chand_dist;
                        let effective_sl = if has_pending_update && pending_sl > 0.0 {
                            pending_sl
                        } else {
                            current_sl
                        };
                        if new_sl > effective_sl && new_sl < sb_low {
                            pending_sl = new_sl;
                            pending_be_locked = if !has_pending_update {
                                be_locked
                            } else {
                                pending_be_locked
                            };
                            pending_trailing_active = if !has_pending_update {
                                trailing_active
                            } else {
                                pending_trailing_active
                            };
                            pending_chandelier_active = true;
                            has_pending_update = true;
                        } else if !has_pending_update {
                            pending_chandelier_active = pending_chandelier_active
                                || chandelier_active
                                || float_pnl_pips >= chandelier_activate_pips;
                            if pending_chandelier_active {
                                pending_be_locked = be_locked;
                                pending_trailing_active = trailing_active;
                                has_pending_update = true;
                            }
                        } else {
                            pending_chandelier_active = true;
                        }
                    } else {
                        let new_sl = chandelier_trough_low + chand_dist;
                        let effective_sl = if has_pending_update && pending_sl > 0.0 {
                            pending_sl
                        } else {
                            current_sl
                        };
                        if new_sl < effective_sl && new_sl > sb_high {
                            pending_sl = new_sl;
                            pending_be_locked = if !has_pending_update {
                                be_locked
                            } else {
                                pending_be_locked
                            };
                            pending_trailing_active = if !has_pending_update {
                                trailing_active
                            } else {
                                pending_trailing_active
                            };
                            pending_chandelier_active = true;
                            has_pending_update = true;
                        } else if !has_pending_update {
                            pending_chandelier_active = pending_chandelier_active
                                || chandelier_active
                                || float_pnl_pips >= chandelier_activate_pips;
                            if pending_chandelier_active {
                                pending_be_locked = be_locked;
                                pending_trailing_active = trailing_active;
                                has_pending_update = true;
                            }
                        } else {
                            pending_chandelier_active = true;
                        }
                    }
                }
            }

            // --- Partial close (immediate) ---
            // Two guards protect against non-physical fills (see
            // docs/validation/2026-04-19-partial-close/):
            //   (a) TP priority — if the TP sits closer to entry than the
            //       partial trigger and the TP is reachable this sub-bar,
            //       a real limit order at tp_price would fill first. Skip
            //       the partial so the TP block below can close the full
            //       position.
            //   (b) Realisation price — a partial fills at the trigger
            //       price (limit-order semantics), not at sb_close. The
            //       prior implementation used sb_close which over-stated
            //       pnl when the sub-bar closed above the trigger.
            if partial_enabled > 0 && !partial_done {
                if float_pnl_pips >= partial_trigger_pips {
                    let tp_pips_from_entry = if is_buy {
                        (tp_price - actual_entry) / pip_value
                    } else {
                        (actual_entry - tp_price) / pip_value
                    };
                    let tp_reachable_this_sub = if is_buy {
                        sb_high >= tp_price
                    } else {
                        sb_low <= tp_price
                    };
                    let tp_has_priority =
                        tp_reachable_this_sub && tp_pips_from_entry < partial_trigger_pips;
                    if !tp_has_priority {
                        partial_done = true;
                        let close_pct = partial_pct / 100.0;
                        // Limit-order fill at the trigger price.
                        let partial_pnl = (partial_trigger_pips - slippage_pips) * close_pct;
                        // Sell-side ask spread for the closing fraction on a short.
                        let partial_spread_cost = if !is_buy {
                            let sb_spread = if sb < sub_spread.len() {
                                let s = sub_spread[sb];
                                if s.is_nan() {
                                    0.0
                                } else {
                                    s
                                }
                            } else {
                                0.0
                            };
                            sb_spread / pip_value * close_pct
                        } else {
                            0.0
                        };
                        realized_pnl_pips += partial_pnl - partial_spread_cost;
                        position_pct -= close_pct;
                    }
                }
            }

            // --- Check SL (uses current_sl) ---
            if is_buy {
                if sb_low <= current_sl {
                    let pnl =
                        (current_sl - slippage_price - actual_entry) / pip_value * position_pct;
                    let exit_code = if chandelier_active {
                        EXIT_CHANDELIER
                    } else if trailing_active {
                        EXIT_TRAILING
                    } else if be_locked {
                        EXIT_BREAKEVEN
                    } else {
                        EXIT_SL
                    };
                    final_pnl = realized_pnl_pips + pnl;
                    exit_reason = exit_code;
                    exit_sub_idx = sb as i64;
                    exit_bar = bar;
                    exit_price = current_sl;
                    break 'bar_loop;
                }
                if sb_high >= tp_price {
                    let pnl = (tp_price - actual_entry) / pip_value * position_pct;
                    final_pnl = realized_pnl_pips + pnl;
                    exit_reason = EXIT_TP;
                    exit_sub_idx = sb as i64;
                    exit_bar = bar;
                    exit_price = tp_price;
                    break 'bar_loop;
                }
            } else {
                if sb_high >= current_sl {
                    let pnl =
                        (actual_entry - current_sl - slippage_price) / pip_value * position_pct;
                    let exit_code = if chandelier_active {
                        EXIT_CHANDELIER
                    } else if trailing_active {
                        EXIT_TRAILING
                    } else if be_locked {
                        EXIT_BREAKEVEN
                    } else {
                        EXIT_SL
                    };
                    final_pnl = realized_pnl_pips + pnl;
                    exit_reason = exit_code;
                    exit_sub_idx = sb as i64;
                    exit_bar = bar;
                    exit_price = current_sl;
                    break 'bar_loop;
                }
                if sb_low <= tp_price {
                    let pnl = (actual_entry - tp_price) / pip_value * position_pct;
                    final_pnl = realized_pnl_pips + pnl;
                    exit_reason = EXIT_TP;
                    exit_sub_idx = sb as i64;
                    exit_bar = bar;
                    exit_price = tp_price;
                    break 'bar_loop;
                }
            }
        }
    }

    // End of data — close remaining position
    if exit_reason == EXIT_NONE {
        // exit_bar is initialized to num_bars - 1, always in bounds
        let close_price = close[exit_bar];
        let pnl = if is_buy {
            (close_price - slippage_price - actual_entry) / pip_value * position_pct
        } else {
            (actual_entry - close_price - slippage_price) / pip_value * position_pct
        };
        final_pnl = realized_pnl_pips + pnl;
        exit_price = close_price;
    }

    // Apply execution costs — sell spread proportional to remaining position.
    // Also surface the spread-adjusted fill price on `exit_price` for the
    // live-parity trade log: shorts close by buying at ask (bid + spread), so
    // the raw bid-referenced `exit_price` understates the MT5 fill by one
    // spread. Longs already close at bid, so no adjustment needed for them.
    if !is_buy {
        let sell_spread = if exit_sub_idx >= 0 && (exit_sub_idx as usize) < sub_spread.len() {
            let s = sub_spread[exit_sub_idx as usize];
            if s.is_nan() {
                0.0
            } else {
                s
            }
        } else if exit_bar < spread_arr.len() {
            let s = spread_arr[exit_bar];
            if s.is_nan() {
                0.0
            } else {
                s
            }
        } else {
            0.0
        };
        final_pnl -= sell_spread / pip_value * position_pct;
        exit_price += sell_spread;
    }
    final_pnl -= commission_pips;

    TradeResult {
        pnl_pips: final_pnl,
        exit_reason,
        direction,
        entry_bar_index: entry_bar as i64,
        entry_sub_bar_index: entry_sub_bar_idx,
        entry_price: actual_entry,
        exit_bar_index: exit_bar as i64,
        exit_sub_bar_index: exit_sub_idx,
        exit_price,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_identity_mapping(n: usize) -> (Vec<i64>, Vec<i64>) {
        let start: Vec<i64> = (0..n as i64).collect();
        let end: Vec<i64> = (1..=n as i64).collect();
        (start, end)
    }

    #[test]
    fn test_max_bars_exit() {
        let high = vec![1.1010, 1.1020, 1.1030, 1.1040];
        let low = vec![1.0990, 1.0990, 1.0990, 1.0990];
        let close = vec![1.1000, 1.1010, 1.1020, 1.1030];
        let spread = vec![0.0; 4];
        let (start, end) = make_identity_mapping(4);

        let r = simulate_trade_full(
            DIR_BUY, 0, 1.1000, 1.0900, 1.1200, 10.0, &high, &low, &close, &spread, 0.0001, 0.0, 4,
            TRAIL_OFF, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0, 0.0, 0.0, 2, // max_bars = 2
            0, 0, 0.0, 0, 0.0, 0.0, // chandelier off
            0.0, &high, &low, &close, &spread, &start, &end,
        );
        assert_eq!(r.exit_reason, EXIT_MAX_BARS);
    }

    #[test]
    fn test_breakeven_deferred() {
        // BE trigger = 5 pips, offset = 2 pips
        // Bar 1 high triggers BE, but SL change applies from bar 2
        let high = vec![1.1000, 1.1010, 1.0990, 1.1020];
        let low = vec![1.0990, 1.0995, 1.0985, 1.0990];
        let close = vec![1.1000, 1.1005, 1.0990, 1.1010];
        let spread = vec![0.0; 4];
        let (start, end) = make_identity_mapping(4);

        let r = simulate_trade_full(
            DIR_BUY, 0, 1.1000, 1.0950, 1.1100, 10.0, &high, &low, &close, &spread, 0.0001, 0.0, 4,
            TRAIL_OFF, 0.0, 0.0, 0.0, 1, 5.0, 2.0, // BE enabled, trigger=5, offset=2
            0, 0.0, 0.0, 0, 0, 0, 0.0, 0, 0.0, 0.0, // chandelier off
            0.0, &high, &low, &close, &spread, &start, &end,
        );
        // Should NOT exit at bar 1 (deferred), and SL moved from 1.0950 to 1.1002
        assert!(r.exit_reason != EXIT_BREAKEVEN || r.pnl_pips >= 0.0);
    }
}
