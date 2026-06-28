"""Phase 16 §11 rank 9 — Cleveland Fed inflation nowcast (daily).

The Cleveland Fed publishes a daily nowcast of CPI, core CPI, PCE and core PCE
— both month-over-month and year-over-year — updated ~10am ET from oil/gasoline
prices and the prior official print. It is the only confirmed-live, keyless,
*daily* inflation estimate ahead of the BLS/BEA releases, so the gap between
today's nowcast and yesterday's (and between the nowcast and the eventual
actual) is a clean pre-CPI / pre-PCE surprise-direction signal the terminal
otherwise misses entirely.

Two keyless JSON files in FusionCharts format:
  * nowcast_month.json — month-over-month percent change
  * nowcast_year.json  — year-over-year percent change

Each file is ``[ { "chart": {...}, "categories": [...], "dataset": [...] } ]``.
``chart._comment`` carries the as-of timestamp ("YYYY-MM-DD HH:MM"). ``dataset``
holds 8 series: a nowcast and an "Actual ..." realized series for each of the
four metrics; data points whose ``value`` is "" are future/blank, so the live
estimate is the *last non-empty* point of each series.

We stamp every series at the file's as-of date and upsert into ts_macro (source
"cleveland", no new table). Because the as-of date advances daily, INSERT OR
REPLACE naturally accumulates a daily history of the nowcast — the drift series
the surprise signal is read from. Same fail-soft contract: either file can fail
without blocking the other, and an empty metric (mid-cycle, e.g. CPI between
prints) is simply skipped, not zero-filled.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime

from app.db.duck import DuckStore
from app.ingest.http import HttpClient
from app.ingest.macro import MacroRow, _d

log = logging.getLogger("market.ingest.cleveland")

MONTH_URL = "https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast_month.json"
YEAR_URL = "https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast_year.json"

# FusionCharts seriesname (base, after stripping a leading "Actual ") -> metric.
_METRIC = {
    "CPI Inflation": "CPI",
    "Core CPI Inflation": "CORECPI",
    "PCE Inflation": "PCE",
    "Core PCE Inflation": "COREPCE",
}


def _as_of(chart: dict) -> date:
    """chart._comment 'YYYY-MM-DD HH:MM' -> date (falls back to today)."""
    raw = (chart.get("_comment") or "").strip()
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return date.today()


def _last_value(data: list[dict]) -> float | None:
    """Last non-blank FusionCharts data point -> float (the live estimate)."""
    out: float | None = None
    for pt in data:
        v = pt.get("value", "")
        if v == "" or v is None:
            continue
        try:
            out = float(v)
        except (TypeError, ValueError):
            continue
    return out


def parse(text: str, freq: str) -> tuple[date | None, list[MacroRow]]:
    """One nowcast JSON -> (as-of date, latest nowcast + actual rows).

    freq is "MOM" (month file) or "YOY" (year file); series_id is
    CLEV_<METRIC>_<freq>[ _ACTUAL ]."""
    doc = json.loads(text)
    if not doc:
        return None, []
    chart = doc[0]
    asof = _as_of(chart.get("chart", {}))
    ts = _d(asof)
    rows: list[MacroRow] = []
    for series in chart.get("dataset", []):
        name = (series.get("seriesname") or "").strip()
        is_actual = name.startswith("Actual")
        base = name[len("Actual"):].strip() if is_actual else name
        metric = _METRIC.get(base)
        if metric is None:
            continue
        val = _last_value(series.get("data", []))
        if val is None:
            continue
        suffix = f"{freq}_ACTUAL" if is_actual else freq
        rows.append(MacroRow(f"CLEV_{metric}_{suffix}", ts, round(val, 4), "cleveland"))
    return asof, rows


class ClevelandPipeline:
    """fetch the two nowcast JSON files -> upsert inflation-nowcast series."""

    def __init__(self, duck: DuckStore, http: HttpClient) -> None:
        self._duck = duck
        self._http = http

    async def _get(self, url: str) -> str | None:
        try:
            return await self._http.get_text(url)
        except Exception as exc:
            log.warning("cleveland fetch %s failed: %s", url, exc)
            return None

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
        n_mom = n_yoy = 0
        mom = await self._get(MONTH_URL)
        if mom:
            try:
                _, rows = parse(mom, "MOM")
                n_mom = await self._store(rows)
            except Exception as exc:
                log.warning("cleveland month parse failed: %s", exc)
        yoy = await self._get(YEAR_URL)
        if yoy:
            try:
                _, rows = parse(yoy, "YOY")
                n_yoy = await self._store(rows)
            except Exception as exc:
                log.warning("cleveland year parse failed: %s", exc)
        log.info("cleveland nowcast ingest: %s MoM + %s YoY points", n_mom, n_yoy)
