"""/api/watchlist — Panel (d): per-symbol command rows + list management.

GET returns one quote row per watchlist symbol straight from the ts_price
cache (warmed on demand via ensure_daily_history, same path the charts use),
decorated with the latest FinBERT-scored headline from news_items — the
watchlist is purely a consumer of data other panels already ingest.

POST validates a new symbol by actually fetching its history before saving,
so a typo fails loudly instead of becoming a permanently empty row.
"""

from __future__ import annotations

import asyncio
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.ingest.prices import ensure_daily_history

router = APIRouter(prefix="/api", tags=["watchlist"])

SPARK_BARS = 30  # daily closes behind each row's mini-sparkline
ASSET_CLASSES = {"equity", "crypto", "metal", "fx", "future"}


class WatchlistQuote(BaseModel):
    symbol: str
    asset_class: str
    display_name: str | None = None
    close: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None  # last close vs prior close
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    ts: str | None = None  # ISO timestamp of the latest daily bar
    spark: list[float] = []
    sent_score: float | None = None
    sent_label: str | None = None
    sent_title: str | None = None
    sent_url: str | None = None
    sent_published: str | None = None


class WatchlistResponse(BaseModel):
    quotes: list[WatchlistQuote]


class WatchlistItemIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    asset_class: str = "equity"
    display_name: str | None = Field(default=None, max_length=80)


def _rows(sqlite) -> list[tuple[str, str, str | None]]:
    return [
        (r["symbol"], r["asset_class"], r["display_name"])
        for r in sqlite.fetchall(
            "SELECT symbol, asset_class, display_name FROM watchlist ORDER BY sort_order"
        )
    ]


def _quote(duck, symbol: str, asset_class: str, display_name: str | None) -> WatchlistQuote:
    """Blocking: latest bars + latest scored headline for one symbol."""
    bars = duck.fetchall(
        "SELECT ts, open, high, low, close, volume FROM ts_price "
        "WHERE source = 'yahoo' AND symbol = ? AND close IS NOT NULL "
        "ORDER BY ts DESC LIMIT ?",
        [symbol, SPARK_BARS],
    )
    q = WatchlistQuote(symbol=symbol, asset_class=asset_class, display_name=display_name)
    if bars:
        ts, q.open, q.high, q.low, q.close, q.volume = bars[0]
        q.ts = ts.isoformat()
        if len(bars) > 1:
            q.prev_close = bars[1][4]
            if q.prev_close:
                q.change_pct = (q.close - q.prev_close) / q.prev_close * 100
        q.spark = [b[4] for b in reversed(bars)]

    sent = duck.fetchone(
        "SELECT score, label, title, url, published FROM news_items "
        "WHERE symbol = ? AND score IS NOT NULL ORDER BY published DESC LIMIT 1",
        [symbol],
    )
    if sent:
        q.sent_score, q.sent_label, q.sent_title, q.sent_url = sent[0], sent[1], sent[2], sent[3]
        q.sent_published = sent[4].isoformat()
    return q


@router.get("/watchlist", response_model=WatchlistResponse)
async def watchlist(request: Request) -> WatchlistResponse:
    duck = request.app.state.duck
    sqlite = request.app.state.sqlite
    http = request.app.state.http
    loop = asyncio.get_running_loop()

    rows = await loop.run_in_executor(None, _rows, sqlite)
    # Cache-first: refetches only when a symbol is missing its latest expected
    # close, so polling this endpoint stays within yfinance politeness.
    for symbol, asset_class, _ in rows:
        await ensure_daily_history(http, duck, symbol, asset_class)

    def _q() -> list[WatchlistQuote]:
        return [_quote(duck, s, a, n) for s, a, n in rows]

    return WatchlistResponse(quotes=await loop.run_in_executor(None, _q))


@router.post("/watchlist", response_model=WatchlistQuote)
async def add_symbol(request: Request, item: WatchlistItemIn) -> WatchlistQuote:
    duck = request.app.state.duck
    sqlite = request.app.state.sqlite
    http = request.app.state.http
    loop = asyncio.get_running_loop()

    symbol = item.symbol.strip().upper()
    if item.asset_class not in ASSET_CLASSES:
        raise HTTPException(status_code=422, detail=f"asset_class must be one of {sorted(ASSET_CLASSES)}")
    exists = await loop.run_in_executor(
        None, sqlite.fetchone, "SELECT symbol FROM watchlist WHERE symbol = ?", [symbol]
    )
    if exists:
        raise HTTPException(status_code=409, detail=f"{symbol} is already on the watchlist")

    # Validate by fetching: an unknown ticker yields no bars and is rejected.
    await ensure_daily_history(http, duck, symbol, item.asset_class)
    quote = await loop.run_in_executor(None, _quote, duck, symbol, item.asset_class, item.display_name)
    if quote.close is None:
        raise HTTPException(status_code=422, detail=f"no price history found for {symbol}")

    def _insert():
        nxt = sqlite.fetchone("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM watchlist")[0]
        sqlite.execute(
            "INSERT INTO watchlist (symbol, asset_class, display_name, sort_order) VALUES (?, ?, ?, ?)",
            [symbol, item.asset_class, item.display_name, nxt],
        )

    await loop.run_in_executor(None, _insert)
    return quote


@router.delete("/watchlist")
async def remove_symbol(request: Request, symbol: str = Query()) -> dict:
    # Query param, not path param: symbols like BTC/USD contain a slash.
    sqlite = request.app.state.sqlite
    loop = asyncio.get_running_loop()
    row = await loop.run_in_executor(
        None, sqlite.fetchone, "SELECT symbol FROM watchlist WHERE symbol = ?", [symbol]
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"{symbol} is not on the watchlist")
    await loop.run_in_executor(
        None, lambda: sqlite.execute("DELETE FROM watchlist WHERE symbol = ?", [symbol])
    )
    return {"deleted": symbol}
