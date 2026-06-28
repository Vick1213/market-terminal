"""In-fold transform pipeline + OOS collection (PLAN §12.2 / §12.4).

Two jobs:

1. ``build_pipeline`` wraps every estimator in median-impute → standardize →
   model. Per §12.2 these transforms are **fit inside each CV fold** (only on
   that fold's training rows) — fitting the scaler/imputer on the whole panel
   would leak test-fold statistics into training. sklearn ``Pipeline`` gives us
   that for free: ``.fit(train)`` fits every step on train only.

2. ``collect_oos`` runs a fresh pipeline per purged fold and gathers the
   out-of-sample predictions + the per-fold score distribution that §12.5's
   PBO / Deflated-Sharpe gates consume. Refitting per fold (no state carried
   across folds) is what makes walk-forward honest.

Trees ignore scaling and handle NaN natively, but the uniform impute+scale
keeps ElasticNet / RandomForest (which can't take NaN) working on the OAS/COT
columns that are NaN before 2023. $0, CPU/MPS-local.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

EstimatorFactory = Callable[[], object]


def build_pipeline(estimator) -> Pipeline:
    """median-impute → standardize → estimator. Every step fits on train only."""
    return Pipeline(
        [
            # keep_empty_features: a fold whose training window predates a
            # feature's history (OAS/COT are NaN before 2023) keeps the column
            # as a constant fill instead of silently dropping it, so the feature
            # width is stable across folds.
            ("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
            ("scale", StandardScaler()),
            ("model", estimator),
        ]
    )


@dataclass
class OOSResult:
    """Pooled out-of-sample predictions + the per-fold score list.

    pred/true/fold are aligned 1:1 (pooled across all test folds). ``fold_ids``
    labels which fold each pooled row came from; ``n_folds`` and the per-fold
    arrays let metrics compute a *distribution* (not a single split).
    """

    pred: np.ndarray
    true: np.ndarray
    fold_ids: np.ndarray
    proba: np.ndarray | None = None  # P(up) for classification tasks
    n_folds: int = 0
    min_train: int = 0
    fold_index: list[np.ndarray] = field(default_factory=list)  # test row positions/fold


def collect_oos(
    make_estimator: EstimatorFactory,
    X: np.ndarray,
    y: np.ndarray,
    label_times: pd.DataFrame,
    splitter,
    *,
    task: str = "regression",
    y_class: np.ndarray | None = None,
) -> OOSResult:
    """Refit ``make_estimator()`` on every purged training fold, predict OOS.

    For ``task='classification'`` the estimator is fit on ``y_class`` (binary
    up/down) and we keep ``P(up)``; ``y`` (the continuous vol-scaled return) is
    still carried as ``true`` so rank-IC / Sharpe can be computed off the same
    predictions. Folds with degenerate training labels are skipped.
    """
    preds: list[float] = []
    trues: list[float] = []
    probas: list[float] = []
    fold_ids: list[int] = []
    fold_index: list[np.ndarray] = []
    n_train: list[int] = []

    for k, (tr, te) in enumerate(splitter.split(label_times)):
        if len(tr) < 30 or len(te) == 0:
            continue
        pipe = build_pipeline(make_estimator())
        if task == "classification":
            ytr = y_class[tr]
            if len(np.unique(ytr[np.isfinite(ytr)])) < 2:
                continue
            mask = np.isfinite(ytr)
            pipe.fit(X[tr][mask], ytr[mask].astype(int))
            p_up = pipe.predict_proba(X[te])[:, 1]
            probas.extend(p_up.tolist())
            preds.extend((p_up - 0.5).tolist())  # centred score for rank-IC/Sharpe
        else:
            ytr = y[tr]
            mask = np.isfinite(ytr)
            if mask.sum() < 30:
                continue
            pipe.fit(X[tr][mask], ytr[mask])
            yhat = pipe.predict(X[te])
            preds.extend(np.asarray(yhat).tolist())
            probas.extend([np.nan] * len(te))
        trues.extend(y[te].tolist())
        fold_ids.extend([k] * len(te))
        fold_index.append(te)
        n_train.append(int(mask.sum()))

    return OOSResult(
        pred=np.asarray(preds, dtype=float),
        true=np.asarray(trues, dtype=float),
        fold_ids=np.asarray(fold_ids, dtype=int),
        proba=np.asarray(probas, dtype=float) if probas else None,
        n_folds=len(set(fold_ids)),
        min_train=int(min(n_train)) if n_train else 0,
        fold_index=fold_index,
    )
