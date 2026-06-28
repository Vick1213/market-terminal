"""Model zoo (PLAN §12.4) — phase-2 baselines behind a uniform factory.

Phase 2 ships only the tier the plan calls "the bar every fancy model must
clear": linear baselines, the gradient-boosted-tree workhorse (expected
champion on daily tabular data), a bagged tree, and naive nulls that define
zero-edge. Sequence nets (TCN/LSTM/CNN-LSTM) are phase 3 and are deliberately
NOT here — you don't build them until these baselines show OOS edge.

Each entry is a **factory** (zero-arg callable returning a *fresh* estimator) so
``pipeline.collect_oos`` can refit a clean model per walk-forward fold. All
hyper-params are biased toward heavy regularisation: ~200 effective independent
samples (§12.9 curse-of-dimensionality) punish anything flexible.
"""

from __future__ import annotations

from typing import Callable

from lightgbm import LGBMClassifier, LGBMRegressor
from sklearn.dummy import DummyClassifier, DummyRegressor
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.linear_model import ElasticNetCV, LogisticRegressionCV

EstimatorFactory = Callable[[], object]

# --- regression target: vol-scaled forward return ---------------------------

REGRESSORS: dict[str, EstimatorFactory] = {
    # The zero-edge bar: predict the unconditional mean (~0 for vol-scaled).
    "naive_mean": lambda: DummyRegressor(strategy="mean"),
    # Linear baseline every fancy model must beat (§12.4).
    "elasticnet": lambda: ElasticNetCV(
        l1_ratio=[0.1, 0.5, 0.9],
        alphas=24,  # sklearn>=1.9: int = number of alphas along the path
        cv=3,
        max_iter=8000,
        random_state=0,
    ),
    # Expected champion on daily tabular data — small, regularised.
    "lightgbm": lambda: LGBMRegressor(
        n_estimators=300,
        learning_rate=0.03,
        num_leaves=15,
        max_depth=4,
        min_child_samples=30,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.7,
        reg_lambda=1.0,
        random_state=0,
        verbose=-1,
        n_jobs=1,
    ),
    # Bagged baseline + honest feature importance.
    "random_forest": lambda: RandomForestRegressor(
        n_estimators=300,
        max_depth=4,
        min_samples_leaf=20,
        max_features=0.5,
        random_state=0,
        n_jobs=1,
    ),
}

# --- classification target: triple-barrier up/down --------------------------

CLASSIFIERS: dict[str, EstimatorFactory] = {
    "naive_prior": lambda: DummyClassifier(strategy="prior"),
    "logistic": lambda: LogisticRegressionCV(
        Cs=10,
        cv=3,
        scoring="neg_log_loss",
        max_iter=4000,
        random_state=0,
        n_jobs=1,
    ),
    "lightgbm_clf": lambda: LGBMClassifier(
        n_estimators=300,
        learning_rate=0.03,
        num_leaves=15,
        max_depth=4,
        min_child_samples=30,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.7,
        reg_lambda=1.0,
        random_state=0,
        verbose=-1,
        n_jobs=1,
    ),
}
