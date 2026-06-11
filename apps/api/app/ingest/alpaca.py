"""Alpaca market data (PLAN §10 #2) — the second leg under equity prices.

Free-tier facts (re-verified 2026-06-11):
  * Keys are free, no funded account (app.alpaca.markets).
  * Stocks: IEX feed live, 200 req/min; SIP historical allowed once data is
    >15 min old — fine for daily bars, so history uses ``feed=sip`` with a
    backed-off ``end`` and snapshots use ``feed=iex``.
  * Crypto (v1beta3, ``us`` locale): no feed/delay restrictions.

Everything goes through the shared ``HttpClient`` so Alpaca gets the same
rate limiting, retries, and source-health watchdog as every other source.

Coverage is deliberately partial: US stocks/ETFs and crypto pairs only.
``alpaca_symbol`` returns ``None`` for COMEX spot proxies (XAU/XAG), fx and
futures — callers fall back to yfinance for those, which keeps the "never a
single point of failure" promise where Alpaca can honor it without
pretending to cover what it can't.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("market.ingest.alpaca")

_STOCKS_BASE = "https://data.alpaca.markets/v2/stocks"
_CRYPTO_BASE = "https://data.alpaca.markets/v1beta3/crypto/us"


def alpaca_symbol(symbol: str, asset_class: str) -> tuple[str, str] | None:
    """Watchlist symbol -> (alpaca symbol, "stock" | "crypto"), or None
    when Alpaca can't serve this asset (metals spot, fx, futures)."""
    if asset_class == "crypto":
        s = symbol.upper()
        if "/" not in s:  # BTC-USD or bare BTC -> BTC/USD
            s = s.replace("-", "/") if "-" in s else f"{s}/USD"
        return (s, "crypto")
    if asset_class == "equity":
        return (symbol.upper(), "stock")
    if asset_class == "metal" and symbol.upper() not in ("XAU", "XAG"):
        return (symbol.upper(), "stock")  # GLD/SLV etc. are ordinary ETFs
    return None


def _headers(key_id: str, secret: str) -> dict[str, str]:
    return {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}


def _split(items: list[tuple[str, str]]) -> tuple[dict[str, str], dict[str, str]]:
    """[(watchlist symbol, asset_class)] -> ({alpaca: watchlist}, same for crypto)."""
    stocks: dict[str, str] = {}
    crypto: dict[str, str] = {}
    for symbol, asset_class in items:
        mapped = alpaca_symbol(symbol, asset_class)
        if mapped is None:
            continue
        asym, kind = mapped
        (stocks if kind == "stock" else crypto)[asym] = symbol
    return stocks, crypto


def _snapshot_quote(symbol: str, snap: dict) -> dict | None:
    """One snapshot payload -> the fetch_live_quote dict shape."""
    trade = snap.get("latestTrade") or {}
    price = trade.get("p")
    if price is None:
        return None
    daily = snap.get("dailyBar") or {}
    prev = snap.get("prevDailyBar") or {}
    return {
        "symbol": symbol,
        "price": float(price),
        "prev_close": float(prev["c"]) if prev.get("c") is not None else None,
        "day_high": float(daily["h"]) if daily.get("h") is not None else None,
        "day_low": float(daily["l"]) if daily.get("l") is not None else None,
        "volume": float(daily["v"]) if daily.get("v") is not None else None,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


async def fetch_snapshots(
    http, key_id: str, secret: str, items: list[tuple[str, str]]
) -> dict[str, dict]:
    """Live-ish quotes for every Alpaca-coverable symbol, batched: one request
    for all stocks + one for all crypto, regardless of watchlist size.

    Returns {watchlist symbol: quote dict}; symbols Alpaca can't serve are
    simply absent (the caller's yfinance leg picks them up).
    """
    stocks, crypto = _split(items)
    headers = _headers(key_id, secret)
    out: dict[str, dict] = {}

    if stocks:
        data = await http.get_json(
            f"{_STOCKS_BASE}/snapshots",
            params={"symbols": ",".join(sorted(stocks)), "feed": "iex"},
            headers=headers,
            conditional=False,
        )
        for asym, snap in data.items():
            if not isinstance(snap, dict):
                continue
            q = _snapshot_quote(stocks.get(asym, asym), snap)
            if q:
                out[q["symbol"]] = q

    if crypto:
        data = await http.get_json(
            f"{_CRYPTO_BASE}/snapshots",
            params={"symbols": ",".join(sorted(crypto))},
            headers=headers,
            conditional=False,
        )
        for asym, snap in (data.get("snapshots") or {}).items():
            q = _snapshot_quote(crypto.get(asym, asym), snap)
            if q:
                out[q["symbol"]] = q

    return out


async def fetch_daily_bars(
    http, key_id: str, secret: str, symbol: str, asset_class: str, start: date
) -> list[tuple[datetime, float, float, float, float, float | None]]:
    """Daily (ts, open, high, low, close, volume) bars from `start`, paginated.

    Stock closes are split/dividend-adjusted (``adjustment=all``) to match the
    auto-adjusted series yfinance has been writing into ts_price.
    """
    mapped = alpaca_symbol(symbol, asset_class)
    if mapped is None:
        return []
    asym, kind = mapped
    headers = _headers(key_id, secret)

    if kind == "stock":
        url = f"{_STOCKS_BASE}/bars"
        params: dict = {
            "symbols": asym,
            "timeframe": "1Day",
            "start": start.isoformat(),
            # free tier may not query SIP data newer than 15 min
            "end": (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat(),
            "adjustment": "all",
            "feed": "sip",
            "limit": 10000,
        }
    else:
        url = f"{_CRYPTO_BASE}/bars"
        params = {
            "symbols": asym,
            "timeframe": "1Day",
            "start": start.isoformat(),
            "limit": 10000,
        }

    rows: list[tuple[datetime, float, float, float, float, float | None]] = []
    while True:
        data = await http.get_json(url, params=params, headers=headers, conditional=False)
        for bar in (data.get("bars") or {}).get(asym, []):
            close = bar.get("c")
            if close is None:
                continue
            ts = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
            rows.append(
                (
                    ts.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0),
                    float(bar.get("o", close)),
                    float(bar.get("h", close)),
                    float(bar.get("l", close)),
                    float(close),
                    float(bar["v"]) if bar.get("v") is not None else None,
                )
            )
        token = data.get("next_page_token")
        if not token:
            return rows
        params["page_token"] = token
