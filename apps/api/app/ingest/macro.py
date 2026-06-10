"""Phase 2 ingestors — FRED, CBOE CDN, FINRA short-sale volume, NAAIM, AAII.

Every fetcher returns plain (series_id, date, value, source) rows for ts_macro
and is allowed to fail: one fragile source never blocks the others (the same
contract as the news ingestors). After each successful run the pipeline
recomputes the composite Risk-On/Off score and pushes it on WS topic "macro".
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.db.duck import DuckStore
from app.ingest.http import HttpClient
from app.ingest.news import BROWSER_HEADERS
from app.macro.composite import CompositeResult, compute_composite
from app.ws.hub import ConnectionManager

log = logging.getLogger("market.ingest.macro")

MACRO_TOPIC = "macro"

# FRED series per PLAN §3c (yield curve, real/breakeven, conditions, credit,
# Fed plumbing, money/rates, VIX backup, broad USD, oil).
FRED_SERIES = [
    "T10Y2Y", "T10Y3M", "DGS10", "DGS2",
    "DFII10", "T10YIE",
    "NFCI", "ANFCI",
    "BAMLH0A0HYM2", "BAMLC0A0CM",
    "WALCL", "RRPONTSYD", "WTREGEN", "WRESBAL",
    "M2SL", "DFF", "SOFR", "VIXCLS",
    "DTWEXBGS", "DCOILWTICO",
]
FRED_DEFAULT_START = "2015-01-01"

CBOE_HISTORY = {
    "VIX": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    "VIX3M": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv",
}
# Daily market-statistics page embeds TOTAL/INDEX/EQUITY put/call ratios as
# escaped JSON. The cdn.cboe.com volume_and_call_put_ratios CSVs are the
# 2006–2019 FROZEN files (verified 2026-06-09) — do not use them for current
# data; we backfill missing recent days from this page instead (browser UA).
CBOE_PC_DAILY = "https://www.cboe.com/us/options/market_statistics/daily/?dt={iso}"
CBOE_PC_SERIES = {
    "PC_TOTAL": "TOTAL PUT/CALL RATIO",
    "PC_INDEX": "INDEX PUT/CALL RATIO",
    "PC_EQUITY": "EQUITY PUT/CALL RATIO",
}
CBOE_PC_LOOKBACK_DAYS = 14

FINRA_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{ymd}.txt"
FINRA_LOOKBACK_DAYS = 14

NAAIM_PAGE = "https://naaim.org/programs/naaim-exposure-index/"
AAII_PAGE = "https://www.aaii.com/sentimentsurvey"

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")


@dataclass
class MacroRow:
    series_id: str
    ts: datetime
    value: float
    source: str


def _d(day: date) -> datetime:
    """Date -> naive midnight timestamp (ts_macro convention)."""
    return datetime(day.year, day.month, day.day)


def _parse_date(text: str) -> date | None:
    text = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# --- FRED ---------------------------------------------------------------


async def fetch_fred(
    http: HttpClient, series_id: str, api_key: str, start: str
) -> list[MacroRow]:
    """One FRED series via the keyed JSON API (free key). The keyless
    fredgraph.csv endpoint is connection-blocked for non-browser clients
    (verified 2026-06-09), so a key is required for the FRED leg."""
    try:
        data = await http.get_json(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                "observation_start": start,
            },
            conditional=False,  # key would be cached into the URL entry
        )
        pairs = [(o.get("date", ""), o.get("value", ".")) for o in data.get("observations", [])]
    except Exception as exc:
        log.warning("fred %s failed: %s", series_id, exc)
        return []
    rows: list[MacroRow] = []
    for ds, vs in pairs:
        day = _parse_date(ds)
        if day is None or vs.strip() in ("", "."):
            continue
        if ds < start:
            continue
        try:
            rows.append(MacroRow(series_id, _d(day), float(vs), "fred"))
        except ValueError:
            continue
    return rows


# --- CBOE ---------------------------------------------------------------


async def fetch_cboe_history(http: HttpClient, series_id: str, url: str) -> list[MacroRow]:
    """VIX / VIX3M daily history CSV (DATE,OPEN,HIGH,LOW,CLOSE) -> close."""
    try:
        text = await http.get_text(url)
    except Exception as exc:
        log.warning("cboe %s failed: %s", series_id, exc)
        return []
    rows: list[MacroRow] = []
    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) < 5:
            continue
        day = _parse_date(parts[0])
        if day is None:
            continue  # title/header lines
        try:
            rows.append(MacroRow(series_id, _d(day), float(parts[4]), "cboe"))
        except ValueError:
            continue
    return rows


async def fetch_cboe_putcall_day(http: HttpClient, day: date) -> list[MacroRow]:
    """TOTAL/INDEX/EQUITY put/call ratios for one day, scraped from the daily
    market-statistics page (the ratios sit in escaped JSON inside the HTML).
    Returns [] on holidays (page renders with 0.00 ratios) or fetch failure."""
    url = CBOE_PC_DAILY.format(iso=day.isoformat())
    try:
        page = await http.get_text(url, headers=BROWSER_HEADERS, conditional=False)
    except Exception as exc:
        log.warning("cboe put/call %s failed: %s", day, exc)
        return []
    rows: list[MacroRow] = []
    for series_id, name in CBOE_PC_SERIES.items():
        m = re.search(
            re.escape(name) + r'\\?"\s*,\s*\\?"value\\?"\s*:\s*\\?"([\d.]+)', page
        )
        if not m:
            continue
        try:
            value = float(m.group(1))
        except ValueError:
            continue
        if value > 0:  # holidays render as 0.00
            rows.append(MacroRow(series_id, _d(day), value, "cboe"))
    return rows


# --- FINRA --------------------------------------------------------------


async def fetch_finra_day(http: HttpClient, day: date) -> MacroRow | None:
    """One CNMSshvol daily file -> market-wide short_volume/total_volume.
    404 = weekend/holiday (expected); this is short SELL volume incl. MM
    hedging, NOT short interest (PLAN §3c)."""
    url = FINRA_URL.format(ymd=day.strftime("%Y%m%d"))
    try:
        text = await http.get_text(url, conditional=False)
    except Exception as exc:
        log.debug("finra %s unavailable: %s", day, exc)
        return None
    short_total = 0.0
    total = 0.0
    for line in text.splitlines()[1:]:  # header: Date|Symbol|ShortVolume|...
        parts = line.split("|")
        if len(parts) < 5:
            continue
        try:
            short_total += float(parts[2])
            total += float(parts[4])
        except ValueError:
            continue
    if total <= 0:
        return None
    return MacroRow("FINRA_SHORT_RATIO", _d(day), short_total / total, "finra")


# --- NAAIM / AAII -------------------------------------------------------


async def fetch_naaim(http: HttpClient) -> list[MacroRow]:
    """Weekly NAAIM Exposure Index: find the Excel link on the program page,
    parse Date + the mean/average exposure column."""
    import pandas as pd  # heavy import kept off module load

    try:
        page = await http.get_text(NAAIM_PAGE, headers=BROWSER_HEADERS)
        m = re.search(r'href="([^"]+\.xlsx?)"', page, re.IGNORECASE)
        if not m:
            log.warning("naaim: no excel link found on program page")
            return []
        resp = await http.get(m.group(1), headers=BROWSER_HEADERS)
        df = pd.read_excel(io.BytesIO(resp.body))
    except Exception as exc:
        log.warning("naaim failed: %s", exc)
        return []
    date_col = df.columns[0]
    value_col = None
    for col in df.columns[1:]:
        if re.search(r"mean|average|exposure|naaim", str(col), re.IGNORECASE):
            value_col = col
            break
    if value_col is None and len(df.columns) > 1:
        value_col = df.columns[1]
    rows: list[MacroRow] = []
    for _, r in df.iterrows():
        try:
            day = pd.Timestamp(r[date_col]).date()
            rows.append(MacroRow("NAAIM_EXPOSURE", _d(day), float(r[value_col]), "naaim"))
        except (ValueError, TypeError):
            continue
    return rows


async def fetch_aaii(http: HttpClient) -> list[MacroRow]:
    """Current week's AAII bull/neutral/bear from the public survey page
    (history is member-gated, so we append our own weekly prints). Cloudflare
    may block this — best-effort, skip-on-fail."""
    try:
        page = await http.get_text(
            AAII_PAGE,
            headers={**BROWSER_HEADERS, "Accept": "text/html,application/xhtml+xml"},
        )
    except Exception as exc:
        log.warning("aaii failed: %s", exc)
        return []
    rows: list[MacroRow] = []
    # Stamp on the most recent Thursday (publication day) so re-pulls replace.
    today = datetime.now(timezone.utc).date()
    thursday = today - timedelta(days=(today.weekday() - 3) % 7)
    for label, sid in (("Bullish", "AAII_BULL"), ("Neutral", "AAII_NEUT"), ("Bearish", "AAII_BEAR")):
        m = re.search(rf"{label}[^0-9%]*([\d.]+)\s*%", page, re.IGNORECASE)
        if m:
            try:
                rows.append(MacroRow(sid, _d(thursday), float(m.group(1)), "aaii"))
            except ValueError:
                continue
    if not rows:
        log.warning("aaii: page fetched but no percentages parsed")
    return rows


# --- Pipeline -----------------------------------------------------------


class MacroPipeline:
    """fetch -> store in ts_macro -> recompute composite -> WS-push."""

    def __init__(
        self,
        duck: DuckStore,
        hub: ConnectionManager,
        http: HttpClient,
        *,
        fred_api_key: str = "",
    ) -> None:
        self._duck = duck
        self._hub = hub
        self._http = http
        self._fred_api_key = fred_api_key

    async def _store(self, rows: list[MacroRow]) -> int:
        if not rows:
            return 0
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._duck.executemany,
            "INSERT OR REPLACE INTO ts_macro (series_id, ts, value, source) "
            "VALUES (?, ?, ?, ?)",
            [(r.series_id, r.ts, r.value, r.source) for r in rows],
        )
        return len(rows)

    def _series_start(self, series_id: str) -> str:
        """Incremental fetch start: a little before the last stored point."""
        row = self._duck.fetchone(
            "SELECT max(ts) FROM ts_macro WHERE series_id = ?", [series_id]
        )
        if row and row[0]:
            return (row[0] - timedelta(days=14)).strftime("%Y-%m-%d")
        return FRED_DEFAULT_START

    def _missing_weekdays(self, series_id: str, lookback_days: int) -> list[date]:
        """Recent business days with no stored point for `series_id`."""
        have = {
            r[0].date() if isinstance(r[0], datetime) else r[0]
            for r in self._duck.fetchall(
                "SELECT ts FROM ts_macro WHERE series_id = ? AND ts >= ?",
                [series_id, _d(date.today() - timedelta(days=lookback_days))],
            )
        }
        days = []
        for i in range(lookback_days + 1):
            day = date.today() - timedelta(days=i)
            if day.weekday() < 5 and day not in have:
                days.append(day)
        return days

    async def recompute_composite(self) -> CompositeResult | None:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, compute_composite, self._duck)
        if result is None:
            return None
        today = _d(date.today())

        def _persist() -> None:
            self._duck.execute(
                "INSERT OR REPLACE INTO ts_macro (series_id, ts, value, source) "
                "VALUES ('COMPOSITE_RISK', ?, ?, 'computed')",
                [today, result.score],
            )
            self._duck.execute(
                "INSERT OR REPLACE INTO macro_composite (ts, score, regime, detail) "
                "VALUES (?, ?, ?, ?)",
                [today, result.score, result.regime, result.detail_json()],
            )

        await loop.run_in_executor(None, _persist)
        await self._hub.broadcast(
            MACRO_TOPIC,
            {
                "type": "macro",
                "score": result.score,
                "regime": result.regime,
                "computed_at": result.computed_at,
            },
        )
        return result

    # Job entrypoints — each isolated, each ends with a composite refresh.

    async def run_fred(self) -> None:
        if not self._fred_api_key:
            log.info("fred ingest skipped (no MARKET_FRED_API_KEY)")
            return
        loop = asyncio.get_running_loop()
        total = 0
        for sid in FRED_SERIES:
            start = await loop.run_in_executor(None, self._series_start, sid)
            total += await self._store(
                await fetch_fred(self._http, sid, self._fred_api_key, start)
            )
        log.info("fred ingest: %s points (%s series)", total, len(FRED_SERIES))
        await self.recompute_composite()

    async def run_cboe(self) -> None:
        loop = asyncio.get_running_loop()
        total = 0
        for sid, url in CBOE_HISTORY.items():
            total += await self._store(await fetch_cboe_history(self._http, sid, url))
        # Put/call: one page fetch per missing recent business day (the three
        # ratios arrive together, keyed off PC_TOTAL's stored dates).
        pc_days = await loop.run_in_executor(
            None, self._missing_weekdays, "PC_TOTAL", CBOE_PC_LOOKBACK_DAYS
        )
        for day in pc_days:
            total += await self._store(await fetch_cboe_putcall_day(self._http, day))
        log.info("cboe ingest: %s points (%s put/call days)", total, len(pc_days))
        await self.recompute_composite()

    async def run_finra(self) -> None:
        loop = asyncio.get_running_loop()
        days = await loop.run_in_executor(
            None, self._missing_weekdays, "FINRA_SHORT_RATIO", FINRA_LOOKBACK_DAYS
        )
        rows = []
        for day in days:
            r = await fetch_finra_day(self._http, day)
            if r:
                rows.append(r)
        n = await self._store(rows)
        log.info("finra ingest: %s days filled (%s candidates)", n, len(days))
        if n:
            await self.recompute_composite()

    async def run_naaim(self) -> None:
        n = await self._store(await fetch_naaim(self._http))
        log.info("naaim ingest: %s points", n)
        if n:
            await self.recompute_composite()

    async def run_aaii(self) -> None:
        n = await self._store(await fetch_aaii(self._http))
        log.info("aaii ingest: %s points", n)
        if n:
            await self.recompute_composite()
