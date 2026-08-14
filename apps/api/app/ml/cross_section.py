"""Cross-sectional harness (PLAN §12 lever #1) — rank a WIDE universe of single
names each day instead of predicting one index.

Why this attacks the real bottleneck: the per-asset SPY/QQQ problem has ~one
noisy independent observation per day (~200 after h-overlap). Ranking N names
per day turns each day into N observations of cross-sectional structure, so the
sample count scales with the universe width — out of the curse-of-dimensionality
zone that flattened the time-series models. This is where free price-derived
factors (momentum / reversal / vol / trend) carry a real, if modest, edge.

Leakage discipline is identical to the time-series path:
  * The feature matrix is the SAME leakage-safe ``features.build_model_matrix``
    (PIT macro joins + the leak gate still apply), just over the wide universe.
  * Walk-forward is **by date** with purge + embargo on the date axis — all
    names of a date move together (they share the calendar and the h-day label
    window), so no test day's label can leak into training.
  * Cross-sectional IC = Spearman *within each test day* across names, averaged
    over days with a t-stat on the daily-IC series (N = #test days) — the
    standard quant "information coefficient", robust to the market-level move.

Targets:
  * relative return — per-day cross-sectionally demeaned vol-scaled forward
    return (removes the common market factor → learn who *out*-performs).
  * forward vol — ``labels.forward_realized_vol`` (lever #2), ranked across names.

Reads prices from a SEPARATE universe DB (data/ml/universe.duckdb) and macro from
the live market DB READ-ONLY, via a small routing shim — so the big single-name
panel never touches the single-writer terminal DB.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import numpy as np
import pandas as pd

from app.ml.cv import spearman_ic


class RoutingDuck:
    """fetchall shim: ts_price → universe DB, everything else → main DB (RO).

    Lets ``dataset.build_feature_matrix`` run unchanged over the wide universe
    while macro / vintages / COT still come from the live terminal DB.
    """

    def __init__(self, main_con, universe_con):
        self._main = main_con
        self._uni = universe_con

    def fetchall(self, sql, params=None):
        if "ts_price" in sql:
            # Per-asset closes for the 147-name universe live in the universe
            # DB, but the cross-asset block (TLT/GLD/copper/DXY/SMH/XLY/XLP)
            # is also a ts_price query and those symbols are NOT in the
            # universe DB (equity-only) — they live in the main terminal DB.
            # Try the universe DB first (the common case); an empty result
            # falls back to main so the cross-asset block still resolves.
            rows = self._uni.execute(sql, params or []).fetchall()
            if rows:
                return rows
            return self._main.execute(sql, params or []).fetchall()
        return self._main.execute(sql, params or []).fetchall()


# --- per-day cross-sectional helpers ---------------------------------------


def demean_by_day(values: pd.Series, dates: np.ndarray) -> pd.Series:
    """Subtract each day's cross-sectional mean → relative (market-neutral)
    target. Rank within a day is unchanged, but training on the residual forces
    the model to learn who out-performs rather than the (hard) market direction.
    """
    df = pd.DataFrame({"v": values.to_numpy(), "d": dates})
    return pd.Series(
        (df["v"] - df.groupby("d")["v"].transform("mean")).to_numpy(),
        index=values.index,
    )


def cross_sectional_ic(
    dates: np.ndarray, pred: np.ndarray, true: np.ndarray, *, min_names: int = 5
) -> dict:
    """Mean daily cross-sectional rank-IC + t-stat over the daily-IC series.

    For each day with ≥ ``min_names`` finite pairs, Spearman(pred, true) across
    names; the headline is the mean of those daily ICs with a t-stat
    ``mean/se`` (se from the daily-IC std). Also returns the daily-IC array so a
    per-fold distribution can feed PBO/DSR.
    """
    df = pd.DataFrame({"d": dates, "p": pred, "t": true}).dropna()
    daily = []
    for _, g in df.groupby("d"):
        if len(g) >= min_names:
            ic = spearman_ic(g["p"].to_numpy(), g["t"].to_numpy())
            if np.isfinite(ic):
                daily.append(ic)
    daily = np.array(daily, dtype=float)
    if len(daily) < 3:
        return {"ic": float("nan"), "ic_t": float("nan"), "n_days": len(daily),
                "hit": float("nan"), "daily": daily}
    mean = float(daily.mean())
    se = float(daily.std(ddof=1) / np.sqrt(len(daily)))
    return {
        "ic": mean,
        "ic_t": mean / se if se > 0 else float("nan"),
        "n_days": len(daily),
        "hit": float((daily > 0).mean()),  # fraction of days the sign is right
        "daily": daily,
    }


# --- date-based purged walk-forward ----------------------------------------


@dataclass
class DateWalkForward:
    """Expanding-window walk-forward over UNIQUE DATES (not rows).

    All names of a date are kept together. Train = every name on dates strictly
    before the test block, minus a purge of the last ``h`` train days whose label
    window [d, d+h] overlaps the first test day, minus an ``embargo`` of days
    after the test block. Yields boolean row-masks into the pooled frame.
    """

    n_splits: int = 6
    min_train_days: int = 252
    embargo_days: int = 5
    horizon: int = 5

    def split(self, dates: np.ndarray) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        uniq = np.array(sorted(pd.unique(dates)))
        n = len(uniq)
        if self.min_train_days >= n:
            raise ValueError("min_train_days exceeds available days")
        starts = np.linspace(self.min_train_days, n, self.n_splits + 1).astype(int)
        for i in range(self.n_splits):
            lo, hi = starts[i], starts[i + 1]
            if hi <= lo:
                continue
            test_days = uniq[lo:hi]
            test0 = test_days[0]
            # Purge: drop train days within h of the first test day (their label
            # window leaks into the test block — train label-end (d+h) must be
            # < test0). Embargo: drop days just after.
            train_days = uniq[:lo]
            train_days = train_days[
                _shift_days(train_days, self.horizon, uniq) < test0
            ]
            test_mask = np.isin(dates, test_days)
            train_mask = np.isin(dates, train_days)
            yield train_mask, test_mask


def _shift_days(days: np.ndarray, h: int, uniq: np.ndarray) -> np.ndarray:
    """Map each day to the calendar date h trading days later (its label-end),
    clamped to the last available day. Used to purge overlapping labels."""
    pos = np.searchsorted(uniq, days)
    end_pos = np.minimum(pos + h, len(uniq) - 1)
    return uniq[end_pos]
