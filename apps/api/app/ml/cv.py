"""Leakage-safe cross-validation (PLAN §12.3) — the part that actually matters.

Daily financial labels are *overlapping*: a 5-day label at ``t`` shares four
days with the label at ``t+1``. Plain K-fold then leaks — a test sample's
outcome is partly determined by days that also sit in adjacent training
samples. The fixes (López de Prado, *Advances in Financial ML*, ch.7):

  * **Purge** — drop any training sample whose label window
    ``[t_decision, t_end]`` overlaps the test fold's span.
  * **Embargo** — additionally drop training samples in a small window *after*
    the test fold, where serial correlation still bleeds.
  * **Walk-forward** — expanding-window OOS, the honest headline evaluation.
  * **CPCV** — Combinatorial Purged CV: many train/test group recombinations to
    get a *distribution* of OOS scores, not one lucky split (§12.5 needs the
    distribution for PBO / Deflated-Sharpe).

Every splitter consumes ``label_times`` (decision date + label-end date) so the
purge is correct regardless of horizon. Pure numpy/pandas; no sklearn.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterator

import numpy as np
import pandas as pd


# --- core purge primitive ---------------------------------------------------


def _purge_train(
    train_pos: np.ndarray,
    test_pos: np.ndarray,
    t_decision: np.ndarray,
    t_end: np.ndarray,
    embargo: int,
) -> np.ndarray:
    """Remove from ``train_pos`` every sample whose label window overlaps the
    test fold's [min decision, max end] span, plus an ``embargo`` of samples
    immediately after the test block. Positions index ``t_decision``/``t_end``.
    """
    if len(test_pos) == 0:
        return train_pos
    test_start = t_decision[test_pos].min()
    test_end = t_end[test_pos].max()

    keep = []
    # Embargo extends the forbidden zone past the last test label-end by
    # `embargo` *samples* (converted to a time bound via the sorted decisions).
    order = np.argsort(t_decision)
    sorted_dec = t_decision[order]
    last_test_dec = t_decision[test_pos].max()
    emb_idx = np.searchsorted(sorted_dec, last_test_dec, side="right") - 1
    emb_idx = min(emb_idx + embargo, len(sorted_dec) - 1)
    embargo_until = sorted_dec[emb_idx]

    for p in train_pos:
        d, e = t_decision[p], t_end[p]
        # Overlap test span?  (train label window intersects test window)
        overlaps = (e >= test_start) and (d <= test_end)
        # Inside embargo tail just after the test block?
        in_embargo = (d > test_end) and (d <= embargo_until)
        if not overlaps and not in_embargo:
            keep.append(p)
    return np.array(keep, dtype=int)


def _as_int64(idx) -> np.ndarray:
    """Datetime-or-numeric index → int64 ns (comparable, sortable)."""
    arr = pd.DatetimeIndex(idx).asi8 if _is_datetime(idx) else np.asarray(idx)
    return arr.astype("int64")


def _is_datetime(idx) -> bool:
    try:
        pd.DatetimeIndex(idx)
        return True
    except (TypeError, ValueError):
        return False


# --- splitters --------------------------------------------------------------


@dataclass
class PurgedKFold:
    """K contiguous test folds (time-ordered, not shuffled) with purge+embargo.

    ``embargo_pct`` is a fraction of the sample count converted to a sample
    count. Yields ``(train_pos, test_pos)`` integer-position arrays into the
    ``label_times`` frame.
    """

    n_splits: int = 5
    embargo_pct: float = 0.01

    def split(self, label_times: pd.DataFrame) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        n = len(label_times)
        if self.n_splits < 2 or self.n_splits > n:
            raise ValueError("n_splits must be in [2, n_samples]")
        t_dec = _as_int64(label_times["t_decision"].to_numpy())
        t_end = _as_int64(label_times["t_end"].to_numpy())
        embargo = int(n * self.embargo_pct)
        all_pos = np.arange(n)
        # Contiguous, equal-ish folds in chronological order.
        bounds = np.linspace(0, n, self.n_splits + 1).astype(int)
        for i in range(self.n_splits):
            test_pos = all_pos[bounds[i] : bounds[i + 1]]
            if len(test_pos) == 0:
                continue
            train_pos = np.setdiff1d(all_pos, test_pos, assume_unique=True)
            train_pos = _purge_train(train_pos, test_pos, t_dec, t_end, embargo)
            yield train_pos, test_pos


@dataclass
class WalkForward:
    """Expanding-window walk-forward — the headline OOS evaluation (§12.3).

    Train on everything up to a fold, test on the next contiguous block, slide
    forward. ``min_train`` is the warm-up before the first test. Purge+embargo
    still applied at the train/test seam (overlapping labels leak even here).
    """

    n_splits: int = 5
    min_train: int = 252
    embargo_pct: float = 0.01

    def split(self, label_times: pd.DataFrame) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        n = len(label_times)
        t_dec = _as_int64(label_times["t_decision"].to_numpy())
        t_end = _as_int64(label_times["t_end"].to_numpy())
        embargo = int(n * self.embargo_pct)
        if self.min_train >= n:
            raise ValueError("min_train exceeds sample count")
        test_starts = np.linspace(self.min_train, n, self.n_splits + 1).astype(int)
        for i in range(self.n_splits):
            lo, hi = test_starts[i], test_starts[i + 1]
            test_pos = np.arange(lo, hi)
            if len(test_pos) == 0:
                continue
            train_pos = np.arange(0, lo)  # expanding past only
            train_pos = _purge_train(train_pos, test_pos, t_dec, t_end, embargo)
            yield train_pos, test_pos


@dataclass
class CombinatorialPurgedCV:
    """CPCV (§12.3) — split into ``n_groups`` contiguous groups, test on every
    combination of ``k_test`` groups, train on the rest (purged). Produces
    C(n_groups, k_test) OOS paths → a *distribution* of performance for PBO /
    Deflated Sharpe (§12.5), instead of a single split's luck.
    """

    n_groups: int = 6
    k_test: int = 2
    embargo_pct: float = 0.01

    def split(self, label_times: pd.DataFrame) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        n = len(label_times)
        if not 1 <= self.k_test < self.n_groups:
            raise ValueError("require 1 <= k_test < n_groups")
        t_dec = _as_int64(label_times["t_decision"].to_numpy())
        t_end = _as_int64(label_times["t_end"].to_numpy())
        embargo = int(n * self.embargo_pct)
        bounds = np.linspace(0, n, self.n_groups + 1).astype(int)
        groups = [np.arange(bounds[g], bounds[g + 1]) for g in range(self.n_groups)]
        for combo in combinations(range(self.n_groups), self.k_test):
            test_pos = np.concatenate([groups[g] for g in combo])
            test_pos.sort()
            if len(test_pos) == 0:
                continue
            train_pos = np.concatenate(
                [groups[g] for g in range(self.n_groups) if g not in combo]
            )
            train_pos.sort()
            train_pos = _purge_train(train_pos, test_pos, t_dec, t_end, embargo)
            yield train_pos, test_pos


# --- evaluation: rank-IC over concatenated OOS folds ------------------------


def spearman_ic(pred: np.ndarray, y: np.ndarray) -> float:
    """Spearman rank correlation (§12.5 rank-IC) without scipy."""
    pred = np.asarray(pred, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(pred) & np.isfinite(y)
    if mask.sum() < 3:
        return float("nan")
    rp = pd.Series(pred[mask]).rank().to_numpy()
    ry = pd.Series(y[mask]).rank().to_numpy()
    if rp.std() == 0 or ry.std() == 0:
        return float("nan")
    return float(np.corrcoef(rp, ry)[0, 1])


def oos_rank_ic(
    X: pd.Series | np.ndarray,
    y: pd.Series | np.ndarray,
    label_times: pd.DataFrame,
    splitter,
    *,
    predict=None,
) -> dict:
    """Out-of-sample rank-IC of a signal under a purged splitter.

    Phase-1 harness has no models, so by default the *feature itself* is the
    prediction (univariate IC) — enough to (a) prove the metric detects a real
    signal and (b) prove the purge/PIT join kills a leaked one. ``predict`` may
    be supplied later as ``f(X_train, y_train, X_test) -> preds`` to score an
    actual model. Returns the pooled OOS IC plus the per-fold distribution
    (the per-fold list is what PBO/DSR consume in phase 2).
    """
    X = np.asarray(X if not isinstance(X, pd.Series) else X.to_numpy(), dtype=float)
    y = np.asarray(y if not isinstance(y, pd.Series) else y.to_numpy(), dtype=float)
    pooled_pred: list[float] = []
    pooled_y: list[float] = []
    fold_ics: list[float] = []
    n_train: list[int] = []
    for train_pos, test_pos in splitter.split(label_times):
        if predict is None:
            preds = X[test_pos]
        else:
            preds = predict(X[train_pos], y[train_pos], X[test_pos])
        fold_ics.append(spearman_ic(preds, y[test_pos]))
        pooled_pred.extend(np.atleast_1d(preds).tolist())
        pooled_y.extend(y[test_pos].tolist())
        n_train.append(len(train_pos))
    return {
        "oos_ic": spearman_ic(np.array(pooled_pred), np.array(pooled_y)),
        "fold_ics": fold_ics,
        "mean_fold_ic": float(np.nanmean(fold_ics)) if fold_ics else float("nan"),
        "n_folds": len(fold_ics),
        "min_train_size": int(min(n_train)) if n_train else 0,
    }
