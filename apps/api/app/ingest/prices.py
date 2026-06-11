"""On-demand daily price history (Phase 3 slice, pulled forward for charts).

Source pecking order, re-verified 2026-06-11 (PLAN §10 #2):
  * Stooq CSV (the plan's primary) now serves a JavaScript browser-verification
    challenge to headless clients — unusable here.
  * Yahoo's v8 chart endpoint 429s plain httpx/curl by TLS fingerprint, but
    yfinance ships curl_cffi browser impersonation and still returns free
    history — so yfinance IS the daily-bar fetcher, used gently: cache-first
    in ts_price, refetched only when the stored history is missing the most
    recent expected close (≤ watchlist-size calls per day).
  * Alpaca (when MARKET_ALPACA_* keys are set) is the redundancy leg: primary
    for live quotes (one batched IEX snapshot call vs one Yahoo hit per
    symbol) and fallback for daily bars when yfinance comes up empty.

Note on ts_price.source: every chart/cookbook/strategist reader filters on
``source = 'yahoo'`` — that value is the *daily-bars series namespace* (the
way multiasset uses 'gold-api' as a separate namespace), not provenance, so
the Alpaca fallback deliberately writes into it to keep the series whole.

yfinance is blocking; its calls run in the default executor.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone

from app.config import get_settings
from app.db.duck import DuckStore
from app.ingest import alpaca

log = logging.getLogger("market.ingest.prices")

_INSERT_SQL = (
    "INSERT OR REPLACE INTO ts_price "
    "(source, symbol, asset_class, ts, open, high, low, close, volume) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def yahoo_symbol(symbol: str, asset_class: str) -> str:
    """Watchlist symbol -> Yahoo ticker."""
    if asset_class == "crypto":
        if "/" in symbol:  # BTC/USD -> BTC-USD
            return symbol.replace("/", "-")
        if "-" not in symbol:  # bare BTC -> BTC-USD (avoid the NYSE 'BTC' stock)
            return f"{symbol.upper()}-USD"
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


def _stored_latest(duck: DuckStore, symbol: str) -> date | None:
    row = duck.fetchone(
        "SELECT max(ts) FROM ts_price WHERE source = 'yahoo' AND symbol = ?", [symbol]
    )
    return row[0].date() if row and row[0] else None


def _fetch_and_store(duck: DuckStore, symbol: str, asset_class: str) -> None:
    """Blocking: pull daily bars via yfinance and upsert into ts_price."""
    import yfinance as yf  # heavy import kept off module load

    latest = _stored_latest(duck, symbol)
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
    duck.executemany(_INSERT_SQL, rows)
    log.info("yfinance %s: stored %s daily bars", symbol, len(rows))


# Live quotes via fast_info, behind a per-symbol TTL so client poll rate
# never translates 1:1 into Yahoo hits regardless of how many panels poll.
LIVE_TTL_SECONDS = 10.0
_live_cache: dict[str, tuple[float, dict]] = {}
_live_lock = threading.Lock()


def fetch_live_quote(symbol: str, asset_class: str) -> dict | None:
    """Blocking: near-real-time quote for one symbol (delayed for equities)."""
    import yfinance as yf  # heavy import kept off module load

    now = time.monotonic()
    with _live_lock:
        hit = _live_cache.get(symbol)
        if hit and now - hit[0] < LIVE_TTL_SECONDS:
            return hit[1]

    ysym = yahoo_symbol(symbol, asset_class)
    try:
        fi = yf.Ticker(ysym).fast_info
        price = fi.last_price
    except Exception as exc:
        log.warning("yfinance live %s (%s) failed: %s", symbol, ysym, exc)
        return None
    if price is None or price != price:  # NaN guard
        return None

    def _num(name: str) -> float | None:
        try:
            v = getattr(fi, name)
            return float(v) if v is not None and v == v else None
        except Exception:
            return None

    quote = {
        "symbol": symbol,
        "price": float(price),
        "prev_close": _num("previous_close"),
        "day_high": _num("day_high"),
        "day_low": _num("day_low"),
        "volume": _num("last_volume"),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with _live_lock:
        _live_cache[symbol] = (now, quote)
    return quote


async def fetch_live_quotes(http, items: list[tuple[str, str]]) -> list[dict]:
    """Near-real-time quotes for [(symbol, asset_class)], in input order.

    Alpaca first when keys are configured — the whole list costs one stock
    snapshot call plus one crypto call. Anything Alpaca can't serve (no keys,
    metals spot, an outage) falls back to per-symbol yfinance fast_info.
    Both paths share the per-symbol TTL cache.
    """
    now = time.monotonic()
    out: dict[str, dict] = {}
    pending: list[tuple[str, str]] = []
    with _live_lock:
        for symbol, asset_class in items:
            hit = _live_cache.get(symbol)
            if hit and now - hit[0] < LIVE_TTL_SECONDS:
                out[symbol] = hit[1]
            else:
                pending.append((symbol, asset_class))

    settings = get_settings()
    if pending and settings.alpaca_key_id and settings.alpaca_secret_key:
        try:
            got = await alpaca.fetch_snapshots(
                http, settings.alpaca_key_id, settings.alpaca_secret_key, pending
            )
        except Exception as exc:
            log.warning("alpaca snapshots failed (yfinance fallback): %s", exc)
            got = {}
        if got:
            with _live_lock:
                for symbol, quote in got.items():
                    _live_cache[symbol] = (now, quote)
            out.update(got)
            pending = [(s, ac) for s, ac in pending if s not in got]

    if pending:
        loop = asyncio.get_running_loop()

        def _rest() -> list[dict]:
            quotes = []
            for symbol, asset_class in pending:
                q = fetch_live_quote(symbol, asset_class)
                if q is not None:
                    quotes.append(q)
            return quotes

        for q in await loop.run_in_executor(None, _rest):
            out[q["symbol"]] = q

    return [out[s] for s, _ in items if s in out]


async def ensure_daily_history(
    http, duck: DuckStore, symbol: str, asset_class: str
) -> None:
    """Fetch + store daily bars for `symbol` unless already fresh.

    yfinance does the work; if it leaves the series stale (Yahoo broken or
    thin) and Alpaca keys are configured, Alpaca backfills the gap into the
    same 'yahoo' namespace (see module docstring).
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _fetch_and_store, duck, symbol, asset_class)

    settings = get_settings()
    if not (settings.alpaca_key_id and settings.alpaca_secret_key):
        return
    latest = await loop.run_in_executor(None, _stored_latest, duck, symbol)
    if latest is not None and latest >= _last_expected_close():
        return
    start = (
        latest - timedelta(days=7) if latest else date.today() - timedelta(days=5 * 365)
    )
    try:
        bars = await alpaca.fetch_daily_bars(
            http, settings.alpaca_key_id, settings.alpaca_secret_key,
            symbol, asset_class, start,
        )
    except Exception as exc:
        log.warning("alpaca bars %s failed: %s", symbol, exc)
        return
    if not bars:
        return
    rows = [("yahoo", symbol, asset_class, *bar) for bar in bars]
    await loop.run_in_executor(None, duck.executemany, _INSERT_SQL, rows)
    log.info("alpaca %s: backfilled %s daily bars", symbol, len(rows))
