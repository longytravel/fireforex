from __future__ import annotations

from ff.live.frozen_signal import pin_frozen_signal
from ff.schema import Choice, IntRange, expand


def test_pin_frozen_signal_injects_combo_outside_pair_grid():
    ea = {
        "signals": {
            "ema_cross": {
                "fast": IntRange(10, 20, step=5),
                "slow": IntRange(30, 50, step=10),
            }
        }
    }
    best_trial = {
        "signal_family": "ema_cross",
        "signal_params": {"fast": 3, "slow": 164},
    }

    out = pin_frozen_signal(ea, best_trial)

    assert isinstance(out["signals"]["ema_cross"]["fast"], Choice)
    assert expand(out["signals"]["ema_cross"]["fast"]) == [3]
    assert expand(out["signals"]["ema_cross"]["slow"]) == [164]


def test_pin_frozen_signal_creates_missing_family():
    ea = {"signals": {}}
    best_trial = {
        "signal_family": "donchian",
        "signal_params": {"lookback": 83},
    }

    out = pin_frozen_signal(ea, best_trial)

    assert expand(out["signals"]["donchian"]["lookback"]) == [83]
