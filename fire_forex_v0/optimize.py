from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import optuna
import pandas as pd
from optuna.samplers import TPESampler

from .backtest import run_backtest
from .params import suggest_params


@dataclass
class OptimizeResult:
    best_params: dict
    best_score: float
    study: optuna.Study


def optimize(
    df: pd.DataFrame,
    n_trials: int = 500,
    n_jobs: int = 1,
    seed: int = 42,
    storage: str | None = None,
    study_name: str = "fire_forex_v0",
    show_progress: bool = True,
) -> OptimizeResult:
    """Run an Optuna TPE search over the 50-parameter space."""
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    sampler = TPESampler(seed=seed, multivariate=True, group=True)
    study = optuna.create_study(
        direction="maximize",
        sampler=sampler,
        study_name=study_name,
        storage=storage,
        load_if_exists=bool(storage),
    )

    def objective(trial: optuna.Trial) -> float:
        p = suggest_params(trial)
        try:
            res = run_backtest(df, p)
        except Exception as e:
            trial.set_user_attr("error", str(e))
            return -1e9
        trial.set_user_attr("trade_count", res.trade_count)
        trial.set_user_attr("sharpe", res.sharpe)
        trial.set_user_attr("total_return", res.total_return)
        trial.set_user_attr("max_drawdown", res.max_drawdown)
        return res.score

    study.optimize(
        objective,
        n_trials=n_trials,
        n_jobs=n_jobs,
        show_progress_bar=show_progress,
        gc_after_trial=True,
    )

    return OptimizeResult(
        best_params=study.best_params,
        best_score=study.best_value,
        study=study,
    )


def save_study_summary(result: OptimizeResult, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    trials = result.study.trials_dataframe(attrs=("number", "value", "params", "user_attrs", "state"))
    trials.to_parquet(path.with_suffix(".parquet"), index=False)
    top = trials.sort_values("value", ascending=False).head(20)
    top.to_csv(path.with_suffix(".top20.csv"), index=False)
