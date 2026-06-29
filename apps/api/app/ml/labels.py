"""Targets / labels for the ML meta-signal (PLAN §12.1).

Cardinal rule: **predict log returns, never price levels.** Price has a unit
root; a model trained on levels memorises "tomorrow ≈ today", scores a fake
R², and has zero edge. Log returns are ~stationary, additive, scale-free.

Every label here is a *forward* quantity realised at ``t + h`` (or at a
triple-barrier touch), so a row's decision time ``t`` and its label-end time are
both returned — ``cv.py`` needs the label-end time to purge overlapping samples.

Pure numpy/pandas. No look-ahead inside any *feature*-side helper (the vol used
to scale is trailing, knowable at ``t``); the label value itself is, by
definition, the future we are trying to predict.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# Trading days per year — annualisation / span defaults.
_TRADING_YEAR = 252


# --- volatility (trailing, knowable at the decision time t) -----------------


def ewma_vol(close: pd.Series, span: int = 32) -> pd.Series:
    """EWMA volatility of daily log returns, in *daily* return units.

    Trailing only: ``σ_t`` uses returns up to and including day ``t`` — it is
    knowable at the close of ``t``, so scaling the forward label by it does not
    leak. ``span`` ≈ 32 sessions ≈ a 1.5-month half-life, the usual choice for
    a daily vol-target.
    """
    r = np.log(close).diff()
    # min_periods guards the warm-up window from a near-zero σ that would blow
    # up the vol-scaled label.
    return r.ewm(span=span, min_periods=max(8, span // 2)).std()


def realized_vol(close: pd.Series, window: int = 21) -> pd.Series:
    """Trailing realised vol (rolling std of daily log returns), daily units."""
    r = np.log(close).diff()
    return r.rolling(window, min_periods=max(5, window // 2)).std()


def annualize_vol(daily_vol: pd.Series) -> pd.Series:
    return daily_vol * np.sqrt(_TRADING_YEAR)


# --- log-return targets -----------------------------------------------------


def forward_log_return(close: pd.Series, h: int) -> pd.Series:
    """``r_t = ln(P_{t+h} / P_t)`` aligned at the *decision* day ``t``.

    The value is realised ``h`` sessions later, so the tail ``h`` rows are NaN
    (no future yet). This is the raw regression target before vol-scaling.
    """
    if h < 1:
        raise ValueError("horizon h must be >= 1")
    logp = np.log(close)
    return logp.shift(-h) - logp


def vol_scaled_label(
    close: pd.Series, h: int, *, vol_span: int = 32, floor: float = 1e-4
) -> pd.Series:
    """Primary target (PLAN §12.1): ``r_{t→t+h} / (σ_t · √h)``.

    σ_t is the trailing EWMA daily vol (knowable at ``t``); ``√h`` puts the
    h-day return on a comparable per-√time footing so high-vol regimes don't
    dominate the loss. ``floor`` clamps the warm-up σ so early rows can't
    explode. This is the single highest-leverage modelling choice in §12.
    """
    r = forward_log_return(close, h)
    sigma = (ewma_vol(close, span=vol_span) * np.sqrt(h)).clip(lower=floor)
    return r / sigma


def forward_realized_vol(close: pd.Series, h: int, *, log: bool = True) -> pd.Series:
    """**Volatility target** (PLAN §12 lever #2): realised vol over the *next*
    ``h`` sessions, ``std(log-returns over (t, t+h])``, in daily units, aligned
    at the decision day ``t``.

    Unlike direction, vol is genuinely forecastable (vol clusters) — this is the
    one free-data target where an honest, gate-clearing edge is plausible. Modeled
    in log space by default (vol is right-skewed and ~log-normal; ``log=True``
    predicts ``ln σ`` so the loss isn't dominated by crisis spikes). The tail
    ``h`` rows are NaN (no future window yet).
    """
    if h < 1:
        raise ValueError("horizon h must be >= 1")
    r = np.log(close).diff()
    # Forward window (t, t+h]: shift the trailing rolling-std back by h so it sits
    # at the decision day. min_periods just below h tolerates the odd missing day.
    fwd = r.rolling(h, min_periods=max(2, h - 1)).std().shift(-h)
    if log:
        return np.log(fwd.clip(lower=1e-6))
    return fwd


# --- triple-barrier (López de Prado) ---------------------------------------


@dataclass(frozen=True)
class TripleBarrier:
    """One labelled event.

    label       : +1 profit-take hit first, -1 stop hit first, 0 vertical
                  (time) barrier hit first.
    ret         : log return realised at the touched barrier.
    t_decision  : the decision day (index position).
    t_touch     : index position where a barrier was first touched (≤ t + h).
    """

    label: int
    ret: float
    t_decision: int
    t_touch: int


def triple_barrier_labels(
    close: pd.Series,
    h: int,
    *,
    pt_mult: float = 2.0,
    sl_mult: float = 2.0,
    vol_span: int = 32,
    side: pd.Series | None = None,
) -> pd.DataFrame:
    """Triple-barrier labelling with barriers set in **vol units** (§12.1).

    For each day ``t`` we look ahead up to ``h`` sessions and see which barrier
    is touched first:

      * upper  = +``pt_mult`` · σ_t · √(elapsed)   → label +1
      * lower  = -``sl_mult`` · σ_t · √(elapsed)   → label -1
      * vertical (``h`` sessions elapse)           → label 0 (sign of ret)

    Barriers widen with √(elapsed days) so they track a diffusion, not a flat
    band. ``side`` (optional, +1/-1 per row) flips the barriers for a primary
    long/short signal — the input to **meta-labelling** (§12.1): the label then
    answers "was the primary signal's bet correct?".

    Returns a DataFrame indexed like ``close`` with columns
    ``[label, ret, t_touch_pos, t_end]`` where ``t_end`` is the *date* the label
    resolved (for ``cv.py`` purge). Rows without a full ``h``-day look-ahead are
    dropped.
    """
    if h < 1:
        raise ValueError("horizon h must be >= 1")

    logp = np.log(close).to_numpy()
    sigma = ewma_vol(close, span=vol_span).to_numpy()
    idx = close.index
    n = len(close)
    side_arr = (
        np.ones(n)
        if side is None
        else side.reindex(close.index).fillna(0.0).to_numpy()
    )

    out_label: list[int] = []
    out_ret: list[float] = []
    out_touch: list[int] = []
    out_pos: list[int] = []

    for t in range(n - h):
        s = sigma[t]
        if not np.isfinite(s) or s <= 0:
            continue  # warm-up: no usable vol yet
        sd = side_arr[t]
        if sd == 0:
            sd = 1.0
        touched = 0
        touch_pos = t + h  # default: vertical barrier
        ret_at_touch = logp[t + h] - logp[t]
        for k in range(1, h + 1):
            elapsed = np.sqrt(k)
            r = (logp[t + k] - logp[t]) * sd  # signed by the primary side
            up = pt_mult * s * elapsed
            dn = -sl_mult * s * elapsed
            if r >= up:
                touched = 1
                touch_pos = t + k
                ret_at_touch = (logp[t + k] - logp[t])
                break
            if r <= dn:
                touched = -1
                touch_pos = t + k
                ret_at_touch = (logp[t + k] - logp[t])
                break
        if touched == 0:
            # vertical barrier: label by realised sign (meta-label collapses
            # this to "did we make money" via the `side` sign downstream).
            touched = int(np.sign(ret_at_touch * sd)) or 0
        out_label.append(int(touched))
        out_ret.append(float(ret_at_touch))
        out_touch.append(touch_pos)
        out_pos.append(t)

    pos = np.array(out_pos, dtype=int)
    return pd.DataFrame(
        {
            "label": out_label,
            "ret": out_ret,
            "t_touch_pos": out_touch,
            "t_end": idx[np.array(out_touch, dtype=int)],
        },
        index=idx[pos],
    )


def meta_labels(primary_side: pd.Series, tb: pd.DataFrame) -> pd.Series:
    """Meta-label (§12.1): 1 if the primary signal's directional bet paid off,
    else 0. Feeds the second "bet-sizing" model. ``primary_side`` is +1/-1;
    ``tb`` is the output of :func:`triple_barrier_labels` run with that side.
    """
    side = primary_side.reindex(tb.index).fillna(0.0)
    won = np.sign(tb["ret"]) == np.sign(side)
    return won.astype(int).where(side != 0, 0)


def label_times(decision_index: pd.DatetimeIndex, h: int) -> pd.Series:
    """For the vol-scaled / log-return targets (fixed ``h`` horizon), the label
    of row ``t`` resolves at ``t + h`` sessions. Returns a Series mapping each
    decision date → its label-end date, which ``cv.py`` purges on. The last
    ``h`` rows have no resolved label and are dropped.
    """
    idx = pd.DatetimeIndex(decision_index)
    end = idx[h:]
    return pd.Series(end, index=idx[: len(idx) - h], name="t_end")
