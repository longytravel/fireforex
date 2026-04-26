// These lints are allowed crate-wide because they conflict with the engine's
// intentional design choices (not because the warnings are wrong):
//   - too_many_arguments: simulate_trade_full takes ~30 plain scalars/slices
//     by design, to keep the pyo3 boundary thin and let Rust monomorphise
//     hot loops without a builder pattern.
//   - needless_range_loop: indexed access (`for i in 0..buf.len()`) is
//     load-bearing in numeric kernels where the loop body uses i for offset
//     arithmetic; rewriting as iterators obscures intent.
//   - collapsible_if / empty_line_after_doc_comments: style preference.
//   - dead_code: SL_FIXED_PIPS / TP_RR_RATIO / TRAIL_ATR_CHANDELIER / M_DSR
//     and `tp_pips` are constants/fields reserved for upcoming SL/TP and
//     metric variants. To be reviewed in the architecture stocktake.
#![allow(
    dead_code,
    clippy::too_many_arguments,
    clippy::needless_range_loop,
    clippy::collapsible_if,
    clippy::empty_line_after_doc_comments
)]

mod constants;
mod filter;
mod metrics;
mod sl_tp;
mod trade_full;

use numpy::{PyArray2, PyArrayMethods, PyReadonlyArray1, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::prelude::*;
use rayon::prelude::*;
use std::panic::{catch_unwind, AssertUnwindSafe};

use constants::*;
use filter::signal_passes_time_filter;
use metrics::compute_metrics_inline;
use sl_tp::compute_sl_tp;
use trade_full::simulate_trade_full;

/// Width of the per-trade record emitted into `trade_records` for the live
/// parity validator. Column order matches `TradeResult`:
/// [pnl_pips, exit_reason, direction, entry_bar_index, entry_sub_bar_index,
///  entry_price, exit_bar_index, exit_sub_bar_index, exit_price]
pub const NUM_TRADE_FIELDS: usize = 9;

/// Evaluate N parameter sets in parallel.
///
/// This is the Rust replacement for jit_loop.batch_evaluate().
/// Signature matches the Numba version exactly for drop-in replacement.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn batch_evaluate<'py>(
    py: Python<'py>,
    // Price data
    high: PyReadonlyArray1<'py, f64>,
    low: PyReadonlyArray1<'py, f64>,
    close: PyReadonlyArray1<'py, f64>,
    spread: PyReadonlyArray1<'py, f64>,
    pip_value: f64,
    slippage_pips: f64,
    // Signal data
    sig_bar_index: PyReadonlyArray1<'py, i64>,
    sig_direction: PyReadonlyArray1<'py, i64>,
    sig_entry_price: PyReadonlyArray1<'py, f64>,
    sig_hour: PyReadonlyArray1<'py, i64>,
    sig_day: PyReadonlyArray1<'py, i64>,
    sig_atr_pips: PyReadonlyArray1<'py, f64>,
    sig_swing_sl: PyReadonlyArray1<'py, f64>,
    sig_filter_value: PyReadonlyArray1<'py, f64>,
    sig_variant: PyReadonlyArray1<'py, i64>,
    // Generic signal filter arrays — shape (NUM_SIGNAL_PARAMS, n_signals), int64
    // Each row corresponds to PL_SIGNAL_P0..P9. Values of -1 mean "no filter".
    sig_filters: PyReadonlyArray2<'py, i64>,
    // Parameter matrix
    param_matrix: PyReadonlyArray2<'py, f64>,
    param_layout: PyReadonlyArray1<'py, i64>,
    // Output (mutable)
    metrics_out: &Bound<'py, PyArray2<f64>>,
    // Working memory
    max_trades: i64,
    bars_per_year: f64,
    // Execution costs
    commission_pips: f64,
    max_spread_pips: f64,
    // Sub-bar arrays
    sub_high: PyReadonlyArray1<'py, f64>,
    sub_low: PyReadonlyArray1<'py, f64>,
    sub_close: PyReadonlyArray1<'py, f64>,
    sub_spread: PyReadonlyArray1<'py, f64>,
    h1_to_sub_start: PyReadonlyArray1<'py, i64>,
    h1_to_sub_end: PyReadonlyArray1<'py, i64>,
    pnl_buffers: &Bound<'py, PyArray2<f64>>,
    trade_records: &Bound<'py, PyArray2<f64>>,
) -> PyResult<()> {
    // Get raw slices from numpy arrays (zero-copy)
    let high_s = high.as_slice()?;
    let low_s = low.as_slice()?;
    let close_s = close.as_slice()?;
    let spread_s = spread.as_slice()?;

    let sig_bar_index_s = sig_bar_index.as_slice()?;
    let sig_direction_s = sig_direction.as_slice()?;
    let sig_entry_price_s = sig_entry_price.as_slice()?;
    let sig_hour_s = sig_hour.as_slice()?;
    let sig_day_s = sig_day.as_slice()?;
    let sig_atr_pips_s = sig_atr_pips.as_slice()?;
    let sig_swing_sl_s = sig_swing_sl.as_slice()?;
    let sig_filter_value_s = sig_filter_value.as_slice()?;
    let sig_variant_s = sig_variant.as_slice()?;

    let param_matrix_s = param_matrix.as_slice()?;
    let param_layout_s = param_layout.as_slice()?;

    let sig_filters_s = sig_filters.as_slice()?;
    let n_filter_rows = sig_filters.shape()[0];
    let n_filter_cols = sig_filters.shape()[1];

    let sub_high_s = sub_high.as_slice()?;
    let sub_low_s = sub_low.as_slice()?;
    let sub_close_s = sub_close.as_slice()?;
    let sub_spread_s = sub_spread.as_slice()?;
    let h1_to_sub_start_s = h1_to_sub_start.as_slice()?;
    let h1_to_sub_end_s = h1_to_sub_end.as_slice()?;

    let n_trials = param_matrix.shape()[0];
    let n_params = param_matrix.shape()[1];
    let n_signals = sig_bar_index_s.len();
    let n_bars = high_s.len();
    let max_trades_usize = max_trades as usize;

    // --- Input validation (catch errors before entering unsafe/parallel code) ---
    if max_trades_usize == 0 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "max_trades must be > 0",
        ));
    }

    // Signal arrays must all have the same length
    if sig_direction_s.len() != n_signals
        || sig_entry_price_s.len() != n_signals
        || sig_hour_s.len() != n_signals
        || sig_day_s.len() != n_signals
        || sig_atr_pips_s.len() != n_signals
        || sig_swing_sl_s.len() != n_signals
        || sig_filter_value_s.len() != n_signals
        || sig_variant_s.len() != n_signals
    {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "Signal arrays must all have the same length",
        ));
    }

    // Generic signal filter array must be (NUM_SIGNAL_PARAMS, n_signals)
    if n_filter_rows != NUM_SIGNAL_PARAMS {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "sig_filters rows ({}) != NUM_SIGNAL_PARAMS ({})",
            n_filter_rows, NUM_SIGNAL_PARAMS
        )));
    }
    if n_filter_cols != n_signals {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "sig_filters cols ({}) != n_signals ({})",
            n_filter_cols, n_signals
        )));
    }

    // H1-to-sub mapping must match price array length
    if h1_to_sub_start_s.len() != n_bars || h1_to_sub_end_s.len() != n_bars {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "h1_to_sub mapping length ({},{}) != n_bars ({})",
            h1_to_sub_start_s.len(),
            h1_to_sub_end_s.len(),
            n_bars
        )));
    }

    // Sub-bar arrays must all have the same length
    let sub_len = sub_high_s.len();
    if sub_low_s.len() != sub_len || sub_close_s.len() != sub_len || sub_spread_s.len() != sub_len {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "Sub-bar arrays must all have the same length",
        ));
    }

    // Validate M1 index bounds (prevent OOB in sub-bar loops)
    if sub_len > 0 {
        for i in 0..n_bars {
            let start = h1_to_sub_start_s[i];
            let end = h1_to_sub_end_s[i];
            if start < 0 || end < 0 || (end as usize) > sub_len || start > end {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "h1_to_sub[{}] invalid: start={}, end={}, sub_len={}",
                    i, start, end, sub_len
                )));
            }
        }
    }

    // Validate param_layout indices are within bounds
    for i in 0..param_layout_s.len() {
        let col = param_layout_s[i];
        if col >= 0 && col as usize >= n_params {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "param_layout[{}]={} exceeds n_params {}",
                i, col, n_params
            )));
        }
    }

    // Fast path for mega sweeps: signal_lib stores signals as contiguous
    // chronological slices per variant. If the incoming array has that shape,
    // each trial can jump straight to its selected variant slice instead of
    // scanning the full pooled library and filtering most rows away. If a
    // legacy caller passes interleaved variants or -1 opt-out rows, fall back
    // to the original full-scan behavior below.
    let mut variant_slices_enabled = true;
    let mut max_variant_id: i64 = -1;
    for i in 0..n_signals {
        let v = sig_variant_s[i];
        if v < 0 {
            variant_slices_enabled = false;
            break;
        }
        if v > max_variant_id {
            max_variant_id = v;
        }
    }
    let mut variant_start: Vec<usize> = Vec::new();
    let mut variant_end: Vec<usize> = Vec::new();
    if variant_slices_enabled && max_variant_id >= 0 {
        let n_variants = (max_variant_id as usize) + 1;
        variant_start = vec![usize::MAX; n_variants];
        variant_end = vec![0; n_variants];
        let mut closed = vec![false; n_variants];
        let mut last_variant: i64 = -1;
        for i in 0..n_signals {
            let v = sig_variant_s[i];
            let vu = v as usize;
            if v != last_variant {
                if last_variant >= 0 {
                    closed[last_variant as usize] = true;
                }
                if closed[vu] {
                    variant_slices_enabled = false;
                    break;
                }
                if variant_start[vu] == usize::MAX {
                    variant_start[vu] = i;
                }
                last_variant = v;
            }
            variant_end[vu] = i + 1;
        }
        if variant_slices_enabled {
            for i in 0..variant_start.len() {
                if variant_start[i] == usize::MAX {
                    variant_start[i] = 0;
                    variant_end[i] = 0;
                }
            }
        } else {
            variant_start.clear();
            variant_end.clear();
        }
    } else {
        variant_slices_enabled = false;
    }

    // Validate trade_records shape: must be (n_trials, max_trades * NUM_TRADE_FIELDS)
    let trade_records_shape = trade_records.shape();
    let expected_cols = max_trades_usize * NUM_TRADE_FIELDS;
    if trade_records_shape[0] != n_trials || trade_records_shape[1] != expected_cols {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "trade_records shape must be ({}, {}), got ({}, {})",
            n_trials, expected_cols, trade_records_shape[0], trade_records_shape[1]
        )));
    }

    // Get mutable access to output arrays
    // SAFETY: We release the GIL below and use Rayon for parallelism.
    // Each trial writes to non-overlapping slices of metrics_out, pnl_buffers,
    // and trade_records.
    let metrics_out_ptr = unsafe { metrics_out.as_slice_mut()? };
    let pnl_buffers_ptr = unsafe { pnl_buffers.as_slice_mut()? };
    let trade_records_ptr = unsafe { trade_records.as_slice_mut()? };

    // Release the GIL and run in parallel
    py.allow_threads(|| {
        // Split output into per-trial chunks for non-overlapping writes
        let metrics_chunks: Vec<&mut [f64]> = metrics_out_ptr.chunks_mut(NUM_METRICS).collect();
        let pnl_chunks: Vec<&mut [f64]> = pnl_buffers_ptr.chunks_mut(max_trades_usize).collect();
        let trade_record_chunks: Vec<&mut [f64]> = trade_records_ptr
            .chunks_mut(max_trades_usize * NUM_TRADE_FIELDS)
            .collect();

        // Zip the mutable slices and iterate in parallel
        metrics_chunks
            .into_iter()
            .zip(pnl_chunks)
            .zip(trade_record_chunks)
            .enumerate()
            .collect::<Vec<_>>()
            .into_par_iter()
            .for_each(|(trial, ((metrics_row, pnl_buffer), trade_record_buf))| {
                // Save pointer for zeroing if trial panics
                let metrics_ptr = metrics_row.as_mut_ptr();
                let n_metrics = metrics_row.len();

                let result = catch_unwind(AssertUnwindSafe(|| {
                    // Get params for this trial (row-major indexing)
                    let params_offset = trial * n_params;
                    let params = &param_matrix_s[params_offset..params_offset + n_params];

                    // Extract standard params via layout
                    let sl_mode = params[param_layout_s[PL_SL_MODE] as usize] as i64;
                    let sl_fixed_pips = params[param_layout_s[PL_SL_FIXED_PIPS] as usize];
                    let sl_atr_mult = params[param_layout_s[PL_SL_ATR_MULT] as usize];
                    let tp_mode = params[param_layout_s[PL_TP_MODE] as usize] as i64;
                    let tp_rr_ratio = params[param_layout_s[PL_TP_RR_RATIO] as usize];
                    let tp_atr_mult = params[param_layout_s[PL_TP_ATR_MULT] as usize];
                    let tp_fixed_pips_val = params[param_layout_s[PL_TP_FIXED_PIPS] as usize];
                    let hours_start = params[param_layout_s[PL_HOURS_START] as usize] as i64;
                    let hours_end = params[param_layout_s[PL_HOURS_END] as usize] as i64;
                    let days_bitmask = params[param_layout_s[PL_DAYS_BITMASK] as usize] as i64;

                    // Management params
                    let trailing_mode = params[param_layout_s[PL_TRAILING_MODE] as usize] as i64;
                    let trail_activate = params[param_layout_s[PL_TRAIL_ACTIVATE] as usize];
                    let trail_distance = params[param_layout_s[PL_TRAIL_DISTANCE] as usize];
                    let trail_atr_m = params[param_layout_s[PL_TRAIL_ATR_MULT] as usize];
                    let be_enabled = params[param_layout_s[PL_BREAKEVEN_ENABLED] as usize] as i64;
                    let be_trigger = params[param_layout_s[PL_BREAKEVEN_TRIGGER] as usize];
                    let be_offset = params[param_layout_s[PL_BREAKEVEN_OFFSET] as usize];
                    let partial_en = params[param_layout_s[PL_PARTIAL_ENABLED] as usize] as i64;
                    let partial_pct = params[param_layout_s[PL_PARTIAL_PCT] as usize];
                    let partial_trig = params[param_layout_s[PL_PARTIAL_TRIGGER] as usize];
                    let max_bars_val = params[param_layout_s[PL_MAX_BARS] as usize] as i64;
                    let stale_en = params[param_layout_s[PL_STALE_ENABLED] as usize] as i64;
                    let stale_bars_val = params[param_layout_s[PL_STALE_BARS] as usize] as i64;
                    let stale_atr = params[param_layout_s[PL_STALE_ATR_THRESH] as usize];
                    let chandelier_en =
                        params[param_layout_s[PL_CHANDELIER_ENABLED] as usize] as i64;
                    let chandelier_activate =
                        params[param_layout_s[PL_CHANDELIER_ACTIVATE] as usize];
                    let chandelier_atr_m = params[param_layout_s[PL_CHANDELIER_ATR_MULT] as usize];

                    // Strategy-specific signal filter params
                    let variant_col = param_layout_s[PL_SIGNAL_VARIANT];
                    let trial_variant = if variant_col >= 0 {
                        params[variant_col as usize] as i64
                    } else {
                        -1
                    };
                    let bfm_col = param_layout_s[PL_BUY_FILTER_MAX];
                    let buy_filter_max = if bfm_col >= 0 {
                        params[bfm_col as usize]
                    } else {
                        -1.0
                    };
                    let sfm_col = param_layout_s[PL_SELL_FILTER_MIN];
                    let sell_filter_min = if sfm_col >= 0 {
                        params[sfm_col as usize]
                    } else {
                        -1.0
                    };

                    // Extract generic signal filter trial values (PL_SIGNAL_P0..P9).
                    // `.round()` not `as i64` truncation, so a sampler drawing 2.9
                    // rounds to 3 (intuitive) rather than truncating to 2.
                    let mut trial_sig_filters: [i64; NUM_SIGNAL_PARAMS] = [-1; NUM_SIGNAL_PARAMS];
                    for f in 0..NUM_SIGNAL_PARAMS {
                        let col = param_layout_s[PL_SIGNAL_P0 + f];
                        if col >= 0 {
                            trial_sig_filters[f] = params[col as usize].round() as i64;
                        }
                    }

                    let mut trade_count = 0usize;
                    let mut total_sl_pips = 0.0_f64;

                    let (signal_start, signal_end) = if variant_slices_enabled && trial_variant >= 0
                    {
                        let vu = trial_variant as usize;
                        if vu < variant_start.len() {
                            (variant_start[vu], variant_end[vu])
                        } else {
                            (0, 0)
                        }
                    } else {
                        (0, n_signals)
                    };

                    'signal_loop: for si in signal_start..signal_end {
                        // Signal variant filter
                        if trial_variant >= 0 && sig_variant_s[si] >= 0 {
                            if sig_variant_s[si] != trial_variant {
                                continue;
                            }
                        }

                        // Strategy-specific value filter. Uses tolerance-compare
                        // to absorb f64 arithmetic drift, and honours signal-side
                        // -1 as an opt-out (matching the Pk family's bilateral
                        // sentinel semantics).
                        let direction = sig_direction_s[si];
                        if buy_filter_max >= 0.0
                            && direction == DIR_BUY
                            && sig_filter_value_s[si] >= 0.0
                        {
                            if (sig_filter_value_s[si] - buy_filter_max).abs() >= 1e-9 {
                                continue;
                            }
                        }
                        if sell_filter_min >= 0.0
                            && direction == DIR_SELL
                            && sig_filter_value_s[si] >= 0.0
                        {
                            if (sig_filter_value_s[si] - sell_filter_min).abs() >= 1e-9 {
                                continue;
                            }
                        }

                        // Generic signal param filters (PL_SIGNAL_P0..P9)
                        for f in 0..NUM_SIGNAL_PARAMS {
                            if trial_sig_filters[f] >= 0 {
                                let sig_val = sig_filters_s[f * n_filter_cols + si];
                                if sig_val >= 0 && sig_val != trial_sig_filters[f] {
                                    continue 'signal_loop;
                                }
                            }
                        }

                        // Time filter
                        if !signal_passes_time_filter(
                            sig_hour_s[si],
                            sig_day_s[si],
                            hours_start,
                            hours_end,
                            days_bitmask,
                        ) {
                            continue;
                        }

                        let bar_idx = sig_bar_index_s[si] as usize;
                        let entry_p = sig_entry_price_s[si];
                        let atr_p = sig_atr_pips_s[si];
                        let swing_sl = sig_swing_sl_s[si];

                        // Max spread filter
                        if max_spread_pips > 0.0 {
                            let spread_at_signal = spread_s[bar_idx] / pip_value;
                            if spread_at_signal.is_nan() || spread_at_signal > max_spread_pips {
                                continue;
                            }
                        }

                        let sl_tp = compute_sl_tp(
                            direction,
                            entry_p,
                            atr_p,
                            pip_value,
                            sl_mode,
                            sl_fixed_pips,
                            sl_atr_mult,
                            swing_sl,
                            tp_mode,
                            tp_rr_ratio,
                            tp_atr_mult,
                            tp_fixed_pips_val,
                        );

                        total_sl_pips += sl_tp.sl_pips;

                        // Simulate trade. Fire Forex uses one trade loop — the
                        // full-featured one. When all management knobs are off
                        // (trailing_mode=0, be_enabled=0, partial_en=0,
                        // stale_en=0, max_bars_val<=0) it degenerates to a pure
                        // SL/TP fill, matching the old EXEC_BASIC behaviour
                        // bit-for-bit (verified 2026-04-19 with baseline.py).
                        let result = simulate_trade_full(
                            direction,
                            bar_idx,
                            entry_p,
                            sl_tp.sl_price,
                            sl_tp.tp_price,
                            atr_p,
                            high_s,
                            low_s,
                            close_s,
                            spread_s,
                            pip_value,
                            slippage_pips,
                            n_bars,
                            trailing_mode,
                            trail_activate,
                            trail_distance,
                            trail_atr_m,
                            be_enabled,
                            be_trigger,
                            be_offset,
                            partial_en,
                            partial_pct,
                            partial_trig,
                            max_bars_val,
                            stale_en,
                            stale_bars_val,
                            stale_atr,
                            chandelier_en,
                            chandelier_activate,
                            chandelier_atr_m,
                            commission_pips,
                            sub_high_s,
                            sub_low_s,
                            sub_close_s,
                            sub_spread_s,
                            h1_to_sub_start_s,
                            h1_to_sub_end_s,
                        );

                        if trade_count < max_trades_usize {
                            pnl_buffer[trade_count] = result.pnl_pips;
                            let rec_off = trade_count * NUM_TRADE_FIELDS;
                            trade_record_buf[rec_off] = result.pnl_pips;
                            trade_record_buf[rec_off + 1] = result.exit_reason as f64;
                            trade_record_buf[rec_off + 2] = result.direction as f64;
                            trade_record_buf[rec_off + 3] = result.entry_bar_index as f64;
                            trade_record_buf[rec_off + 4] = result.entry_sub_bar_index as f64;
                            trade_record_buf[rec_off + 5] = result.entry_price;
                            trade_record_buf[rec_off + 6] = result.exit_bar_index as f64;
                            trade_record_buf[rec_off + 7] = result.exit_sub_bar_index as f64;
                            trade_record_buf[rec_off + 8] = result.exit_price;
                            trade_count += 1;
                        }
                    }

                    // Compute metrics
                    let avg_sl = if trade_count > 0 {
                        total_sl_pips / trade_count as f64
                    } else {
                        30.0
                    };
                    compute_metrics_inline(
                        pnl_buffer,
                        trade_count,
                        avg_sl,
                        n_bars,
                        bars_per_year,
                        metrics_row,
                    );
                })); // end catch_unwind

                if result.is_err() {
                    // Trial panicked — zero out metrics (optimizer treats as bad trial)
                    // SAFETY: metrics_ptr points to this trial's non-overlapping slice,
                    // allocated by Python and valid for the duration of this function.
                    unsafe {
                        std::ptr::write_bytes(metrics_ptr, 0, n_metrics);
                    }
                }
            });
    });

    Ok(())
}

/// Python module
#[pymodule]
fn ff_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(batch_evaluate, m)?)?;

    // Export constants for Python-side verification
    m.add("NUM_PL", NUM_PL)?;
    m.add("NUM_METRICS", NUM_METRICS)?;
    m.add("NUM_TRADE_FIELDS", NUM_TRADE_FIELDS)?;

    // Export PL_* constants
    m.add("PL_SL_MODE", PL_SL_MODE)?;
    m.add("PL_SL_FIXED_PIPS", PL_SL_FIXED_PIPS)?;
    m.add("PL_SL_ATR_MULT", PL_SL_ATR_MULT)?;
    m.add("PL_TP_MODE", PL_TP_MODE)?;
    m.add("PL_TP_RR_RATIO", PL_TP_RR_RATIO)?;
    m.add("PL_TP_ATR_MULT", PL_TP_ATR_MULT)?;
    m.add("PL_TP_FIXED_PIPS", PL_TP_FIXED_PIPS)?;
    m.add("PL_HOURS_START", PL_HOURS_START)?;
    m.add("PL_HOURS_END", PL_HOURS_END)?;
    m.add("PL_DAYS_BITMASK", PL_DAYS_BITMASK)?;
    m.add("PL_TRAILING_MODE", PL_TRAILING_MODE)?;
    m.add("PL_TRAIL_ACTIVATE", PL_TRAIL_ACTIVATE)?;
    m.add("PL_TRAIL_DISTANCE", PL_TRAIL_DISTANCE)?;
    m.add("PL_TRAIL_ATR_MULT", PL_TRAIL_ATR_MULT)?;
    m.add("PL_BREAKEVEN_ENABLED", PL_BREAKEVEN_ENABLED)?;
    m.add("PL_BREAKEVEN_TRIGGER", PL_BREAKEVEN_TRIGGER)?;
    m.add("PL_BREAKEVEN_OFFSET", PL_BREAKEVEN_OFFSET)?;
    m.add("PL_PARTIAL_ENABLED", PL_PARTIAL_ENABLED)?;
    m.add("PL_PARTIAL_PCT", PL_PARTIAL_PCT)?;
    m.add("PL_PARTIAL_TRIGGER", PL_PARTIAL_TRIGGER)?;
    m.add("PL_MAX_BARS", PL_MAX_BARS)?;
    m.add("PL_STALE_ENABLED", PL_STALE_ENABLED)?;
    m.add("PL_STALE_BARS", PL_STALE_BARS)?;
    m.add("PL_STALE_ATR_THRESH", PL_STALE_ATR_THRESH)?;
    m.add("PL_CHANDELIER_ENABLED", PL_CHANDELIER_ENABLED)?;
    m.add("PL_CHANDELIER_ACTIVATE", PL_CHANDELIER_ACTIVATE)?;
    m.add("PL_CHANDELIER_ATR_MULT", PL_CHANDELIER_ATR_MULT)?;
    m.add("PL_SIGNAL_VARIANT", PL_SIGNAL_VARIANT)?;
    m.add("PL_BUY_FILTER_MAX", PL_BUY_FILTER_MAX)?;
    m.add("PL_SELL_FILTER_MIN", PL_SELL_FILTER_MIN)?;

    // Generic signal param slots
    m.add("PL_SIGNAL_P0", PL_SIGNAL_P0)?;
    m.add("PL_SIGNAL_P1", PL_SIGNAL_P1)?;
    m.add("PL_SIGNAL_P2", PL_SIGNAL_P2)?;
    m.add("PL_SIGNAL_P3", PL_SIGNAL_P3)?;
    m.add("PL_SIGNAL_P4", PL_SIGNAL_P4)?;
    m.add("PL_SIGNAL_P5", PL_SIGNAL_P5)?;
    m.add("PL_SIGNAL_P6", PL_SIGNAL_P6)?;
    m.add("PL_SIGNAL_P7", PL_SIGNAL_P7)?;
    m.add("PL_SIGNAL_P8", PL_SIGNAL_P8)?;
    m.add("PL_SIGNAL_P9", PL_SIGNAL_P9)?;
    m.add("NUM_SIGNAL_PARAMS", NUM_SIGNAL_PARAMS)?;

    Ok(())
}
