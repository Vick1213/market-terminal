"""On-demand daily price history (Phase 3 slice, pulled forward for charts).

Source pecking order, re-verified 2026-06-09:
  * Stooq CSV (the plan's primary) now serves a JavaScript browser-verification
    challenge to headless clients — unusable here.
  * Yahoo's v8 chart endpoint 429s plain httpx/curl by TLS fingerprint, but
    yfinance ships curl_cffi browser impersonation and still returns free
    history — so yfinance IS the fetcher, used gently: cache-first in
    ts_price, refetched only when the stored history is missing the most
    recent expected close (≤ watchlist-size calls per day).

yfinance is blocking; everything here runs in the default executor.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from app.db.duck import DuckStore

log = logging.getLogger("market.ingest.prices")


def yahoo_symbol(symbol: str, asset_class: str) -> str:
    """Watchlist symbol -> Yahoo ticker."""
    if asset_class == "crypto" and "/" in symbol:  # BTC/USD -> BTC-USD
        return symbol.replace("/", "-")
    if symbol.upper() == "XAU":  # metal spot -> COMEX futures (panel-e form)
        return "GC=F"
    if symbol.upper() == "XAG":
        return "SI=F"
    return symbol


def _last_expected_close() -> date:
    """Most recent weekday that should have a close by now (UTC, generous)."""
    day = date.today() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _fetch_and_store(duck: DuckStore, symbol: str, asset_class: str) -> None:
    """Blocking: pull daily bars via yfinance and upsert into ts_price."""
    import yfinance as yf  # heavy import kept off module load

    row = duck.fetchone(
        "SELECT max(ts) FROM ts_price WHERE source = 'yahoo' AND symbol = ?", [symbol]
    )
    latest = row[0].date() if row and row[0] else None
    if latest is not None and latest >= _last_expected_close():
        return

    ysym = yahoo_symbol(symbol, asset_class)
    try:
        if latest is None:
            hist = yf.Ticker(ysym).history(period="5y", interval="1d")
        else:
            hist = yf.Ticker(ysym).history(
                start=(latest - timedelta(days=7)).isoformat(), interval="1d"
            )
    except Exception as exc:
        log.warning("yfinance %s (%s) failed: %s", symbol, ysym, exc)
        return
    if hist is None or hist.empty:
        log.warning("yfinance %s (%s): no rows", symbol, ysym)
        return

    rows = []
    for ts, r in hist.iterrows():
        close = r.get("Close")
        if close is None or close != close:  # NaN guard
            continue
        rows.append(
            (
                "yahoo", symbol, asset_class,
                ts.to_pydatetime().replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0),
                float(r.get("Open", close)), float(r.get("High", close)),
                float(r.get("Low", close)), float(close),
                float(r["Volume"]) if "Volume" in r and r["Volume"] == r["Volume"] else None,
            )
        )
    if not rows:
        return
    duck.executemany(
        "INSERT OR REPLACE INTO ts_price "
        "(source, symbol, asset_class, ts, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    log.info("yfinance %s: stored %s daily bars", symbol, len(rows))


async def ensure_daily_history(
    http, duck: DuckStore, symbol: str, asset_class: str
) -> None:
    """Fetch + store daily bars for `symbol` unless already fresh.

    `http` is unused (yfinance manages its own impersonated session) but kept
    in the signature so callers don't change when a source swap happens again.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _fetch_and_store, duck, symbol, asset_class)
