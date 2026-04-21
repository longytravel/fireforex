"""Numeric exit-reason codes emitted by the Rust engine → human names.

Mirrors `core/src/constants.rs` EXIT_* constants. Used by the trade log
widener and the live reconciler for string-equality comparison between
backtest exits and MT5 deal reasons.
"""
from __future__ import annotations

EXIT_REASON_NAMES: dict[int, str] = {
    0: "NONE",
    1: "SL",
    2: "TP",
    3: "TRAILING",
    4: "BREAKEVEN",
    5: "MAX_BARS",
    6: "STALE",
    7: "CHANDELIER",
}


def exit_reason_name(code: int | float) -> str:
    """Map a numeric exit code (int or float64 from the engine) to its name.

    Unknown codes fall back to ``UNKNOWN`` — defensive so a new Rust enum
    value doesn't crash the trade-log writer.
    """
    return EXIT_REASON_NAMES.get(int(code), "UNKNOWN")
