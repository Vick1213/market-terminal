"""Forecast grader for the per-name vol scorer (PLAN §13.9 step 4, §13.6).

**Grades the FORECAST first, P&L second** — exactly the ordering §13.6
mandates, because forecast grading (predicted vs realised vol) has no
confound, while P&L grading is confounded the instant a vol rule changes
behaviour — which Phase A (``vol_shadow.py``) is explicitly forbidden from
doing. This module never touches a bot decision path; it only reads
``ml_vol_scores`` + ``ts_price`` (primary) and ``day_signal_journal``
(secondary, via the shadow annotations Phase A already wrote) and writes to
a brand-new ``ml_vol_grade`` table nothing else reads.

Primary — for every scored (symbol, ts, horizon) whose forward window has
now resolved (``labels.forward_realized_vol`` stops being NaN once ``h``
sessions have actually elapsed past ``ts``):
  * cross-sectional rank IC per day (``app.ml.cross_section.cross_sectional_ic``
    — the SAME function the original §13.0a/§13.0c backtest numbers came
    from, reused rather than reimplemented so a grader bug can't silently
    diverge from what "IC" meant when the promotion bar was set).
  * QLIKE (Patton 2011) and RMSE in level space.
  * the LIVE calibration slope — OLS of realised on predicted — which is
    what §13.4 rule 2 trips on: outside ``[0.8, 1.2]``, ``pred_vol`` stops
    being an admissible sizing level.

Secondary — a best-effort counterfactual-outcome summary from the day
sleeve's shadow annotations (``day_signal_journal.context['vol']``, written
by ``vol_shadow.day_vol_shadow``): whether any counterfactual trade would
have failed the $5 min-risk fee gate (§13.6 criterion 4), plus an
approximate linear P&L rescaling. It does NOT attempt "would vol-scaled
stops have avoided a stop-out" — Phase A's counterfactual deliberately holds
stop DISTANCE fixed (see ``vol_shadow.day_vol_shadow``'s docstring), so a
size-only counterfactual cannot change which stops get hit; that is recorded
explicitly as a known limitation rather than approximated.

**§13.6's four promotion criteria are reported explicitly** — pass / fail /
insufficient_data, each with its observed value next to its threshold.
Every criterion needs ≥30 TRADING DAYS of live scores before it can be
anything but ``insufficient_data`` — this module will say so even when the
underlying number would technically clear the bar on a handful of days
(§13.6: "never a premature pass"). Anchors are the §13.0c RECENT-fold
numbers (IC 0.46 h=5 / 0.65 h=21, 2023-2026), not the full-sample headline
(0.52/0.70) — live expectations decay measurably from the GFC era forward.

Run:  cd apps/api && .venv/bin/python -m app.ml.vol_grade --dry-run
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from app.ml import labels
from app.ml.cross_section import cross_sectional_ic
from app.ml.vol_scores import (
    LEVEL_ADMISSIBLE_HORIZON,
    _load_ohlc_routed,
    _open_universe_reader,
)

log_name = "ml.vol_grade"

GRADED_HORIZONS = (5, 21)
LEVEL_HORIZON = LEVEL_ADMISSIBLE_HORIZON  # 21 -- the only horizon calibration/RMSE/QLIKE governs sizing at

# PLAN §13.6 promotion bar, pre-registered before any live data existed.
_PROMOTION_MIN_TRADING_DAYS = 30
_IC_H5_THRESHOLD = 0.30
_CALIB_BAND = (0.8, 1.2)
# §13.0c RECENT-fold (2023-2026) backtest anchor -- NOT the full-sample 0.52/0.70
# headline, per §13.4 rule 3 ("anchor live expectations to the most recent fold").
_RECENT_FOLD_IC_ANCHOR = {5: 0.46, 21: 0.65}

_MIN_NAMES_PER_DAY = 5  # mirrors cross_sectional_ic's own default


# --- level metrics: QLIKE / RMSE / calibration -------------------------------


def qlike(pred_vol: np.ndarray, realized_vol: np.ndarray) -> float:
    """Patton (2011) QLIKE loss in VARIANCE space (the standard vol-forecast
    loss: scale-sensitive, penalises under-prediction more than over,
    minimised at 0 when predicted==realised exactly)::

        QLIKE = mean( real_var/pred_var - ln(real_var/pred_var) - 1 )

    ``pred_vol``/``realized_vol`` are DAILY SIGMA (level) units — squared here
    to variance, matching how §13.0a's QLIKE numbers were produced. Non-finite
    / non-positive pairs are dropped; returns NaN if nothing is left.
    """
    pv = np.asarray(pred_vol, dtype=float)
    rv = np.asarray(realized_vol, dtype=float)
    mask = np.isfinite(pv) & np.isfinite(rv) & (pv > 0) & (rv > 0)
    if not mask.any():
        return float("nan")
    pred_var = pv[mask] ** 2
    real_var = rv[mask] ** 2
    ratio = real_var / pred_var
    return float(np.mean(ratio - np.log(ratio) - 1.0))


def rmse(pred_vol: np.ndarray, realized_vol: np.ndarray) -> float:
    """RMSE in daily-sigma LEVEL units (not variance) -- matches §13.0a's table."""
    pv = np.asarray(pred_vol, dtype=float)
    rv = np.asarray(realized_vol, dtype=float)
    mask = np.isfinite(pv) & np.isfinite(rv)
    if not mask.any():
        return float("nan")
    return float(np.sqrt(np.mean((rv[mask] - pv[mask]) ** 2)))


def calibration_slope(pred_vol: np.ndarray, realized_vol: np.ndarray) -> dict:
    """OLS ``realised = a + b*predicted`` -- the SAME regression
    ``vol_scores._pooled_calibration`` fits at write-time, re-derived here
    from what actually resolved (the "live" slope §13.4 rule 2 / §13.6
    criterion 2 watches for drifting outside ``[0.8, 1.2]``). Returns
    ``{'a', 'b', 'r2', 'n'}``; ``b``/``a``/``r2`` are NaN when fewer than 3
    finite pairs are available (degenerate fit -- never a false calibration
    reading off ~nothing)."""
    pv = np.asarray(pred_vol, dtype=float)
    rv = np.asarray(realized_vol, dtype=float)
    mask = np.isfinite(pv) & np.isfinite(rv)
    n = int(mask.sum())
    if n < 3:
        return {"a": float("nan"), "b": float("nan"), "r2": float("nan"), "n": n}
    X = np.column_stack([np.ones(n), pv[mask]])
    y = rv[mask]
    beta, _resid, mat_rank, _sv = np.linalg.lstsq(X, y, rcond=None)
    if mat_rank < 2 or not np.all(np.isfinite(beta)):
        return {"a": float("nan"), "b": float("nan"), "r2": float("nan"), "n": n}
    a, b = float(beta[0]), float(beta[1])
    yhat = X @ beta
    ss_res = float(np.sum((y - yhat) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 0 else float("nan")
    return {"a": a, "b": b, "r2": r2, "n": n}


# --- resolved-score loading ----------------------------------------------------


_GRADE_COLS = ["ts", "symbol", "estimator", "pred_vol", "rank", "pctile",
               "in_reference_panel", "n_obs"]


def load_graded_frame(duck, horizon: int, *, uni_reader=None,
                       reference_panel: Sequence[str] | None = None) -> pd.DataFrame:
    """One row per (symbol, ts) already scored at ``horizon`` in
    ``ml_vol_scores`` whose forward window has RESOLVED — i.e.
    ``labels.forward_realized_vol`` returns a finite value, which it can only
    do once ``horizon`` sessions have actually elapsed past ``ts`` in that
    symbol's price history. Unresolved (too-recent) rows are silently absent,
    not zero-filled — grading only ever sees what could genuinely be known.

    ``uni_reader`` optionally routes symbol OHLC through ``universe.duckdb``
    the SAME way ``vol_scores._load_ohlc_routed`` does when scoring (research-
    panel names' price history lives there, not in ``market.duckdb``) — reused
    directly rather than reimplemented so the grader can never silently
    diverge from what the scorer itself considered "clean OHLC".

    ``reference_panel`` is an OPT-IN restriction: when given, only those
    symbols are graded (e.g. "grade panel members only, ignore watchlist
    extras"); the default (``None``) grades every symbol ``ml_vol_scores``
    happened to score, which is the right default for FORECAST grading
    (unlike the sizing counterfactual in ``vol_shadow.py``, a graded
    cross-section doesn't need to stay a fixed, un-reshuffled panel).
    """
    empty = pd.DataFrame(columns=_GRADE_COLS + ["realized_vol"])
    try:
        rows = duck.fetchall(
            "SELECT ts, symbol, estimator, pred_vol, rank, pctile, in_reference_panel, n_obs "
            "FROM ml_vol_scores WHERE horizon = ? AND level_admissible = TRUE "
            "AND pred_vol IS NOT NULL ORDER BY symbol, ts",
            [horizon],
        )
    except Exception:
        return empty
    if not rows:
        return empty
    df = pd.DataFrame(rows, columns=_GRADE_COLS)
    if reference_panel is not None:
        df = df[df["symbol"].isin(set(reference_panel))]
        if df.empty:
            return empty
    df["ts"] = pd.to_datetime(df["ts"]).dt.normalize()

    out_frames: list[pd.DataFrame] = []
    for sym, g in df.groupby("symbol", sort=False):
        ohlc = _load_ohlc_routed(sym, uni_reader, duck)
        if ohlc is None or ohlc.empty:
            continue
        realized = labels.forward_realized_vol(ohlc["c"], horizon, log=False)
        realized.index = pd.DatetimeIndex(realized.index).normalize()
        gi = g.set_index("ts")
        gi = gi.assign(realized_vol=realized.reindex(gi.index).to_numpy())
        out_frames.append(gi.reset_index())
    if not out_frames:
        return empty
    full = pd.concat(out_frames, ignore_index=True)
    return full.dropna(subset=["realized_vol", "pred_vol"]).reset_index(drop=True)


def grade_horizon(duck, horizon: int, *, uni_reader=None,
                   reference_panel: Sequence[str] | None = None,
                   min_names_per_day: int = _MIN_NAMES_PER_DAY) -> dict:
    """Grade one horizon's LIVE forecast against realised vol. Returns a dict
    shaped for both the JSON response and ``ml_vol_grade.metrics_json``; an
    empty/insufficient frame returns ``n_days=0`` with every metric ``None``
    rather than raising or fabricating a number from too little data."""
    df = load_graded_frame(duck, horizon, uni_reader=uni_reader, reference_panel=reference_panel)
    if df.empty:
        return {"n_days": 0, "n_obs": 0, "rank_ic": None, "qlike": None, "rmse": None,
                "calibration": None, "as_of_start": None, "as_of_end": None}

    ic = cross_sectional_ic(
        df["ts"].to_numpy(), df["pred_vol"].to_numpy(), df["realized_vol"].to_numpy(),
        min_names=min_names_per_day,
    )
    q = qlike(df["pred_vol"].to_numpy(), df["realized_vol"].to_numpy())
    r = rmse(df["pred_vol"].to_numpy(), df["realized_vol"].to_numpy())
    cal = calibration_slope(df["pred_vol"].to_numpy(), df["realized_vol"].to_numpy())
    n_days = int(df["ts"].nunique())
    return {
        "n_days": n_days,
        "n_obs": int(len(df)),
        "rank_ic": {
            "mean": None if not np.isfinite(ic["ic"]) else float(ic["ic"]),
            "t": None if not np.isfinite(ic["ic_t"]) else float(ic["ic_t"]),
            "n_days": int(ic["n_days"]),
            "hit_rate": None if not np.isfinite(ic["hit"]) else float(ic["hit"]),
        },
        "qlike": None if not np.isfinite(q) else q,
        "rmse": None if not np.isfinite(r) else r,
        "calibration": {
            "a": None if not np.isfinite(cal["a"]) else cal["a"],
            "b": None if not np.isfinite(cal["b"]) else cal["b"],
            "r2": None if not np.isfinite(cal["r2"]) else cal["r2"],
            "n": cal["n"],
        },
        "as_of_start": str(df["ts"].min().date()),
        "as_of_end": str(df["ts"].max().date()),
    }


# --- secondary: day-sleeve counterfactual outcome summary ---------------------


def summarize_day_counterfactuals(sqlite) -> dict:
    """Best-effort secondary summary from the day sleeve's shadow annotations
    (``day_signal_journal.context['vol']``, written by
    ``vol_shadow.day_vol_shadow``). Read-only, never raises. Reports:
      * fee-gate impact (§13.6 criterion 4): how many counterfactual trades
        would have fallen below the $5 min-risk gate after vol scaling.
      * an APPROXIMATE linear P&L rescaling (``pnl * cf_qty/actual_qty``) for
        acted trades with a graded outcome — informational only, NOT
        fee/slippage-aware, and NOT used for any of the four hard promotion
        criteria.
      * ``stop_out_avoidance`` is explicitly reported as NOT MODELED: Phase
        A's counterfactual holds stop distance fixed (see
        ``vol_shadow.day_vol_shadow``), so a size-only counterfactual cannot
        change which stops would have been hit — approximating that would be
        fabricating a result this data cannot support.
    """
    try:
        rows = sqlite.fetchall(
            "SELECT trade_date, outcome, pnl, context FROM day_signal_journal WHERE decision = 'acted'"
        )
    except Exception as exc:
        return {"available": False, "reason": f"day_signal_journal unavailable: {exc}"}

    n_total = 0
    n_score_available = 0
    n_cf_computed = 0
    n_actual_fails_gate = 0
    n_cf_fails_gate = 0
    trading_dates: set[str] = set()
    pnl_actual_sum = 0.0
    pnl_cf_sum = 0.0
    n_pnl_pairs = 0

    for r in rows:
        n_total += 1
        raw_ctx = r["context"]
        if not raw_ctx:
            continue
        try:
            ctx = json.loads(raw_ctx)
        except Exception:
            continue
        vol = (ctx or {}).get("vol") or {}
        if not vol.get("available"):
            continue
        n_score_available += 1
        trading_dates.add(r["trade_date"])
        cf = vol.get("counterfactual")
        if not cf:
            continue
        n_cf_computed += 1
        if cf.get("actual_clears_fee_gate") is False:
            n_actual_fails_gate += 1
        if cf.get("cf_clears_fee_gate") is False:
            n_cf_fails_gate += 1

        pnl = r["pnl"]
        outcome = r["outcome"]
        actual_qty = cf.get("actual_qty")
        cf_qty = cf.get("cf_qty")
        if pnl is not None and outcome in ("win", "loss") and actual_qty:
            ratio = (cf_qty / actual_qty) if cf_qty is not None else None
            if ratio is not None and np.isfinite(ratio):
                pnl_actual_sum += float(pnl)
                pnl_cf_sum += float(pnl) * ratio
                n_pnl_pairs += 1

    return {
        "available": True,
        "n_acted_decisions": n_total,
        "n_score_available": n_score_available,
        "n_trading_days_with_scored_annotations": len(trading_dates),
        "n_counterfactual_computed": n_cf_computed,
        "fee_gate": {
            "n_actual_would_fail": n_actual_fails_gate,
            "n_counterfactual_would_fail": n_cf_fails_gate,
            "note": ("A counterfactual failing the gate means vol-scaling would have silently "
                     "killed a trade the bot actually took (PLAN §13.6 criterion 4 / §13.7's "
                     "'re-check the gate AFTER vol scaling' rule)."),
        },
        "pnl_scaling_estimate": {
            "n_pairs": n_pnl_pairs,
            "actual_pnl_sum": round(pnl_actual_sum, 2),
            "counterfactual_pnl_sum_est": round(pnl_cf_sum, 2),
            "method": ("linear rescaling of realised P&L by cf_qty/actual_qty -- NOT fee/slippage "
                       "aware, informational only, never used for the four hard promotion criteria."),
        },
        "stop_out_avoidance": {
            "modeled": False,
            "note": ("Phase A's counterfactual holds stop DISTANCE fixed and only scales size "
                     "(see vol_shadow.day_vol_shadow's docstring), so it cannot change which stops "
                     "would be hit -- 'stop-outs avoided' is not measurable from these annotations. "
                     "Recorded explicitly rather than approximated."),
        },
    }


# --- §13.6 promotion criteria ---------------------------------------------------


def evaluate_promotion(metrics_by_h: dict[int, dict], day_counterfactual: dict | None) -> dict:
    """The four §13.6 promotion criteria, each reported pass / fail /
    insufficient_data with its observed value next to its threshold. Every
    criterion requires >= 30 TRADING DAYS of the relevant live evidence before
    it can be anything but insufficient_data -- this function will say so even
    when the underlying number would technically clear the bar on a handful
    of days."""
    out: dict[str, dict] = {}

    # --- criterion 1: live per-name rank IC >= 0.30 at h=5 over >=30 days ---
    m5 = metrics_by_h.get(5) or {}
    ic5 = m5.get("rank_ic") or {}
    n_days5 = m5.get("n_days") or 0
    if n_days5 < _PROMOTION_MIN_TRADING_DAYS:
        out["criterion_1_live_rank_ic_h5"] = {
            "status": "insufficient_data",
            "n_trading_days": n_days5, "required_trading_days": _PROMOTION_MIN_TRADING_DAYS,
            "threshold": _IC_H5_THRESHOLD, "observed_ic": ic5.get("mean"),
            "recent_fold_backtest_anchor": _RECENT_FOLD_IC_ANCHOR[5],
            "note": "PLAN §13.6 criterion 1 -- needs >=30 live trading days before a verdict is meaningful",
        }
    else:
        val = ic5.get("mean")
        status = "pass" if (val is not None and val >= _IC_H5_THRESHOLD) else "fail"
        out["criterion_1_live_rank_ic_h5"] = {
            "status": status, "n_trading_days": n_days5, "threshold": _IC_H5_THRESHOLD,
            "observed_ic": val, "recent_fold_backtest_anchor": _RECENT_FOLD_IC_ANCHOR[5],
        }

    # --- criterion 2: level calibration slope in [0.8, 1.2] at h=21 ---
    m21 = metrics_by_h.get(21) or {}
    cal21 = m21.get("calibration") or {}
    n_days21 = m21.get("n_days") or 0
    slope = cal21.get("b")
    if n_days21 < _PROMOTION_MIN_TRADING_DAYS or slope is None:
        out["criterion_2_level_calibration_h21"] = {
            "status": "insufficient_data",
            "n_trading_days": n_days21, "band": list(_CALIB_BAND), "observed_slope": slope,
            "note": ("PLAN §13.6 criterion 2 / §13.4 rule 2 -- if the slope drifts outside "
                     "[0.8, 1.2], sizing must revert to equal-weight, never to an uncalibrated number"),
        }
    else:
        lo, hi = _CALIB_BAND
        status = "pass" if lo <= slope <= hi else "fail"
        out["criterion_2_level_calibration_h21"] = {
            "status": status, "n_trading_days": n_days21, "band": list(_CALIB_BAND),
            "observed_slope": slope,
        }

    # --- criteria 3 & 4: from the day-sleeve counterfactual annotations ---
    dc = day_counterfactual or {}
    n_days_cf = dc.get("n_trading_days_with_scored_annotations") or 0
    have_cf = bool(dc.get("available")) and n_days_cf >= _PROMOTION_MIN_TRADING_DAYS

    if not have_cf:
        out["criterion_3_counterfactual_stopouts_reduced"] = {
            "status": "insufficient_data",
            "n_trading_days": n_days_cf, "required_trading_days": _PROMOTION_MIN_TRADING_DAYS,
        }
    else:
        out["criterion_3_counterfactual_stopouts_reduced"] = {
            "status": "not_modeled",
            "n_trading_days": n_days_cf,
            "note": (dc.get("stop_out_avoidance") or {}).get("note"),
        }

    if not have_cf:
        out["criterion_4_counterfactual_clears_fee_gate"] = {
            "status": "insufficient_data",
            "n_trading_days": n_days_cf, "required_trading_days": _PROMOTION_MIN_TRADING_DAYS,
            "n_counterfactual_trades": dc.get("n_counterfactual_computed") or 0,
        }
    else:
        fee = dc.get("fee_gate") or {}
        n_fail = fee.get("n_counterfactual_would_fail") or 0
        status = "pass" if n_fail == 0 else "fail"
        out["criterion_4_counterfactual_clears_fee_gate"] = {
            "status": status, "n_trading_days": n_days_cf,
            "n_counterfactual_trades": dc.get("n_counterfactual_computed") or 0,
            "n_counterfactual_trades_below_gate": n_fail,
        }

    return out


# --- persistence + orchestration -----------------------------------------------

_GRADE_DDL = """
CREATE TABLE IF NOT EXISTS ml_vol_grade (
    run_ts                  TIMESTAMP NOT NULL,
    as_of_start              DATE,
    as_of_end                DATE,
    n_trading_days_h5        INTEGER,
    n_trading_days_h21       INTEGER,
    metrics_json             VARCHAR NOT NULL,
    day_counterfactual_json  VARCHAR,
    promotion_json           VARCHAR NOT NULL,
    created_at                TIMESTAMP NOT NULL,
    PRIMARY KEY (run_ts)
);
"""


def ensure_grade_table(duck) -> None:
    """Idempotent create -- also declared in db/schema.py::init_duckdb (the
    convention app.ml.vol_scores.ensure_table already follows), kept here too
    so a bare/temp DuckStore (tests, the standalone CLI) works without running
    the full schema bootstrap."""
    duck.execute(_GRADE_DDL)


def run_grade(duck, sqlite, *, uni_db: str | Path | None = None,
              min_names_per_day: int = _MIN_NAMES_PER_DAY) -> dict:
    """Full grading pass: both horizons' forecast metrics + the day-sleeve
    counterfactual summary + the §13.6 promotion verdict. Never raises --
    an unreachable universe DB / missing tables degrade to empty metrics
    rather than killing the run (mirrors vol_scores._score_and_persist's
    routed-reader pattern)."""
    uni_reader, uni_con = _open_universe_reader(uni_db)
    try:
        metrics_by_h = {
            h: grade_horizon(duck, h, uni_reader=uni_reader, min_names_per_day=min_names_per_day)
            for h in GRADED_HORIZONS
        }
    finally:
        if uni_con is not None:
            uni_con.close()

    day_cf = summarize_day_counterfactuals(sqlite)
    promotion = evaluate_promotion(metrics_by_h, day_cf)
    return {
        "run_ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metrics": metrics_by_h,
        "day_counterfactual": day_cf,
        "promotion": promotion,
    }


def write_grade(duck, result: dict) -> None:
    """Idempotent upsert into ``ml_vol_grade`` keyed on ``run_ts`` -- so the
    grading record accrues over time (PLAN §13.9 step 4 deliverable 2)."""
    ensure_grade_table(duck)
    m5 = result["metrics"].get(5) or {}
    m21 = result["metrics"].get(21) or {}
    as_of_start = m21.get("as_of_start") or m5.get("as_of_start")
    as_of_end = m21.get("as_of_end") or m5.get("as_of_end")
    duck.execute(
        "INSERT OR REPLACE INTO ml_vol_grade (run_ts, as_of_start, as_of_end, "
        "n_trading_days_h5, n_trading_days_h21, metrics_json, day_counterfactual_json, "
        "promotion_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            pd.Timestamp(result["run_ts"]).to_pydatetime(), as_of_start, as_of_end,
            m5.get("n_days"), m21.get("n_days"),
            json.dumps(result["metrics"], default=str),
            json.dumps(result["day_counterfactual"], default=str),
            json.dumps(result["promotion"], default=str),
            datetime.now(timezone.utc),
        ],
    )


# --- CLI ------------------------------------------------------------------------


def _market_db_path() -> Path:
    return Path(__file__).resolve().parents[4] / "data" / "market.duckdb"


def _app_db_path() -> Path:
    return Path(__file__).resolve().parents[4] / "data" / "app.db"


def _main() -> int:
    ap = argparse.ArgumentParser(description="Vol-forecast grader (PLAN §13.9 step 4)")
    ap.add_argument("--main-db", default=None)
    ap.add_argument("--app-db", default=None)
    ap.add_argument("--uni-db", default=None)
    ap.add_argument("--write", action="store_true", help="persist to ml_vol_grade (default: dry-run/print only)")
    a = ap.parse_args()

    from app.db.duck import DuckStore
    from app.db.sqlite import SqliteStore

    main_path = Path(a.main_db) if a.main_db else _market_db_path()
    app_path = Path(a.app_db) if a.app_db else _app_db_path()
    if not main_path.exists():
        print(f"no DuckDB at {main_path} -- nothing to grade")
        return 0

    duck = DuckStore(main_path)
    sqlite = SqliteStore(app_path) if app_path.exists() else None
    try:
        result = run_grade(duck, sqlite, uni_db=a.uni_db) if sqlite is not None else {
            "run_ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metrics": {h: grade_horizon(duck, h) for h in GRADED_HORIZONS},
            "day_counterfactual": {"available": False, "reason": "no app.db found"},
            "promotion": None,
        }
        if result["promotion"] is None:
            result["promotion"] = evaluate_promotion(result["metrics"], result["day_counterfactual"])

        print(f"=== VOL GRADE ===  run_ts={result['run_ts']}")
        for h in GRADED_HORIZONS:
            m = result["metrics"][h]
            print(f"\n-- horizon {h} -- n_days={m['n_days']} n_obs={m['n_obs']}")
            if m["n_days"]:
                print(f"   rank_ic mean={m['rank_ic']['mean']:.4f} t={m['rank_ic']['t']:.2f} "
                      f"n_days={m['rank_ic']['n_days']} hit_rate={m['rank_ic']['hit_rate']:.3f}")
                print(f"   qlike={m['qlike']:.4f} rmse={m['rmse']:.6f}")
                cal = m["calibration"]
                print(f"   calibration a={cal['a']:.5f} b={cal['b']:.4f} r2={cal['r2']:.3f} n={cal['n']}")
        print("\n-- day-sleeve counterfactual --")
        print(json.dumps(result["day_counterfactual"], indent=2, default=str))
        print("\n-- §13.6 promotion criteria --")
        print(json.dumps(result["promotion"], indent=2, default=str))

        if a.write:
            write_grade(duck, result)
            print(f"\nwrote 1 row to ml_vol_grade in {main_path}")
        else:
            print("\ndry-run: nothing written (pass --write to persist)")
    finally:
        duck.close()
        if sqlite is not None:
            sqlite.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
