"""Leakage harness gate (PLAN §12.8 step 1).

> Ship NOTHING past phase 1 until a deliberately-leaky feature is provably
> caught by the CV harness.

This module IS that proof. It injects known-future information three ways and
asserts the harness neutralises each:

  1. release_join never surfaces a value before its release date, and the
     assert_pit_safe guard fires on a corrupted (future-dated) matrix.
  2. A feature that literally equals the *future* label scores OOS rank-IC ≈ 1
     when leaked, and ≈ 0 once routed through the release-time join — i.e. the
     PIT join collapses the leak.
  3. PurgedKFold removes every training sample whose overlapping label window
     intersects the test fold (a plain split would not), killing the
     overlapping-label leak that inflates daily-return backtests.

pytest is not installed in the api venv, so each check is a plain function with
``assert`` and a ``__main__`` runner prints PASS/FAIL. ``pytest`` would also
collect the ``test_*`` functions unchanged once it's added.

Run:  cd apps/api && .venv/bin/python -m app.ml.tests.test_leakage
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.ml import labels
from app.ml.cv import PurgedKFold, WalkForward, oos_rank_ic, spearman_ic
from app.ml.dataset import (
    LookaheadError,
    assert_pit_safe,
    release_join,
)

H = 5  # headline horizon


def _synthetic_prices(n: int = 900, seed: int = 7) -> pd.Series:
    """Deterministic geometric random walk on business days."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2021-06-10", periods=n)
    rets = rng.normal(0.0003, 0.011, size=n)
    close = 400 * np.exp(np.cumsum(rets))
    return pd.Series(close, index=dates, name="close")


# --- check 1: release_join is leak-safe; the guard catches corruption -------


def test_release_join_is_leak_safe() -> None:
    cal = pd.bdate_range("2026-01-01", periods=40)
    # A weekly feature observed each Monday, released 5 calendar days later.
    obs = pd.DataFrame(
        {
            "ref_date": pd.bdate_range("2026-01-05", periods=6, freq="W-MON"),
            "value": np.arange(6, dtype=float),
        }
    )
    joined = release_join(cal, obs, release_lag_days=5)

    # Mechanism: no value is visible before its release date.
    for ref, val in zip(obs["ref_date"], obs["value"]):
        rel = ref + pd.Timedelta(days=5)
        before = joined.loc[joined.index < rel, "value"]
        assert (before != val).all() or before.isna().all(), (
            f"value {val} leaked before its release {rel.date()}"
        )

    # Guard passes on a correct join...
    assert_pit_safe(joined)

    # ...and fires when a release date is hand-corrupted into the future.
    corrupt = joined.copy()
    first_valid = corrupt["_release_date"].first_valid_index()
    corrupt.loc[first_valid, "_release_date"] = first_valid + pd.Timedelta(days=10)
    raised = False
    try:
        assert_pit_safe(corrupt)
    except LookaheadError:
        raised = True
    assert raised, "assert_pit_safe failed to catch a future-dated value"


# --- check 2: a future-label feature is neutralised by the PIT join ---------


def test_future_feature_neutralized() -> None:
    close = _synthetic_prices()
    y = labels.vol_scaled_label(close, H)

    # Decision rows = those with a resolved h-day label.
    lt = labels.label_times(close.index, H)
    valid = y.reindex(lt.index).dropna().index
    lt = lt.reindex(valid)
    y_v = y.reindex(valid)
    label_times = pd.DataFrame({"t_decision": valid, "t_end": lt.values}, index=valid)

    # --- LEAKED feature: literally the future label, aligned at t. ----------
    leaked = pd.Series(y_v.to_numpy(), index=valid)
    wf = WalkForward(n_splits=5, min_train=200, embargo_pct=0.01)
    res_leak = oos_rank_ic(leaked, y_v, label_times, wf)
    assert res_leak["oos_ic"] > 0.9, (
        f"positive control failed: a future-label feature should score IC≈1, "
        f"got {res_leak['oos_ic']:.3f} (the evaluator can't see the leak)"
    )

    # --- PIT version: the same values, but only knowable h sessions later. ---
    # ref_date = the session the h-day return is realised; release lag 0 → at
    # decision t the as-of join returns a value realised at/<= t (a PAST
    # return), so it can't peek at t→t+h.
    pos = {d: i for i, d in enumerate(close.index)}
    known_date = [close.index[pos[d] + H] for d in valid]
    feat = pd.DataFrame({"ref_date": known_date, "value": y_v.to_numpy()})
    joined = release_join(pd.DatetimeIndex(valid), feat, release_lag_days=0)
    assert_pit_safe(joined)  # the join itself must be clean
    pit_feat = joined["value"].reindex(valid)
    res_pit = oos_rank_ic(pit_feat, y_v, label_times, wf)

    assert abs(res_pit["oos_ic"]) < 0.15, (
        f"PIT join did NOT neutralise the future feature: OOS IC "
        f"{res_pit['oos_ic']:.3f} (expected ≈0)"
    )
    # And the collapse must be large — the harness demonstrably removes the edge.
    assert res_leak["oos_ic"] - abs(res_pit["oos_ic"]) > 0.7, (
        "leak collapse too small; PIT join is not doing its job"
    )


# --- check 3: purge removes overlapping-label train samples -----------------


def test_purge_removes_overlapping_labels() -> None:
    # 600 daily decisions, each with an h-day overlapping label window.
    dates = pd.bdate_range("2021-06-10", periods=600 + H)
    dec = dates[:600]
    end = dates[H : 600 + H]
    label_times = pd.DataFrame({"t_decision": dec, "t_end": end}, index=dec)

    pk = PurgedKFold(n_splits=5, embargo_pct=0.02)
    splits = list(pk.split(label_times))
    assert len(splits) == 5

    t_dec = label_times["t_decision"].to_numpy()
    t_end = label_times["t_end"].to_numpy()
    naive_overlap_seen = False

    for train_pos, test_pos in splits:
        test_start = t_dec[test_pos].min()
        test_end = t_end[test_pos].max()
        # No purged training sample's label window may intersect the test span.
        for p in train_pos:
            overlaps = (t_end[p] >= test_start) and (t_dec[p] <= test_end)
            assert not overlaps, (
                f"purge leak: train sample at {pd.Timestamp(t_dec[p]).date()} "
                f"overlaps test span"
            )
        # A naive (unpurged) split = all-other-positions; confirm it WOULD have
        # included overlapping samples (so purge is doing real work, not no-op).
        all_pos = np.arange(len(label_times))
        naive_train = np.setdiff1d(all_pos, test_pos, assume_unique=True)
        for p in naive_train:
            if (t_end[p] >= test_start) and (t_dec[p] <= test_end):
                naive_overlap_seen = True
                break

    assert naive_overlap_seen, (
        "expected the unpurged split to contain overlapping samples; if not, "
        "the test fixture isn't exercising purge"
    )


# --- check 4: spearman_ic sanity --------------------------------------------


def test_spearman_ic_basic() -> None:
    x = np.arange(50, dtype=float)
    assert spearman_ic(x, x) > 0.99
    assert spearman_ic(x, -x) < -0.99
    rng = np.random.default_rng(1)
    assert abs(spearman_ic(rng.normal(size=200), rng.normal(size=200))) < 0.25


def _run_all() -> int:
    checks = [
        test_release_join_is_leak_safe,
        test_future_feature_neutralized,
        test_purge_removes_overlapping_labels,
        test_spearman_ic_basic,
    ]
    failed = 0
    for c in checks:
        try:
            c()
            print(f"PASS  {c.__name__}")
        except Exception as exc:  # noqa: BLE001 - test harness reporting
            failed += 1
            print(f"FAIL  {c.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
