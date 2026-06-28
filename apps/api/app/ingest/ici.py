"""Phase 16 §11 rank 5 — ICI weekly fund flows + money-market fund assets.

Two keyless ICI Excel files give the canonical "where is the money going /
sitting" gauges that the terminal lacked:

  * combined_flows_data_<year>.xls — estimated *weekly* net flows into long-term
    mutual funds + ETFs, split equity / bond / hybrid / commodity ($millions).
    Persistent equity outflows with bond inflows = risk-off rotation in real
    dollars (orthogonal to the price-derived breadth/positioning signals).
  * mm_summary_data_<year>.xls — weekly total net assets of money-market funds
    split government / prime / Treasury&repo / tax-exempt ($millions). The
    $8T+ "cash on the sidelines" pile; a prime->government rotation is a
    textbook early stress tell, and institutional MMF outflows precede equity
    reallocation by days to weeks.

All series land in ts_macro (simple scalar weekly series, same as every other
macro print) under source "ici" so they chart and the corr engine can pick
them up. The Excel is legacy BIFF (.xls) — pandas parses it via xlrd.

The URL embeds the calendar year (a convention that changed once before, per
PLAN §11 "fragile" notes); we parameterize it and fall back to the prior year
so a January boundary or a not-yet-published current-year file degrades softly.
Same fail-soft contract as every other ingestor: one bad fetch never blocks
the rest.
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import date, datetime, timezone

from app.db.duck import DuckStore
from app.ingest.http import HttpClient
from app.ingest.macro import MacroRow, _d, _parse_date
from app.ingest.news import BROWSER_HEADERS

log = logging.getLogger("market.ingest.ici")

COMBINED_URL = "https://www.ici.org/combined_flows_data_{year}.xls"
MM_URL = "https://www.ici.org/mm_summary_data_{year}.xls"

# combined_flows: the "Estimated weekly fund flows" section. Value columns are
# $millions; the even spacer columns between them are blank. Verified column
# positions 2026-06-28 (header rows 4-6: Total / Equity / Hybrid / Bond /
# Commodity). The section follows a row whose first cell == "Estimated weekly
# fund flows"; a trailing "Note:" row also mentions "weekly" so we anchor on
# the exact section header, not a substring match.
COMBINED_HEADER = "estimated weekly fund flows"
COMBINED_COLS = {
    "ICI_FLOW_TOTAL": 1,
    "ICI_FLOW_EQUITY": 3,
    "ICI_FLOW_BOND": 11,
    "ICI_FLOW_HYBRID": 9,
    "ICI_FLOW_COMMODITY": 17,
}

# mm_summary: total-net-assets (TNA, $millions) columns for ALL money-market
# funds. Data rows start at index 9 (the three-row stacked header sits above).
# Verified column positions 2026-06-28.
MM_DATA_START = 9
MM_COLS = {
    "ICI_MMF_TOTAL": 2,
    "ICI_MMF_GOVT": 6,
    "ICI_MMF_TREASURY": 8,  # Treasury & repo sleeve within government
    "ICI_MMF_PRIME": 12,
    "ICI_MMF_TAXEXEMPT": 4,
}


def _num(v) -> float | None:
    """A spreadsheet cell -> float, or None for blanks/NaN/non-numerics."""
    if v is None or v != v:  # None or NaN
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _fetch_xls(http: HttpClient, url_tmpl: str) -> "object | None":
    """Download the current-year (fall back to prior-year) ICI .xls and return
    a header-less DataFrame, or None if neither year is reachable."""
    import pandas as pd  # heavy import kept off module load

    year = datetime.now(timezone.utc).year
    for yr in (year, year - 1):
        url = url_tmpl.format(year=yr)
        try:
            resp = await http.get(url, headers=BROWSER_HEADERS)
            df = pd.read_excel(io.BytesIO(resp.body), sheet_name=0, header=None)
            if df is not None and not df.empty:
                return df
        except Exception as exc:
            log.warning("ici fetch %s failed: %s", url, exc)
    return None


def _parse_flows(df) -> list[MacroRow]:
    """Estimated weekly fund-flow rows from the combined_flows sheet."""
    start = None
    for r in range(df.shape[0]):
        if str(df.iat[r, 0]).strip().lower() == COMBINED_HEADER:
            start = r + 1
            break
    if start is None:
        log.warning("ici flows: weekly section header not found")
        return []
    rows: list[MacroRow] = []
    for r in range(start, df.shape[0]):
        day = _parse_date(str(df.iat[r, 0]))
        if day is None:
            continue
        for sid, col in COMBINED_COLS.items():
            val = _num(df.iat[r, col]) if col < df.shape[1] else None
            if val is not None:
                rows.append(MacroRow(sid, _d(day), val, "ici"))
    return rows


def _parse_mmf(df) -> list[MacroRow]:
    """Weekly money-market-fund TNA rows from the mm_summary sheet."""
    rows: list[MacroRow] = []
    for r in range(MM_DATA_START, df.shape[0]):
        day = _parse_date(str(df.iat[r, 0]))
        if day is None:
            continue
        for sid, col in MM_COLS.items():
            val = _num(df.iat[r, col]) if col < df.shape[1] else None
            if val is not None:
                rows.append(MacroRow(sid, _d(day), val, "ici"))
    return rows


async def fetch_flows(http: HttpClient) -> list[MacroRow]:
    df = await _fetch_xls(http, COMBINED_URL)
    if df is None:
        return []
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _parse_flows, df)


async def fetch_mmf(http: HttpClient) -> list[MacroRow]:
    df = await _fetch_xls(http, MM_URL)
    if df is None:
        return []
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _parse_mmf, df)


class ICIPipeline:
    """fetch ICI weekly flows + MMF assets -> upsert into ts_macro."""

    def __init__(self, duck: DuckStore, http: HttpClient) -> None:
        self._duck = duck
        self._http = http

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

    async def run(self) -> None:
        n_flows = await self._store(await fetch_flows(self._http))
        n_mmf = await self._store(await fetch_mmf(self._http))
        log.info("ici ingest: %s flow points + %s mmf points", n_flows, n_mmf)
