"""Selection metrics (PLAN §12.5) — how you avoid crowning noise.

Running ~4 model types × 3 horizons × 2 assets × 2 targets is a
multiple-comparisons minefield; a flattering in-sample fold is the default
outcome, not the exception. These are the discounts:

  * **rank-IC + t-stat** — Spearman(pred, realised vol-scaled return) pooled
    OOS, with a t-stat from the per-fold IC distribution (not one split).
  * **OOS long/short Sharpe after costs** — sign(signal)·raw forward return,
    minus turnover · cost; the only number that survives contact with a broker.
  * **AUC + Brier** — for the classification target (calibration curve is wired
    in phase 4; here we report discrimination + raw Brier).
  * **PBO (CSCV)** — Probability of Backtest Overfitting: P(the model that looks
    best in-sample lands below median out-of-sample). >0.5 ⇒ the selection is
    noise. (Bailey & López de Prado, 2014.)
  * **Deflated Sharpe Ratio** — shrinks the best Sharpe for the number of trials
    and the non-normality of returns; DSR is the prob the true SR > 0.

scipy + numpy. No look-ahead — every input is already OOS.
"""

from __future__ import annotations

from itertools import combinations

import numpy as np
from scipy import stats

from app.ml.cv import spearman_ic

_NORM = stats.norm


# --- regression-side -------------------------------------------------------


def ic_tstat(per_fold_ics: list[float]) -> tuple[float, float]:
    """(mean IC, t-stat) across folds. t = mean / (std/√k). A single positive
    pooled IC means little; a t-stat ≳ 2 across folds is the real bar.
    """
    arr = np.array([x for x in per_fold_ics if np.isfinite(x)], dtype=float)
    if len(arr) < 2:
        return (float(arr.mean()) if len(arr) else float("nan"), float("nan"))
    mean = float(arr.mean())
    se = float(arr.std(ddof=1) / np.sqrt(len(arr)))
    return mean, (mean / se if se > 0 else float("nan"))


def strategy_sharpe(
    signal: np.ndarray,
    raw_fwd_ret: np.ndarray,
    h: int,
    *,
    cost_bps: float = 1.0,
) -> dict:
    """Annualised OOS Sharpe of a sign(signal) long/short, after turnover cost.

    ``raw_fwd_ret`` is the *raw* (not vol-scaled) h-day forward log return, so
    the Sharpe is in real return units. Position = sign(signal); turnover cost
    charged on |Δposition| at ``cost_bps`` per side. Annualisation uses
    √(252/h); overlapping h-day windows make this an optimistic upper bound on
    a tradable Sharpe — reported as a screen, not a promise.
    """
    sig = np.asarray(signal, dtype=float)
    r = np.asarray(raw_fwd_ret, dtype=float)
    mask = np.isfinite(sig) & np.isfinite(r)
    sig, r = sig[mask], r[mask]
    if len(sig) < 20:
        return {"sharpe": float("nan"), "ann_ret": float("nan"), "n": int(len(sig))}
    pos = np.sign(sig)
    turnover = np.abs(np.diff(np.concatenate([[0.0], pos])))
    cost = turnover * (cost_bps / 1e4)
    pnl = pos * r - cost
    mu, sd = pnl.mean(), pnl.std(ddof=1)
    ann = np.sqrt(252.0 / h)
    return {
        "sharpe": float(mu / sd * ann) if sd > 0 else float("nan"),
        "ann_ret": float(mu * (252.0 / h)),
        "hit_rate": float((np.sign(pos) == np.sign(r)).mean()),
        "n": int(len(sig)),
    }


# --- classification-side ---------------------------------------------------


def classification_metrics(proba_up: np.ndarray, y_up: np.ndarray) -> dict:
    """AUC + Brier for P(up) vs realised up/down. Robust to degenerate inputs."""
    from sklearn.metrics import brier_score_loss, roc_auc_score

    p = np.asarray(proba_up, dtype=float)
    y = np.asarray(y_up, dtype=float)
    mask = np.isfinite(p) & np.isfinite(y)
    p, y = p[mask], y[mask]
    out = {"auc": float("nan"), "brier": float("nan"), "n": int(len(y))}
    if len(y) < 20 or len(np.unique(y)) < 2:
        return out
    try:
        out["auc"] = float(roc_auc_score(y, p))
        out["brier"] = float(brier_score_loss(y, p))
    except ValueError:
        pass
    return out


# --- multiple-testing discounts --------------------------------------------


def pbo_cscv(score_matrix: np.ndarray) -> dict:
    """Probability of Backtest Overfitting via Combinatorially-Symmetric CV.

    ``score_matrix`` is ``(n_folds, n_models)`` of an OOS performance metric
    (e.g. per-fold IC). We split the folds into every balanced (train, test)
    half, pick the in-sample-best model on the train half, and record its
    relative rank ``ω`` on the test half. PBO = P(logit(ω) < 0) = fraction of
    splits where the IS-winner is below the OOS median. >0.5 ⇒ selection is
    overfit noise. Needs an even, modest number of folds.
    """
    S = np.asarray(score_matrix, dtype=float)
    n_folds, n_models = S.shape
    if n_folds < 4 or n_models < 2:
        return {"pbo": float("nan"), "n_splits": 0, "note": "need >=4 folds, >=2 models"}
    half = n_folds // 2
    logits: list[float] = []
    fold_ids = list(range(n_folds))
    for train_folds in combinations(fold_ids, half):
        test_folds = [f for f in fold_ids if f not in train_folds]
        is_mean = np.nanmean(S[list(train_folds)], axis=0)
        oos_mean = np.nanmean(S[test_folds], axis=0)
        if np.all(np.isnan(is_mean)) or np.all(np.isnan(oos_mean)):
            continue
        best = int(np.nanargmax(is_mean))
        # Relative rank of the IS-winner among OOS scores (1 = best).
        order = stats.rankdata(oos_mean)
        omega = order[best] / (n_models + 1)
        omega = min(max(omega, 1e-6), 1 - 1e-6)
        logits.append(np.log(omega / (1 - omega)))
    if not logits:
        return {"pbo": float("nan"), "n_splits": 0}
    logits_arr = np.array(logits)
    return {
        "pbo": float((logits_arr <= 0).mean()),
        "n_splits": len(logits),
        "median_logit": float(np.median(logits_arr)),
    }


def deflated_sharpe(
    returns: np.ndarray,
    observed_sharpe: float,
    n_trials: int,
    sharpe_variance: float,
) -> dict:
    """Deflated Sharpe Ratio (Bailey & López de Prado, 2014).

    Deflates ``observed_sharpe`` (per-period, NOT annualised) for (a) the number
    of independent ``n_trials`` configurations tried and (b) the skew/kurtosis
    of ``returns``. Returns DSR = P(true SR > 0) given the multiple testing.
    DSR < ~0.95 means the best result is not distinguishable from luck.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = len(r)
    if n < 20 or not np.isfinite(observed_sharpe):
        return {"dsr": float("nan"), "sr0": float("nan"), "n": int(n)}
    skew = float(stats.skew(r))
    kurt = float(stats.kurtosis(r, fisher=False))  # non-excess
    # Expected maximum Sharpe under N independent trials (the deflation target).
    e = np.e
    gamma = 0.5772156649  # Euler–Mascheroni
    N = max(int(n_trials), 2)
    z1 = _NORM.ppf(1 - 1.0 / N)
    z2 = _NORM.ppf(1 - 1.0 / (N * e))
    sr0 = np.sqrt(max(sharpe_variance, 1e-12)) * ((1 - gamma) * z1 + gamma * z2)
    denom = np.sqrt(max(1 - skew * observed_sharpe + (kurt - 1) / 4 * observed_sharpe**2, 1e-9))
    dsr = _NORM.cdf((observed_sharpe - sr0) * np.sqrt(n - 1) / denom)
    return {"dsr": float(dsr), "sr0": float(sr0), "skew": skew, "kurt": kurt, "n": int(n)}
