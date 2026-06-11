"""Event Horizon — forward calendar of market-moving events.

Not in the original PLAN §6 list, but the obvious missing daily-driver layer:
knowing CPI/FOMC/OPEX is *tomorrow* changes how every other signal is read
(don't fade a correlation break into an FOMC decision).

Legs, cheapest first:
  * FOMC decision days — the Fed announces the schedule a year ahead; static
    table below (source: federalreserve.gov/monetarypolicy/fomccalendars.htm).
  * OPEX / quad-witching + quarterly futures roll — pure date math (3rd Friday).
  * COT report Fridays — date math.
  * CPI / NFP / PPI / GDP / PCE release dates — FRED release-calendar API when
    a key is set (include_release_dates_with_no_data returns future dates);
    keyless fallback: NFP estimated as first Friday, others skipped.
  * Watchlist earnings dates — yfinance (blocking, executor), refreshed daily.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ingest.http import HttpClient

log = logging.getLogger("market.edge.calendar")

# FOMC decision (statement) days — FALLBACK only; the live schedule is now
# scraped from federalreserve.gov each run (Phase 9 §3). Extend yearly so the
# fallback stays useful when the scrape breaks.
FOMC_DECISIONS = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

# DOM verified 2026-06-10: per-year panels headed by <h4>…YYYY FOMC Meetings…
# </h4>, each meeting a row with a fomc-meeting__month div (<strong>January
# </strong>, or "April/May" when a meeting straddles months) and a
# fomc-meeting__date div ("27-28", "3-4*", single "29" possible).
_FOMC_YEAR_RE = re.compile(r"(\d{4})\s+FOMC\s+Meetings", re.I)
_FOMC_MONTH_RE = re.compile(
    r'fomc-meeting__month[^>]*>\s*(?:<strong>)?\s*([A-Za-z./ ]+(?:/[A-Za-z.]+)?)', re.I
)
_FOMC_DATE_RE = re.compile(r'fomc-meeting__date[^>]*>([^<]*)<', re.I)

_MONTHS = {
    m.lower(): i + 1 for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"]
    )
}


def _month_num(name: str) -> int | None:
    name = name.strip().strip(".").lower()
    if name in _MONTHS:
        return _MONTHS[name]
    for full, num in _MONTHS.items():  # tolerate "Jan", "Sept" abbreviations
        if full.startswith(name[:3]) and len(name) >= 3:
            return num
    return None


def parse_fomc_meetings(html: str) -> list[date]:
    """Fed calendar page -> decision days (the LAST day of each meeting)."""
    years = [(m.start(), int(m.group(1))) for m in _FOMC_YEAR_RE.finditer(html)]
    if not years:
        return []
    months = [(m.start(), m.group(1)) for m in _FOMC_MONTH_RE.finditer(html)]
    dates = [(m.start(), m.group(1)) for m in _FOMC_DATE_RE.finditer(html)]

    def _year_at(pos: int) -> int | None:
        prior = [y for p, y in years if p < pos]
        return prior[-1] if prior else None

    out: list[date] = []
    di = 0
    for pos, month_text in months:
        # The date cell that belongs to this month cell is the next one after it.
        while di < len(dates) and dates[di][0] < pos:
            di += 1
        if di >= len(dates):
            break
        date_text = dates[di][1]
        di += 1
        year = _year_at(pos)
        if year is None:
            continue
        day_nums = re.findall(r"\d+", date_text)
        if not day_nums:
            continue
        # "April/May" + "28-29" -> the decision day is the last day in the
        # last-listed month; a single month keeps that month for both days.
        month_names = [p for p in month_text.split("/") if p.strip()]
        last_month = _month_num(month_names[-1]) if month_names else None
        if last_month is None:
            continue
        try:
            out.append(date(year, last_month, int(day_nums[-1])))
        except ValueError:
            continue
    return sorted(set(out))

# FRED release ids for the prints that move markets.
FRED_RELEASES = {
    10: ("cpi", "CPI release (08:30 ET)"),
    50: ("nfp", "Employment Situation / NFP (08:30 ET)"),
    46: ("ppi", "PPI release (08:30 ET)"),
    53: ("gdp", "GDP release (08:30 ET)"),
    54: ("pce", "Personal Income & Outlays / PCE (08:30 ET)"),
}
FRED_RELEASE_DATES = "https://api.stlouisfed.org/fred/release/dates"

HORIZON_DAYS = 120


def _third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)  # 3rd Friday is always the 15th..21st
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def _next_fridays(start: date, n: int) -> list[date]:
    d = start + timedelta(days=(4 - start.weekday()) % 7)
    return [d + timedelta(weeks=i) for i in range(n)]


class CalendarPipeline:
    def __init__(self, duck: DuckStore, sqlite: SqliteStore, http: HttpClient,
                 fred_api_key: str = "") -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._http = http
        self._fred_key = fred_api_key

    def _upsert(self, events: list[tuple[str, date, str, str, str | None, str]]) -> None:
        self._duck.executemany(
            "INSERT OR REPLACE INTO events (id, ts, kind, title, symbol, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(eid, datetime(d.year, d.month, d.day), kind, title, sym, src)
             for eid, d, kind, title, sym, src in events],
        )

    async def _fomc_events(self) -> list[tuple]:
        """Scrape the live FOMC schedule; static table is the fallback."""
        today = date.today()
        horizon = today + timedelta(days=HORIZON_DAYS)
        days: list[date] = []
        source = "federalreserve.gov (scraped)"
        try:
            html = await self._http.get_text(FOMC_URL)
            days = [d for d in parse_fomc_meetings(html) if today <= d <= horizon]
            if not days:
                log.warning("FOMC scrape returned no upcoming meetings — using static table")
        except Exception as exc:
            log.warning("FOMC calendar scrape failed (%s) — using static table", exc)
        if not days:
            source = "federalreserve.gov (static fallback)"
            days = [d for i in FOMC_DECISIONS
                    if today <= (d := date.fromisoformat(i)) <= horizon]
            if not days and not any(date.fromisoformat(i) >= today for i in FOMC_DECISIONS):
                log.warning("FOMC static table exhausted — add the new year's dates")
        return [(f"fomc:{d}", d, "fomc",
                 "FOMC rate decision (14:00 ET) + presser", None, source)
                for d in days]

    def _static_events(self) -> list[tuple]:
        today = date.today()
        horizon = today + timedelta(days=HORIZON_DAYS)
        ev: list[tuple] = []
        m_year, m_month = today.year, today.month
        for _ in range(5):
            opex = _third_friday(m_year, m_month)
            if today <= opex <= horizon:
                quad = m_month in (3, 6, 9, 12)
                ev.append((f"opex:{opex}", opex, "opex",
                           "Quad witching (index futures+options expiry)" if quad
                           else "Monthly OPEX (3rd Friday)", None, "computed"))
                if quad:
                    roll = opex - timedelta(days=8)
                    if roll >= today:
                        ev.append((f"roll:{roll}", roll, "opex",
                                   "Equity futures roll week begins", None, "computed"))
            m_month += 1
            if m_month > 12:
                m_month, m_year = 1, m_year + 1

        for d in _next_fridays(today, 2):
            ev.append((f"cot:{d}", d, "cot", "CFTC COT report (15:30 ET)",
                       None, "computed"))
        return ev

    async def _fred_release_events(self) -> list[tuple]:
        today = date.today()
        horizon = today + timedelta(days=HORIZON_DAYS)
        ev: list[tuple] = []
        if not self._fred_key:
            # Keyless: only NFP is safely predictable (first Friday).
            d = today.replace(day=1)
            for _ in range(4):
                first_friday = d + timedelta(days=(4 - d.weekday()) % 7)
                if today <= first_friday <= horizon:
                    ev.append((f"nfp:{first_friday}", first_friday, "nfp",
                               "NFP / jobs report (est. — first Friday)", None,
                               "estimated"))
                d = (d + timedelta(days=32)).replace(day=1)
            return ev
        for rid, (kind, title) in FRED_RELEASES.items():
            try:
                data = await self._http.get_json(
                    FRED_RELEASE_DATES,
                    params={
                        "release_id": str(rid),
                        "api_key": self._fred_key,
                        "file_type": "json",
                        "include_release_dates_with_no_data": "true",
                        "realtime_start": today.isoformat(),
                        "realtime_end": horizon.isoformat(),
                        "sort_order": "asc",
                        "limit": "20",
                    },
                )
            except Exception as exc:
                log.warning("fred release dates %s failed: %s", rid, exc)
                continue
            for rd in data.get("release_dates", []):
                d = date.fromisoformat(rd.get("date", "1970-01-01"))
                if today <= d <= horizon:
                    ev.append((f"{kind}:{d}", d, kind, title, None, "fred"))
        return ev

    def _earnings_events(self) -> list[tuple]:
        """Blocking (yfinance) — run in executor."""
        import yfinance as yf

        rows = self._sqlite.fetchall(
            "SELECT symbol FROM watchlist WHERE asset_class = 'equity' ORDER BY sort_order"
        )
        today = date.today()
        horizon = today + timedelta(days=HORIZON_DAYS)
        ev: list[tuple] = []
        for (sym,) in rows:
            try:
                df = yf.Ticker(sym).get_earnings_dates(limit=8)
            except Exception as exc:
                log.debug("earnings dates %s failed: %s", sym, exc)
                continue
            if df is None or df.empty:
                continue
            future = sorted(
                ts.date() for ts in df.index
                if today <= ts.date() <= horizon
            )
            if future:
                d = future[0]
                ev.append((f"earnings:{d}:{sym}", d, "earnings",
                           f"{sym} earnings", sym, "yahoo"))
        return ev

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        events = self._static_events()
        events += await self._fomc_events()
        events += await self._fred_release_events()
        try:
            events += await loop.run_in_executor(None, self._earnings_events)
        except Exception as exc:
            log.warning("earnings leg failed: %s", exc)
        # Every leg is fully regenerated each run, so drop all future rows
        # first — a rescheduled release/earnings date can't leave a ghost.
        await loop.run_in_executor(
            None, self._duck.execute,
            "DELETE FROM events WHERE ts >= current_date", None,
        )
        await loop.run_in_executor(None, self._upsert, events)
        log.info("calendar refreshed: %s upcoming events", len(events))
        return len(events)


def upcoming_events(duck: DuckStore, days: int = 30) -> list[dict]:
    df = duck.fetchdf(
        "SELECT id, ts, kind, title, symbol, source FROM events "
        "WHERE ts >= current_date AND ts <= current_date + INTERVAL (?) DAY "
        "ORDER BY ts, kind",
        [days],
    )
    if df is None or df.empty:
        return []
    today = date.today()
    out = []
    for _, r in df.iterrows():
        d = r["ts"].date()
        out.append({
            "id": r["id"],
            "date": d.isoformat(),
            "days_until": (d - today).days,
            "kind": r["kind"],
            "title": r["title"],
            "symbol": r["symbol"] if r["symbol"] == r["symbol"] else None,
            "source": r["source"],
        })
    return out
