"""GET /api/news — the deduped, FinBERT-scored timeline backing Panel (a).

Live updates arrive over WS topic "news"; this endpoint serves the initial
backfill and per-ticker filtered queries from DuckDB.
"""

from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api", tags=["news"])

# Same shape the ingest layer accepts (skips BTC/USD, _SPX, spot codes ...).
_TICKER_RE = re.compile(r"^[A-Z]{1,6}(\.[A-Z]{1,2})?$")


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
    custom: list[str] = []  # user-added news-only tickers (removable chips)


class NewsTickerIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=10)


@router.get("/news", response_model=NewsResponse)
async def news(
    request: Request,
    symbol: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
) -> NewsResponse:
    duck = request.app.state.duck
    sqlite = request.app.state.sqlite
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
        custom = sqlite.fetchall("SELECT symbol FROM news_tickers ORDER BY symbol")
        return rows, [s[0] for s in syms], [c["symbol"] for c in custom]

    rows, symbols, custom = await loop.run_in_executor(None, _q)
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
        custom=custom,
    )


@router.post("/news/tickers")
async def add_news_ticker(request: Request, item: NewsTickerIn) -> dict:
    """Track an extra ticker in the news ingestors (without touching the
    watchlist) and pull its per-symbol sources once, immediately."""
    sqlite = request.app.state.sqlite
    pipeline = request.app.state.news_pipeline
    loop = asyncio.get_running_loop()

    symbol = item.symbol.strip().upper()
    if not _TICKER_RE.match(symbol):
        raise HTTPException(status_code=422, detail=f"{symbol!r} does not look like an exchange ticker")
    exists = await loop.run_in_executor(
        None, sqlite.fetchone, "SELECT 1 FROM news_tickers WHERE symbol = ?", [symbol]
    )
    if exists:
        raise HTTPException(status_code=409, detail=f"{symbol} is already tracked")
    await loop.run_in_executor(
        None, sqlite.execute,
        "INSERT OR IGNORE INTO news_tickers (symbol) VALUES (?)", [symbol],
    )
    new_items = await pipeline.run_symbol(symbol)
    return {"symbol": symbol, "new_items": new_items}


@router.delete("/news/tickers")
async def remove_news_ticker(request: Request, symbol: str = Query()) -> dict:
    sqlite = request.app.state.sqlite
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    sym = symbol.strip().upper()
    row = await loop.run_in_executor(
        None, sqlite.fetchone, "SELECT 1 FROM news_tickers WHERE symbol = ?", [sym]
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"{sym} is not a tracked news ticker")

    def _rm():
        sqlite.execute("DELETE FROM news_tickers WHERE symbol = ?", [sym])
        # Drop its stored items too (unless the watchlist also covers it),
        # otherwise the filter chip would survive the removal.
        on_watchlist = sqlite.fetchone(
            "SELECT 1 FROM watchlist WHERE upper(symbol) = ?", [sym]
        )
        if not on_watchlist:
            duck.execute("DELETE FROM news_items WHERE symbol = ?", [sym])

    await loop.run_in_executor(None, _rm)
    return {"deleted": sym}
