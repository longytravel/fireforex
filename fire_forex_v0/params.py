from dataclasses import dataclass, asdict, fields
from typing import Any


@dataclass
class Params:
    # ── Entry signals (15) ───────────────────────────────────────────
    ema_fast: int = 12
    ema_slow: int = 48
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    atr_period: int = 14
    bb_period: int = 20
    bb_std: float = 2.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    donchian_period: int = 55
    keltner_mult: float = 1.5
    momentum_period: int = 10
    momentum_threshold: float = 0.0

    # ── Filters (12) ─────────────────────────────────────────────────
    session_london: bool = True
    session_ny: bool = True
    session_asian: bool = False
    min_atr_pct: float = 0.00005
    max_atr_pct: float = 0.01
    trend_ema: int = 200
    require_trend: bool = True
    day_mon: bool = True
    day_tue: bool = True
    day_wed: bool = True
    day_thu: bool = True
    day_fri: bool = True

    # ── Risk & sizing (10) ───────────────────────────────────────────
    sl_atr_mult: float = 2.0
    tp_atr_mult: float = 3.0
    trail_stop_mult: float = 1.5
    breakeven_atr: float = 1.0
    max_spread_pips: float = 2.0
    commission_pips: float = 0.3
    initial_cash: float = 10_000.0
    fee_pct: float = 0.00005
    slippage_pct: float = 0.00005
    risk_pct: float = 0.01

    # ── Exit rules (8) ───────────────────────────────────────────────
    time_exit_bars: int = 240
    reverse_exit: bool = True
    exit_on_rsi_extreme: bool = True
    rsi_exit_long: float = 80.0
    rsi_exit_short: float = 20.0
    use_trailing: bool = True
    use_breakeven: bool = True
    exit_end_of_session: bool = False

    # ── Meta (5) ─────────────────────────────────────────────────────
    confirm_bars: int = 1
    min_bars_between: int = 5
    use_htf_filter: bool = True
    htf_ema: int = 50
    htf_bars_back: int = 60

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Params":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


DEFAULT_PARAMS = Params()


# Optuna search space — order mirrors the dataclass.
# Ranges picked to be wide enough to discover something, narrow enough to converge.
def suggest_params(trial) -> Params:
    p: dict[str, Any] = {}

    # Entry (15)
    p["ema_fast"] = trial.suggest_int("ema_fast", 5, 40)
    p["ema_slow"] = trial.suggest_int("ema_slow", 20, 240)
    p["rsi_period"] = trial.suggest_int("rsi_period", 5, 30)
    p["rsi_oversold"] = trial.suggest_float("rsi_oversold", 15.0, 40.0)
    p["rsi_overbought"] = trial.suggest_float("rsi_overbought", 60.0, 85.0)
    p["atr_period"] = trial.suggest_int("atr_period", 5, 30)
    p["bb_period"] = trial.suggest_int("bb_period", 10, 60)
    p["bb_std"] = trial.suggest_float("bb_std", 1.0, 3.5)
    p["macd_fast"] = trial.suggest_int("macd_fast", 5, 20)
    p["macd_slow"] = trial.suggest_int("macd_slow", 15, 60)
    p["macd_signal"] = trial.suggest_int("macd_signal", 3, 20)
    p["donchian_period"] = trial.suggest_int("donchian_period", 10, 120)
    p["keltner_mult"] = trial.suggest_float("keltner_mult", 0.8, 3.0)
    p["momentum_period"] = trial.suggest_int("momentum_period", 3, 30)
    p["momentum_threshold"] = trial.suggest_float("momentum_threshold", -0.001, 0.001)

    # Filters (12)
    p["session_london"] = trial.suggest_categorical("session_london", [True, False])
    p["session_ny"] = trial.suggest_categorical("session_ny", [True, False])
    p["session_asian"] = trial.suggest_categorical("session_asian", [True, False])
    p["min_atr_pct"] = trial.suggest_float("min_atr_pct", 0.0, 0.0005)
    p["max_atr_pct"] = trial.suggest_float("max_atr_pct", 0.001, 0.05)
    p["trend_ema"] = trial.suggest_int("trend_ema", 50, 400)
    p["require_trend"] = trial.suggest_categorical("require_trend", [True, False])
    p["day_mon"] = trial.suggest_categorical("day_mon", [True, False])
    p["day_tue"] = trial.suggest_categorical("day_tue", [True, False])
    p["day_wed"] = trial.suggest_categorical("day_wed", [True, False])
    p["day_thu"] = trial.suggest_categorical("day_thu", [True, False])
    p["day_fri"] = trial.suggest_categorical("day_fri", [True, False])

    # Risk & sizing (10) — initial_cash fixed; others tunable
    p["sl_atr_mult"] = trial.suggest_float("sl_atr_mult", 0.5, 5.0)
    p["tp_atr_mult"] = trial.suggest_float("tp_atr_mult", 0.5, 8.0)
    p["trail_stop_mult"] = trial.suggest_float("trail_stop_mult", 0.5, 4.0)
    p["breakeven_atr"] = trial.suggest_float("breakeven_atr", 0.3, 3.0)
    p["max_spread_pips"] = trial.suggest_float("max_spread_pips", 0.5, 5.0)
    p["commission_pips"] = trial.suggest_float("commission_pips", 0.0, 1.5)
    p["initial_cash"] = 10_000.0
    p["fee_pct"] = trial.suggest_float("fee_pct", 0.0, 0.0002)
    p["slippage_pct"] = trial.suggest_float("slippage_pct", 0.0, 0.0002)
    p["risk_pct"] = trial.suggest_float("risk_pct", 0.002, 0.02)

    # Exits (8)
    p["time_exit_bars"] = trial.suggest_int("time_exit_bars", 20, 1440)
    p["reverse_exit"] = trial.suggest_categorical("reverse_exit", [True, False])
    p["exit_on_rsi_extreme"] = trial.suggest_categorical("exit_on_rsi_extreme", [True, False])
    p["rsi_exit_long"] = trial.suggest_float("rsi_exit_long", 65.0, 90.0)
    p["rsi_exit_short"] = trial.suggest_float("rsi_exit_short", 10.0, 35.0)
    p["use_trailing"] = trial.suggest_categorical("use_trailing", [True, False])
    p["use_breakeven"] = trial.suggest_categorical("use_breakeven", [True, False])
    p["exit_end_of_session"] = trial.suggest_categorical("exit_end_of_session", [True, False])

    # Meta (5)
    p["confirm_bars"] = trial.suggest_int("confirm_bars", 1, 5)
    p["min_bars_between"] = trial.suggest_int("min_bars_between", 1, 60)
    p["use_htf_filter"] = trial.suggest_categorical("use_htf_filter", [True, False])
    p["htf_ema"] = trial.suggest_int("htf_ema", 10, 200)
    p["htf_bars_back"] = trial.suggest_int("htf_bars_back", 5, 300)

    return Params(**p)


assert len(fields(Params)) == 50, f"expected 50 params, got {len(fields(Params))}"
