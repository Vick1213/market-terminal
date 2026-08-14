"""Tests for app.ml.vol_grade (PLAN §13.9 step 4, §13.6 — the forecast
grader). Same plain-assert-function style as the other app/ml/tests modules
(each ``test_*`` is pytest-collectible; ``_run_all()`` is a pytest-free
fallback runner). Network-free: synthetic OHLC + ``ml_vol_scores`` rows are
seeded into temp DuckDB files, and synthetic ``day_signal_journal`` rows into
a temp SQLite file — the real ``data/market.duckdb`` / ``data/app.db`` are
never opened for writes.

Run:  cd apps/api && .venv/bin/python -m app.ml.tests.test_vol_grade
  or: cd apps/api && .venv/bin/python -m pytest app/ml/tests/test_vol_grade.py -q
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ml.vol_grade import (
    _PROMOTION_MIN_TRADING_DAYS,
    calibration_slope,
    ensure_grade_table,
    evaluate_promotion,
    grade_horizon,
    load_graded_frame,
    qlike,
    rmse,
    summarize_day_counterfactuals,
    write_grade,
)

_SCORES_DDL = """
CREATE TABLE ml_vol_scores (
    ts                  TIMESTAMP NOT NULL,
    symbol              VARCHAR NOT NULL,
    horizon             INTEGER NOT NULL,
    estimator           VARCHAR NOT NULL,
    pred_vol            DOUBLE,
    level_admissible    BOOLEAN NOT NULL,
    calib_a             DOUBLE,
    calib_b             DOUBLE,
    rank                INTEGER,
    pctile              DOUBLE,
    in_reference_panel  BOOLEAN NOT NULL,
    n_obs               INTEGER,
    created_at          TIMESTAMP NOT NULL,
    PRIMARY KEY (ts, symbol, horizon)
);
"""

_PRICE_DDL = """
CREATE TABLE ts_price (
    source VARCHAR, symbol VARCHAR, asset_class VARCHAR, ts TIMESTAMP,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE
);
"""

_JOURNAL_DDL = """
CREATE TABLE day_signal_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    decision TEXT NOT NULL,
    outcome TEXT,
    pnl REAL,
    context TEXT
);
"""

_NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _mkduck() -> DuckStore:
    tmp = tempfile.TemporaryDirectory()
    db = DuckStore(Path(tmp.name) / "market.duckdb")
    db._tmp = tmp
    db.execute(_SCORES_DDL)
    db.execute(_PRICE_DDL)
    return db


def _mksqlite() -> SqliteStore:
    tmp = tempfile.TemporaryDirectory()
    db = SqliteStore(Path(tmp.name) / "app.db")
    db._tmp = tmp
    db.execute(_JOURNAL_DDL)
    return db


# --- qlike / rmse / calibration_slope: known synthetic answers -------------------


def test_qlike_is_zero_when_predicted_equals_realized() -> None:
    v = np.array([0.01, 0.02, 0.015, 0.03])
    assert abs(qlike(v, v)) < 1e-12


def test_qlike_penalizes_underprediction_more_than_overprediction() -> None:
    # QLIKE is asymmetric: under-predicting a given realized vol costs more
    # than over-predicting by the same *ratio* the other way -- check the
    # textbook shape rather than a specific magnitude.
    realized = np.array([0.02])
    under = qlike(np.array([0.01]), realized)   # predicted HALF of realized
    over = qlike(np.array([0.04]), realized)    # predicted DOUBLE realized
    assert under > 0 and over > 0
    assert under > over


def test_rmse_known_answer() -> None:
    pred = np.array([0.01, 0.02, 0.03])
    real = np.array([0.02, 0.02, 0.02])
    # errors: -0.01, 0, 0.01 -> mean sq = (0.0001+0+0.0001)/3 -> sqrt
    expected = float(np.sqrt((0.0001 + 0.0 + 0.0001) / 3))
    assert abs(rmse(pred, real) - expected) < 1e-12


def test_calibration_slope_recovers_a_known_linear_relationship() -> None:
    rng = np.random.default_rng(0)
    pred = rng.uniform(0.005, 0.05, size=2000)
    realized = 0.9 * pred + 0.001  # a = 0.001, b = 0.9, EXACT (no noise)
    cal = calibration_slope(pred, realized)
    assert abs(cal["b"] - 0.9) < 1e-9
    assert abs(cal["a"] - 0.001) < 1e-9
    assert cal["r2"] > 0.999999
    assert cal["n"] == 2000


def test_calibration_slope_degenerate_with_too_few_points() -> None:
    cal = calibration_slope(np.array([0.01, 0.02]), np.array([0.01, 0.02]))
    assert np.isnan(cal["b"])
    assert cal["n"] == 2


# --- load_graded_frame / grade_horizon: synthetic price + score ----------------


def _seed_price(db: DuckStore, symbol: str, closes: list[float], start: str = "2020-01-01") -> None:
    dates = pd.bdate_range(start, periods=len(closes))
    rows = [
        ("yahoo", symbol, "equity", d.to_pydatetime(), c, c, c, c, 1000.0)
        for d, c in zip(dates, closes)
    ]
    db.executemany(
        "INSERT INTO ts_price (source, symbol, asset_class, ts, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?,?,?)", rows,
    )


def test_load_graded_frame_drops_unresolved_tail() -> None:
    """A score stamped on the LAST available price day has no h=5 forward
    window yet -- it must be silently absent from the graded frame, not
    zero-filled or raising."""
    db = _mkduck()
    try:
        closes = [100.0 * (1.01 ** i) for i in range(40)]
        _seed_price(db, "AAPL", closes)
        dates = pd.bdate_range("2020-01-01", periods=40)
        # A resolvable score (10 sessions before the end -- h=5 fits) ...
        resolvable_ts = dates[20].to_pydatetime()
        # ... and one stamped on the very last price day (unresolved at h=5).
        unresolved_ts = dates[39].to_pydatetime()
        db.executemany(
            "INSERT INTO ml_vol_scores (ts, symbol, horizon, estimator, pred_vol, "
            "level_admissible, calib_a, calib_b, rank, pctile, in_reference_panel, "
            "n_obs, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (resolvable_ts, "AAPL", 5, "har", 0.01, True, 0.0, 1.0, 1, 0.5, True, 100, _NOW),
                (unresolved_ts, "AAPL", 5, "har", 0.01, True, 0.0, 1.0, 1, 0.5, True, 100, _NOW),
            ],
        )
        df = load_graded_frame(db, 5)
    finally:
        db.close()
    assert len(df) == 1
    assert pd.Timestamp(df.iloc[0]["ts"]) == pd.Timestamp(resolvable_ts).normalize()


def test_grade_horizon_empty_when_nothing_resolved() -> None:
    db = _mkduck()
    try:
        m = grade_horizon(db, 21)
    finally:
        db.close()
    assert m["n_days"] == 0
    assert m["rank_ic"] is None
    assert m["qlike"] is None


def test_grade_horizon_recovers_known_rank_ic_and_calibration() -> None:
    """3 symbols, 3 scoring days, a KNOWN monotone predicted-vs-realized
    relationship -> rank IC must be exactly +1.0 every day (perfect
    agreement) and the calibration slope must recover the known linear map.
    (cross_sectional_ic needs >=3 DAYS to report a non-NaN mean/t-stat --
    see its docstring -- so a single-day fixture can't exercise this path.)
    """
    db = _mkduck()
    try:
        n = 80
        # Realized daily vol path controlled directly: LOW/MID/HIGH differ only
        # in the SIZE of daily moves, so GK/close-to-close-style realized vol
        # over any 5-session window is monotone LOW < MID < HIGH by construction
        # (deterministic alternating +/- step -- no rng, no edge effects at the
        # score dates chosen below).
        specs = {"LOW": 0.002, "MID": 0.01, "HIGH": 0.04}
        dates = pd.bdate_range("2020-01-01", periods=n + 1)
        close_series_by_sym: dict[str, pd.Series] = {}
        for sym, step in specs.items():
            rets = [step if i % 2 == 0 else -step for i in range(n)]
            closes = [100.0]
            for r in rets:
                closes.append(closes[-1] * (1 + r))
            _seed_price(db, sym, closes)
            close_series_by_sym[sym] = pd.Series(closes, index=dates)

        import app.ml.labels as labels
        score_positions = [30, 40, 50]  # 3 distinct scoring days, all well clear of the tail
        rows = []
        for pos in score_positions:
            score_ts = dates[pos].to_pydatetime()
            for sym in specs:
                realized_at_score = labels.forward_realized_vol(
                    close_series_by_sym[sym], 5, log=False
                ).loc[pd.Timestamp(score_ts)]
                pred = realized_at_score / 2.0  # exact known linear map -> calibration b=2.0
                rows.append((score_ts, sym, 5, "har", float(pred), True, 0.0, 1.0,
                             None, None, True, 100, _NOW))
        db.executemany(
            "INSERT INTO ml_vol_scores (ts, symbol, horizon, estimator, pred_vol, "
            "level_admissible, calib_a, calib_b, rank, pctile, in_reference_panel, "
            "n_obs, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows,
        )
        m = grade_horizon(db, 5, min_names_per_day=3)
    finally:
        db.close()

    assert m["n_days"] == 3
    assert abs(m["rank_ic"]["mean"] - 1.0) < 1e-9
    assert m["rank_ic"]["hit_rate"] == 1.0
    # predicted = realized/2 exactly on EVERY day -> realized = 0 + 2.0*predicted
    assert abs(m["calibration"]["b"] - 2.0) < 1e-6
    assert abs(m["calibration"]["a"] - 0.0) < 1e-6
    assert abs(m["rmse"]) > 0  # predicted != realized (half), RMSE must be nonzero


# --- summarize_day_counterfactuals ----------------------------------------------


def _journal_row(sq: SqliteStore, *, trade_date: str, decision: str = "acted",
                  outcome: str | None = None, pnl: float | None = None,
                  vol_ctx: dict | None = None) -> None:
    ctx = {"vol": vol_ctx} if vol_ctx is not None else {}
    sq.execute(
        "INSERT INTO day_signal_journal (trade_date, decision, outcome, pnl, context) "
        "VALUES (?,?,?,?,?)",
        [trade_date, decision, outcome, pnl, json.dumps(ctx)],
    )


def test_summarize_day_counterfactuals_missing_table_is_fail_soft() -> None:
    tmp = tempfile.TemporaryDirectory()
    sq = SqliteStore(Path(tmp.name) / "app.db")  # day_signal_journal never created
    try:
        out = summarize_day_counterfactuals(sq)
    finally:
        sq.close()
    assert out["available"] is False


def test_summarize_day_counterfactuals_counts_fee_gate_failures() -> None:
    sq = _mksqlite()
    try:
        _journal_row(sq, trade_date="2026-08-01", outcome="win", pnl=10.0, vol_ctx={
            "available": True,
            "counterfactual": {"actual_clears_fee_gate": True, "cf_clears_fee_gate": True,
                                "actual_qty": 10.0, "cf_qty": 20.0},
        })
        _journal_row(sq, trade_date="2026-08-02", outcome="loss", pnl=-6.0, vol_ctx={
            "available": True,
            "counterfactual": {"actual_clears_fee_gate": True, "cf_clears_fee_gate": False,
                                "actual_qty": 1.0, "cf_qty": 0.0},
        })
        _journal_row(sq, trade_date="2026-08-03", decision="skipped")  # not 'acted' -> excluded
        _journal_row(sq, trade_date="2026-08-04", vol_ctx={"available": False, "reason": "no_score"})
        out = summarize_day_counterfactuals(sq)
    finally:
        sq.close()
    assert out["available"] is True
    assert out["n_acted_decisions"] == 3   # excludes the 'skipped' row
    assert out["n_score_available"] == 2
    assert out["n_counterfactual_computed"] == 2
    assert out["fee_gate"]["n_counterfactual_would_fail"] == 1
    assert out["fee_gate"]["n_actual_would_fail"] == 0
    # pnl scaling: win 10.0 * (20/10)=20.0 ; loss -6.0 * (0/1)=0.0
    assert abs(out["pnl_scaling_estimate"]["actual_pnl_sum"] - 4.0) < 1e-9
    assert abs(out["pnl_scaling_estimate"]["counterfactual_pnl_sum_est"] - 20.0) < 1e-9
    assert out["stop_out_avoidance"]["modeled"] is False


# --- evaluate_promotion: insufficient-data gate + calibration-out-of-band -------


def test_promotion_is_insufficient_data_before_30_trading_days() -> None:
    metrics = {
        5: {"n_days": 10, "rank_ic": {"mean": 0.5}},
        21: {"n_days": 10, "calibration": {"b": 1.0}},
    }
    day_cf = {"available": True, "n_trading_days_with_scored_annotations": 10,
              "n_counterfactual_computed": 5, "fee_gate": {"n_counterfactual_would_fail": 0}}
    out = evaluate_promotion(metrics, day_cf)
    for key in out:
        assert out[key]["status"] == "insufficient_data", key
    assert out["criterion_1_live_rank_ic_h5"]["observed_ic"] == 0.5  # value shown even while insufficient


def test_promotion_passes_criterion_1_and_2_once_days_clear_and_thresholds_met() -> None:
    n = _PROMOTION_MIN_TRADING_DAYS
    metrics = {
        5: {"n_days": n, "rank_ic": {"mean": 0.35}},
        21: {"n_days": n, "calibration": {"b": 1.0}},
    }
    out = evaluate_promotion(metrics, None)
    assert out["criterion_1_live_rank_ic_h5"]["status"] == "pass"
    assert out["criterion_2_level_calibration_h21"]["status"] == "pass"


def test_promotion_fails_criterion_1_below_threshold() -> None:
    n = _PROMOTION_MIN_TRADING_DAYS
    metrics = {5: {"n_days": n, "rank_ic": {"mean": 0.10}}, 21: {"n_days": 0, "calibration": None}}
    out = evaluate_promotion(metrics, None)
    assert out["criterion_1_live_rank_ic_h5"]["status"] == "fail"


def test_promotion_calibration_out_of_band_fails() -> None:
    """The rule §13.4/§13.6 exists to trip: a live slope drifting outside
    [0.8, 1.2] must fail criterion 2, not silently pass."""
    n = _PROMOTION_MIN_TRADING_DAYS
    metrics = {5: {"n_days": 0, "rank_ic": None}, 21: {"n_days": n, "calibration": {"b": 1.35}}}
    out = evaluate_promotion(metrics, None)
    assert out["criterion_2_level_calibration_h21"]["status"] == "fail"
    assert out["criterion_2_level_calibration_h21"]["observed_slope"] == 1.35

    metrics_low = {5: {"n_days": 0, "rank_ic": None}, 21: {"n_days": n, "calibration": {"b": 0.5}}}
    out_low = evaluate_promotion(metrics_low, None)
    assert out_low["criterion_2_level_calibration_h21"]["status"] == "fail"


def test_promotion_criterion_3_is_not_modeled_never_a_fake_pass() -> None:
    n = _PROMOTION_MIN_TRADING_DAYS
    metrics = {5: {"n_days": n, "rank_ic": {"mean": 0.5}}, 21: {"n_days": n, "calibration": {"b": 1.0}}}
    day_cf = {"available": True, "n_trading_days_with_scored_annotations": n,
              "n_counterfactual_computed": 40, "fee_gate": {"n_counterfactual_would_fail": 0},
              "stop_out_avoidance": {"note": "not modeled"}}
    out = evaluate_promotion(metrics, day_cf)
    assert out["criterion_3_counterfactual_stopouts_reduced"]["status"] == "not_modeled"


def test_promotion_criterion_4_fails_when_counterfactuals_would_miss_the_gate() -> None:
    n = _PROMOTION_MIN_TRADING_DAYS
    metrics = {5: {"n_days": n, "rank_ic": {"mean": 0.5}}, 21: {"n_days": n, "calibration": {"b": 1.0}}}
    day_cf = {"available": True, "n_trading_days_with_scored_annotations": n,
              "n_counterfactual_computed": 40, "fee_gate": {"n_counterfactual_would_fail": 3}}
    out = evaluate_promotion(metrics, day_cf)
    assert out["criterion_4_counterfactual_clears_fee_gate"]["status"] == "fail"
    assert out["criterion_4_counterfactual_clears_fee_gate"]["n_counterfactual_trades_below_gate"] == 3


# --- persistence: ml_vol_grade accrues -------------------------------------------


def test_write_grade_roundtrip() -> None:
    db = _mkduck()
    try:
        ensure_grade_table(db)
        result = {
            "run_ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metrics": {5: {"n_days": 0, "as_of_start": None, "as_of_end": None},
                        21: {"n_days": 0, "as_of_start": None, "as_of_end": None}},
            "day_counterfactual": {"available": False},
            "promotion": {"criterion_1_live_rank_ic_h5": {"status": "insufficient_data"}},
        }
        write_grade(db, result)
        row = db.fetchone("SELECT run_ts, promotion_json FROM ml_vol_grade")
    finally:
        db.close()
    assert row is not None
    assert "insufficient_data" in row[1]


def _run_all() -> int:
    checks = [
        test_qlike_is_zero_when_predicted_equals_realized,
        test_qlike_penalizes_underprediction_more_than_overprediction,
        test_rmse_known_answer,
        test_calibration_slope_recovers_a_known_linear_relationship,
        test_calibration_slope_degenerate_with_too_few_points,
        test_load_graded_frame_drops_unresolved_tail,
        test_grade_horizon_empty_when_nothing_resolved,
        test_grade_horizon_recovers_known_rank_ic_and_calibration,
        test_summarize_day_counterfactuals_missing_table_is_fail_soft,
        test_summarize_day_counterfactuals_counts_fee_gate_failures,
        test_promotion_is_insufficient_data_before_30_trading_days,
        test_promotion_passes_criterion_1_and_2_once_days_clear_and_thresholds_met,
        test_promotion_fails_criterion_1_below_threshold,
        test_promotion_calibration_out_of_band_fails,
        test_promotion_criterion_3_is_not_modeled_never_a_fake_pass,
        test_promotion_criterion_4_fails_when_counterfactuals_would_miss_the_gate,
        test_write_grade_roundtrip,
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
