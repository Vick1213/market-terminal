"""ML-signal REST surface (PLAN §13.9 step 2). First route: the persisted
per-name volatility cross-section (``app.ml.vol_scores``). No ml router
existed before this — created following ``routers/edge.py``'s fail-soft
convention for ``app.ml.*``-backed routes (``/api/vol-overlay``, edge.py:87),
since a trimmed desktop build may not ship ``data/market.duckdb`` fully
populated with ``ml_vol_scores`` yet (the writer job is default-OFF).

This route only READS the already-persisted table — it never triggers a
(slow, ~147-symbol) live scoring pass on request.
"""

from __future__ import annotations

import asyncio

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
