"""ML-signal REST surface (PLAN §13.9 step 2). First route: the persisted
per-name volatility cross-section (``app.ml.vol_scores``). No ml router
existed before this — created following ``routers/edge.py``'s fail-soft
convention for ``app.ml.*``-backed routes (``/api/vol-overlay``, edge.py:87),
since a trimmed desktop build may not ship ``data/market.duckdb`` fully
populated with ``ml_vol_scores`` yet (the writer job is default-OFF).

This route only READS the already-persisted table — it never triggers a
(slow, ~147-symbol) live scoring pass on request.

Second route (PLAN §13.9 step 4): the vol forecast grader (``app.ml.vol_grade``)
— same read-only convention. Grading itself is a separate, slower pass over
``ml_vol_scores`` + price history + the day journal, run via
``python -m app.ml.vol_grade --write`` (or a future scheduler job), not
triggered by this request.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/api/ml", tags=["ml"])

_COLUMNS = [
    "ts", "symbol", "horizon", "estimator", "pred_vol", "level_admissible",
    "calib_a", "calib_b", "rank", "pctile", "in_reference_panel", "n_obs", "created_at",
]


def _row_to_dict(r: tuple) -> dict:
    d = dict(zip(_COLUMNS, r))
    d["ts"] = str(d["ts"])
    d["created_at"] = str(d["created_at"])
    for k in ("pred_vol", "calib_a", "calib_b", "pctile"):
        if d[k] is not None:
            d[k] = float(d[k])
    for k in ("horizon", "rank", "n_obs"):
        if d[k] is not None:
            d[k] = int(d[k])
    d["level_admissible"] = bool(d["level_admissible"])
    d["in_reference_panel"] = bool(d["in_reference_panel"])
    return d


def _fetch_latest(duck, symbol: str | None, horizon: int | None) -> list[tuple]:
    where = []
    params: list = []
    if symbol:
        where.append("symbol = ?")
        params.append(symbol.upper())
    if horizon is not None:
        where.append("horizon = ?")
        params.append(horizon)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    sql = f"""
        SELECT ts, symbol, horizon, estimator, pred_vol, level_admissible,
               calib_a, calib_b, rank, pctile, in_reference_panel, n_obs, created_at
        FROM ml_vol_scores
        {where_sql}
        QUALIFY row_number() OVER (PARTITION BY symbol, horizon ORDER BY ts DESC) = 1
        ORDER BY horizon, pctile DESC NULLS LAST, symbol
    """
    return duck.fetchall(sql, params)


@router.get("/vol-scores")
async def vol_scores(
    request: Request,
    symbol: str | None = Query(None),
    horizon: int | None = Query(None),
) -> dict:
    """Latest per-name vol cross-section (PLAN §13.5). Fail-soft: an empty or
    not-yet-created ``ml_vol_scores`` table (the writer job is default-OFF
    until wired) returns a valid empty-shaped 200, never a 500."""
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()
    try:
        rows = await loop.run_in_executor(None, _fetch_latest, duck, symbol, horizon)
        return {"scores": [_row_to_dict(r) for r in rows], "count": len(rows)}
    except Exception as exc:  # table missing (job never run), or a bad filter
        return {"scores": [], "count": 0, "note": f"vol-scores unavailable: {exc}"}


def _fetch_latest_grade(duck) -> tuple | None:
    return duck.fetchone(
        "SELECT run_ts, as_of_start, as_of_end, n_trading_days_h5, n_trading_days_h21, "
        "metrics_json, day_counterfactual_json, promotion_json, created_at "
        "FROM ml_vol_grade ORDER BY run_ts DESC LIMIT 1"
    )


@router.get("/vol-grade")
async def vol_grade(request: Request) -> dict:
    """Latest persisted vol-forecast grading run (PLAN §13.6/§13.9 step 4):
    per-horizon rank IC / QLIKE / RMSE / calibration, the day-sleeve
    counterfactual summary, and the four §13.6 promotion criteria as
    pass/fail/insufficient_data. Fail-soft: a not-yet-created or empty
    ``ml_vol_grade`` table (grading has never been run) returns a valid
    200 with ``available: false``, never a 500 — mirrors ``/vol-scores``."""
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()
    try:
        row = await loop.run_in_executor(None, _fetch_latest_grade, duck)
    except Exception as exc:  # table missing (grader never run)
        return {"available": False, "note": f"vol-grade unavailable: {exc}"}
    if row is None:
        return {"available": False, "note": "no grading run has been persisted yet -- "
                                             "run `python -m app.ml.vol_grade --write`"}
    (run_ts, as_of_start, as_of_end, n_days_h5, n_days_h21,
     metrics_json, day_cf_json, promotion_json, created_at) = row
    return {
        "available": True,
        "run_ts": str(run_ts),
        "as_of_start": str(as_of_start) if as_of_start is not None else None,
        "as_of_end": str(as_of_end) if as_of_end is not None else None,
        "n_trading_days_h5": n_days_h5,
        "n_trading_days_h21": n_days_h21,
        "metrics": json.loads(metrics_json) if metrics_json else None,
        "day_counterfactual": json.loads(day_cf_json) if day_cf_json else None,
        "promotion": json.loads(promotion_json) if promotion_json else None,
        "created_at": str(created_at),
    }
