/// Signal filtering — mirrors _filter_signals_for_trial() from jit_loop.py.

/// Apply time filters to a single signal. Returns true if signal passes.
#[inline(always)]
pub fn signal_passes_time_filter(
    sig_hour: i64,
    sig_day: i64,
    hours_start: i64,
    hours_end: i64,
    days_bitmask: i64,
) -> bool {
    // Day filter: check if day's bit is set in bitmask
    let day_bit = 1i64 << sig_day;
    if (days_bitmask & day_bit) == 0 {
        return false;
    }

    // Hour filter: handle wrap-around (e.g., 22-06)
    if hours_start <= hours_end {
        sig_hour >= hours_start && sig_hour <= hours_end
    } else {
        sig_hour >= hours_start || sig_hour <= hours_end
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_day_filter() {
        // Monday (bit 0) only
        assert!(signal_passes_time_filter(12, 0, 0, 23, 1));
        assert!(!signal_passes_time_filter(12, 1, 0, 23, 1));
    }

    #[test]
    fn test_hour_filter_normal() {
        // Hours 8-16, all days
        assert!(signal_passes_time_filter(8, 0, 8, 16, 127));
        assert!(signal_passes_time_filter(16, 0, 8, 16, 127));
        assert!(!signal_passes_time_filter(7, 0, 8, 16, 127));
        assert!(!signal_passes_time_filter(17, 0, 8, 16, 127));
    }

    #[test]
    fn test_hour_filter_wraparound() {
        // Hours 22-06 (overnight), all days
        assert!(signal_passes_time_filter(22, 0, 22, 6, 127));
        assert!(signal_passes_time_filter(23, 0, 22, 6, 127));
        assert!(signal_passes_time_filter(0, 0, 22, 6, 127));
        assert!(signal_passes_time_filter(6, 0, 22, 6, 127));
        assert!(!signal_passes_time_filter(7, 0, 22, 6, 127));
        assert!(!signal_passes_time_filter(21, 0, 22, 6, 127));
    }
}
