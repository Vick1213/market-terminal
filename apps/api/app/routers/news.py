"""GET /api/news — the deduped, FinBERT-scored timeline backing Panel (a).

Live updates arrive over WS topic "news"; this endpoint serves the initial
backfill and per-ticker filtered queries from DuckDB.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api", tags=["news"])


class NewsItemOut(BaseModel):
    id: str
    source: str
    symbol: str | None
    title: str
    summary: str | None
    url: str | None
    published: str
    score: float | None
    confidence: float | None
    label: str | None
    outlets: int = 1
    outlet_names: str | None = None


class NewsResponse(BaseModel):
    items: list[NewsItemOut]
    symbols: list[str]  # distinct symbols present, for the filter chips


@router.get("/news", response_model=NewsResponse)
async def news(
    request: Request,
    symbol: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
) -> NewsResponse:
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    where, params = "", []
    if symbol:
        where = "WHERE symbol = ?"
        params.append(symbol.upper())

    def _q():
        rows = duck.fetchall(
            f"SELECT id, source, symbol, title, summary, url, published, "
            f"score, confidence, label, outlets, outlet_names FROM news_items {where} "
            f"ORDER BY published DESC LIMIT ?",
            [*params, limit],
        )
        syms = duck.fetchall(
            "SELECT DISTINCT symbol FROM news_items WHERE symbol IS NOT NULL ORDER BY symbol"
        )
        return rows, [s[0] for s in syms]

    rows, symbols = await loop.run_in_executor(None, _q)
    return NewsResponse(
        items=[
            NewsItemOut(
                id=r[0], source=r[1], symbol=r[2], title=r[3], summary=r[4], url=r[5],
                published=r[6].isoformat() if r[6] else "",
                score=r[7], confidence=r[8], label=r[9],
                outlets=r[10] or 1, outlet_names=r[11],
            )
            for r in rows
        ],
        symbols=symbols,
    )
