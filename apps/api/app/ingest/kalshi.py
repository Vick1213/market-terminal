"""Phase 16 §11 rank 8 — Kalshi FOMC + CPI prediction markets (keyless REST).

Kalshi publishes a full per-strike probability distribution for the next FOMC
rate decision (series KXFED, an "Above X%" threshold ladder) and CPI prints
(KXCPI) — not just a binary next-cut odds. We snapshot the nearest open event
per tracked series into kalshi_markets (replace per market each poll), and
derive a probability-weighted expected fed-funds rate for the nearest FOMC into
ts_macro as KALSHI_FOMC_RATE — a clean scalar that charts and that the Phase 15
divergence engine can contrast against FRED's EFFR/DFF curve (a bond-vs-
prediction-market gap is the tradeable signal).

Keyless public host is api.elections.kalshi.com (the trade-api/v2 path). Same
fail-soft contract as every other ingestor.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

from app.db.duck import DuckStore
from app.ingest.http import HttpClient
from app.ingest.macro import MacroRow, _parse_date
from app.profile import gated

log = logging.getLogger("market.ingest.kalshi")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
# Tracked series: FOMC rate path + CPI. Series tickers are not fully documented
# and seasonal contracts rotate, but KXFED/KXCPI have been stable; the markets
# endpoint simply returns [] for a retired ticker (fail-soft).
KALSHI_SERIES = ("KXFED", "KXCPI")


def _price(m: dict) -> float | None:
    """Implied yes probability 0..1 from the dollar price fields (Kalshi moved
    cents -> *_dollars). Prefer the bid/ask mid, fall back to last trade."""
    bid = m.get("yes_bid_dollars")
    ask = m.get("yes_ask_dollars")
    try:
        b = float(bid) if bid not in (None, "") else None
        a = float(ask) if ask not in (None, "") else None
    except (TypeError, ValueError):
        b = a = None
    if b is not None and a is not None and (b > 0 or a > 0):
        return (b + a) / 2.0
    last = m.get("last_price_dollars")
    try:
        return float(last) if last not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _f(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def fetch_series_markets(http: HttpClient, series_ticker: str) -> list[dict]:
    """All open markets for one series (one page covers a series' live events)."""
    try:
        data = await http.get_json(
            f"{KALSHI_BASE}/markets",
            params={"series_ticker": series_ticker, "status": "open", "limit": "1000"},
            conditional=False,
        )
        return data.get("markets", []) or []
    except Exception as exc:
        log.warning("kalshi %s markets failed: %s", series_ticker, exc)
        return []


def _nearest_event(markets: list[dict]) -> str | None:
    """event_ticker of the soonest-closing event (the next FOMC / CPI print)."""
    closes: dict[str, str] = {}
    for m in markets:
        et = m.get("event_ticker")
        ct = m.get("close_time") or ""
        if et and (et not in closes or ct < closes[et]):
            closes[et] = ct
    if not closes:
        return None
    return min(closes, key=lambda e: closes[e] or "z")


def _rows_for_series(series_ticker: str, markets: list[dict]) -> list[tuple]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows: list[tuple] = []
    for m in markets:
        tk = m.get("ticker")
        et = m.get("event_ticker")
        if not tk or not et:
            continue
        close = _parse_date((m.get("close_time") or "")[:10])
        rows.append(
            (
                tk,
                series_ticker,
                et,
                m.get("yes_sub_title") or m.get("subtitle"),
                _f(m.get("floor_strike")),
                _price(m),
                _f(m.get("volume_fp")) or _f(m.get("volume")),
                datetime(close.year, close.month, close.day) if close else None,
                now,
            )
        )
    return rows


def expected_fomc_rate(markets: list[dict], event_ticker: str) -> float | None:
    """Probability-weighted expected target rate from the nearest FOMC ladder.

    KXFED strikes are cumulative P(rate above floor_strike), monotonically
    decreasing. The per-bucket probability between adjacent strikes is the
    difference; the bucket's representative rate is its lower edge + 12.5bp
    (mid of the 25bp step). Returns the weighted mean implied target rate."""
    ladder = sorted(
        (
            (s, p)
            for m in markets
            if m.get("event_ticker") == event_ticker
            and (s := _f(m.get("floor_strike"))) is not None
            and (p := _price(m)) is not None
        ),
        key=lambda sp: sp[0],
    )
    if len(ladder) < 2:
        return None
    num = 0.0
    den = 0.0
    for (lo, p_lo), (hi, p_hi) in zip(ladder, ladder[1:]):
        bucket = max(0.0, p_lo - p_hi)  # P(rate in [lo, hi))
        num += bucket * (lo + (hi - lo) / 2.0)
        den += bucket
    # Tail above the top strike (P that rate exceeds the highest floor).
    top_strike, top_p = ladder[-1]
    if top_p > 0:
        num += top_p * (top_strike + 0.125)
        den += top_p
    return num / den if den > 0 else None


class KalshiPipeline:
    """fetch open KXFED/KXCPI markets -> upsert kalshi_markets + derive the
    FOMC expected-rate scalar into ts_macro (KALSHI_FOMC_RATE)."""

    def __init__(self, duck: DuckStore, http: HttpClient, *, series=KALSHI_SERIES) -> None:
        self._duck = duck
        self._http = http
        self._series = tuple(series)

    async def _store_markets(self, rows: list[tuple]) -> int:
        if not rows:
            return 0
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._duck.executemany,
            "INSERT OR REPLACE INTO kalshi_markets (market_ticker, series_ticker, "
            "event_ticker, yes_sub_title, floor_strike, yes_prob, volume, "
            "close_time, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        return len(rows)

    async def _store_macro(self, row: MacroRow) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._duck.execute,
            "INSERT OR REPLACE INTO ts_macro (series_id, ts, value, source) "
            "VALUES (?, ?, ?, ?)",
            [row.series_id, row.ts, row.value, row.source],
        )

    @gated("kalshi")
    async def run(self) -> None:
        """Kalshi Data ToS: personal/non-business use only, bans AI/ML use,
        forbids sharing without written permission — gated off in
        MARKET_PROFILE=commercial."""
        total = 0
        for st in self._series:
            markets = await fetch_series_markets(self._http, st)
            total += await self._store_markets(_rows_for_series(st, markets))
            if st == "KXFED" and markets:
                event = _nearest_event(markets)
                rate = expected_fomc_rate(markets, event) if event else None
                if rate is not None:
                    today = date.today()
                    await self._store_macro(
                        MacroRow(
                            "KALSHI_FOMC_RATE",
                            datetime(today.year, today.month, today.day),
                            round(rate, 4),
                            "kalshi",
                        )
                    )
                    log.info("kalshi FOMC expected rate (%s): %.3f%%", event, rate)
        log.info("kalshi ingest: %s market snapshots (%s series)", total, len(self._series))
