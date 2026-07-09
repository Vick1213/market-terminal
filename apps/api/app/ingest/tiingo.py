"""Tiingo daily EOD bars — the BYO-key-safe yfinance replacement (M2.5).

Tiingo's own ToS explicitly permits the BYO-key desktop pattern, in writing
(docs/data-licensing-audit.md, "key finding"): *"If you are a developer and
are building software for your audience that requires users to submit their
own Tiingo API token in order to use your software, and your software is not
distributing our data, you do not need to contact us regarding licensing."*
That is the one vendor in the whole audit that says so outright — the reason
it's the yfinance replacement rather than another premium vendor (Polygon
Individual and EODHD Non-Professional both explicitly forbid third-party
BYO-key apps even with a customer's own key).

Coverage is intentionally partial, same "never pretend to cover what you
can't" contract as ``app/ingest/alpaca.py``. The free
``/tiingo/daily/<ticker>/prices`` endpoint only covers US-listed equities and
ETFs:
  * crypto (BTC/USD etc.) needs Tiingo's separate crypto product — not wired
    here; Alpaca already covers crypto daily bars.
  * COMEX metals proxies (GC=F/SI=F, our XAU/XAG mapping in
    ``app/ingest/prices.py::yahoo_symbol``) are futures tickers, not
    equities — not on this endpoint.
  * Index tickers (``^MOVE``, ``^VIX``, ...) aren't equities either.
``tiingo_symbol()`` returns ``None`` for all of the above; callers MUST treat
that as "this series can't come from Tiingo" and degrade gracefully (fall
back to another provider, or simply leave the series stale/missing) rather
than raising or crashing.

Stored values use Tiingo's split/dividend-*adjusted* OHLCV columns to match
the auto-adjusted convention yfinance has been writing into ``ts_price``
(see ``app/ingest/prices.py``'s module docstring on the ``'yahoo'``
namespace, and ``app/ingest/alpaca.py``'s ``adjustment=all``).
"""

from __future__ import annotations

import logging
from datetime import date, datetime

log = logging.getLogger("market.ingest.tiingo")

TIINGO_HOST = "api.tiingo.com"
_DAILY_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"


def tiingo_symbol(symbol: str, asset_class: str) -> str | None:
    """Watchlist symbol -> Tiingo ticker, or None when the free daily-equity
    endpoint can't serve this asset (see module docstring)."""
    if asset_class == "crypto":
        return None
    if symbol.upper() in ("XAU", "XAG"):
        return None
    if symbol.startswith("^"):
        return None
    return symbol.upper()


def _parse_rows(payload: list, symbol: str, asset_class: str) -> list[tuple]:
    """Tiingo JSON rows -> (source, symbol, asset_class, ts, open, high, low,
    close, volume) tuples — the exact shape of prices.py's ``_INSERT_SQL``.
    Prefers the adjusted OHLCV columns; falls back to the raw ones if Tiingo
    ever omits an adjustment field (seen for some very illiquid tickers)."""
    rows: list[tuple] = []
    for r in payload:
        if not isinstance(r, dict):
            continue
        ds = r.get("date")
        close = r.get("adjClose", r.get("close"))
        if not ds or close is None:
            continue
        try:
            ts = datetime.fromisoformat(str(ds).replace("Z", "+00:00")).replace(
                tzinfo=None, hour=0, minute=0, second=0, microsecond=0
            )
        except ValueError:
            continue
        open_ = r.get("adjOpen", r.get("open", close))
        high = r.get("adjHigh", r.get("high", close))
        low = r.get("adjLow", r.get("low", close))
        volume = r.get("adjVolume", r.get("volume"))
        try:
            rows.append(
                (
                    "yahoo",  # daily-bars series NAMESPACE, not provenance — see prices.py
                    symbol, asset_class, ts,
                    float(open_), float(high), float(low), float(close),
                    float(volume) if volume is not None else None,
                )
            )
        except (TypeError, ValueError):
            continue
    return rows


async def fetch_daily_bars(
    http, api_key: str, symbol: str, asset_class: str, start: date
) -> list[tuple]:
    """Daily bars for one symbol from `start`, already shaped for
    ``prices.py``'s ``_INSERT_SQL``. Returns ``[]`` (never raises) when: no
    key is configured, Tiingo structurally can't serve this symbol (see
    ``tiingo_symbol``), or the request itself fails — callers must treat an
    empty result as "try the next provider" or "accept a stale/missing
    series," never as a crash.
    """
    if not api_key:
        return []
    tsym = tiingo_symbol(symbol, asset_class)
    if tsym is None:
        log.debug(
            "tiingo: %s (%s) not servable by the daily-equity endpoint "
            "(crypto/metals-future/index) — degrading gracefully",
            symbol, asset_class,
        )
        return []
    try:
        data = await http.get_json(
            _DAILY_URL.format(ticker=tsym),
            params={"startDate": start.isoformat(), "token": api_key, "format": "json"},
            conditional=False,  # key rides in params — keep it out of the disk cache
        )
    except Exception as exc:
        log.warning("tiingo bars %s (%s) failed: %s", symbol, tsym, exc)
        return []
    if not isinstance(data, list):
        log.warning("tiingo bars %s: unexpected payload %r", symbol, type(data))
        return []
    return _parse_rows(data, symbol, asset_class)
