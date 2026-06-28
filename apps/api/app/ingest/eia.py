"""Phase 16 §11 rank 10 — EIA weekly energy fundamentals (WPSR + nat-gas).

Three keyless EIA CSVs give the only genuine energy-fundamentals signals on the
terminal — the weekly catalysts that move energy names:

  * wpsr/table1.csv  — Weekly Petroleum Status Report stock levels ($mmbbl):
    commercial crude (ex-SPR), SPR, gasoline, distillate, plus each week's
    build/draw. Released Wed 10:30 ET.
  * wpsr/table11.csv — daily spot prices: WTI, Brent, RBOB, No.2 heating oil.
    From these we compute the in-process 3:2:1 crack spread (refining margin).
  * ngs/wngsr.csv    — Working Gas in Underground Storage (Lower 48, Bcf):
    total stocks, weekly net change, and the surplus/deficit vs the 5-year
    average and vs a year ago — the primary weekly nat-gas price catalyst.
    Released Thu 10:30 ET.

Everything lands in ts_macro (scalar series, source "eia") so it charts like
every other macro print — no new table. Same fail-soft contract: any one CSV
can fail without blocking the others.

The 3:2:1 crack = (2*gasoline + 1*distillate - 3*WTI) / 3 in $/bbl. Product
prices are quoted $/gal, so they are converted to $/bbl (×42) before the spread
— the PLAN's note dropped that conversion, which would make the number
meaningless. We use RBOB for the gasoline leg and No.2 heating oil for the
distillate leg, both standard for the 3:2:1.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from datetime import date, datetime

from app.db.duck import DuckStore
from app.ingest.http import HttpClient
from app.ingest.macro import MacroRow, _d, _parse_date

log = logging.getLogger("market.ingest.eia")

TABLE1_URL = "https://ir.eia.gov/wpsr/table1.csv"
TABLE11_URL = "https://ir.eia.gov/wpsr/table11.csv"
WNGSR_URL = "https://ir.eia.gov/ngs/wngsr.csv"

GAL_PER_BBL = 42.0  # product spot prices are $/gal; crack spread is $/bbl

# table1 stock rows we keep (STUB label -> series suffix). Levels are $mmbbl;
# the week-over-week build/draw is the 3rd numeric column ("Difference").
CRUDE_STOCKS = {
    "Commercial (Excluding SPR)": "CRUDE_COMMERCIAL",
    "Strategic Petroleum Reserve (SPR)": "CRUDE_SPR",
    "Total Motor Gasoline": "GASOLINE_STOCKS",
    "Distillate Fuel Oil": "DISTILLATE_STOCKS",
}

# table11 daily-spot products, matched on (group, grade) cells in the first
# three columns. RBOB + heating oil feed the crack spread.
SPOT_PRODUCTS = {
    ("Crude Oil", "WTI - Cushing"): "WTI",
    ("Crude Oil", "Brent"): "BRENT",
    ("RBOB Regular", "Los Angeles"): "RBOB",
    ("Heating Oils", "No. 2 Heating Oil"): "HEATING_OIL",
}


def _num(s) -> float | None:
    """EIA cell -> float. Strips thousands commas; the report uses a replacement
    glyph for n/a cells which arrives as '?'/'\\ufffd' after a lossy decode."""
    s = str(s).replace(",", "").strip()
    if not s or s in ("-", "�") or s.startswith("�"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def parse_crude(text: str) -> tuple[date | None, list[MacroRow]]:
    """table1 -> (report week-ending date, stock-level + build/draw rows)."""
    rows = _rows(text)
    if not rows:
        return None, []
    anchor = _parse_date(rows[0][1]) if len(rows[0]) > 1 else None
    if anchor is None:
        return None, []
    ts = _d(anchor)
    out: list[MacroRow] = []
    for r in rows[1:]:
        if not r or r[0] not in CRUDE_STOCKS:
            continue
        suffix = CRUDE_STOCKS[r[0]]
        level = _num(r[1]) if len(r) > 1 else None
        chg = _num(r[3]) if len(r) > 3 else None
        if level is not None:
            out.append(MacroRow(f"EIA_{suffix}", ts, level, "eia"))
        if suffix == "CRUDE_COMMERCIAL" and chg is not None:
            out.append(MacroRow("EIA_CRUDE_COMMERCIAL_CHG", ts, chg, "eia"))
    return anchor, out


def parse_spot(text: str, anchor: date) -> list[MacroRow]:
    """table11 daily-price block -> per-day spot series + 3:2:1 crack spread.

    The daily block is the second 'STUB_1' header row whose later columns are
    day labels ('Fri 6/19'); those labels carry no year, so we anchor each to
    the report week (wrapping back a year for a Dec/Jan boundary)."""
    rows = _rows(text)
    di = None
    for i, r in enumerate(rows):
        if r and r[0] == "STUB_1" and any("/" in c for c in r[3:]):
            di = i
            break
    if di is None:
        log.warning("eia table11: daily price block not found")
        return []
    header = rows[di]
    day_cols = [(j, c) for j, c in enumerate(header) if "/" in c]

    def _col_date(label: str) -> date | None:
        m = re.search(r"(\d{1,2})/(\d{1,2})", label)
        if not m:
            return None
        mo, da = int(m.group(1)), int(m.group(2))
        try:
            d = date(anchor.year, mo, da)
        except ValueError:
            return None
        if (d - anchor).days > 7:  # Dec column on a January report
            d = date(anchor.year - 1, mo, da)
        return d

    def _key(r: list[str]) -> str | None:
        cells = [c.strip() for c in r[:3]]
        for (grp, grade), k in SPOT_PRODUCTS.items():
            if grp in cells and grade in cells:
                return k
        return None

    daily: dict[date, dict[str, float]] = {}
    for r in rows[di + 1 :]:
        k = _key(r)
        if not k:
            continue
        for j, label in day_cols:
            v = _num(r[j]) if j < len(r) else None
            if v is None:
                continue
            d = _col_date(label)
            if d is not None:
                daily.setdefault(d, {})[k] = v

    out: list[MacroRow] = []
    for d, prices in daily.items():
        ts = _d(d)
        for k, v in prices.items():
            out.append(MacroRow(f"EIA_{k}", ts, v, "eia"))
        if all(x in prices for x in ("WTI", "RBOB", "HEATING_OIL")):
            crack = (
                2 * prices["RBOB"] * GAL_PER_BBL
                + prices["HEATING_OIL"] * GAL_PER_BBL
                - 3 * prices["WTI"]
            ) / 3.0
            out.append(MacroRow("EIA_CRACK_321", ts, round(crack, 3), "eia"))
    return out


# Fixed cell indices in the wngsr "Total" row (the layout interleaves empty
# quoted cells; positional indices are stable, a value of "0" still parses).
_WNGSR_FIELDS = {
    1: "EIA_NATGAS_STORAGE",       # total working gas, Bcf
    7: "EIA_NATGAS_CHANGE",        # weekly net change (+inject / -draw), Bcf
    16: "EIA_NATGAS_VS_YRAGO_PCT",  # % vs year ago
    22: "EIA_NATGAS_VS_5YR_PCT",   # % vs 5-year average
}


def parse_natgas(text: str) -> list[MacroRow]:
    """wngsr -> total storage, weekly change, and vs-5yr / vs-year-ago %."""
    m = re.search(r"Week Ending ([A-Za-z]+ \d{1,2}, 20\d\d)", text)
    if not m:
        log.warning("eia wngsr: week-ending date not found")
        return []
    try:
        wk = datetime.strptime(m.group(1), "%B %d, %Y").date()
    except ValueError:
        return []
    ts = _d(wk)
    for r in _rows(text):
        if r and r[0].strip() == "Total":
            out = []
            for idx, sid in _WNGSR_FIELDS.items():
                v = _num(r[idx]) if idx < len(r) else None
                if v is not None:
                    out.append(MacroRow(sid, ts, v, "eia"))
            return out
    log.warning("eia wngsr: Total row not found")
    return []


async def _get_text(http: HttpClient, url: str) -> str | None:
    try:
        return (await http.get_text(url)).lstrip("﻿")
    except Exception as exc:
        log.warning("eia fetch %s failed: %s", url, exc)
        return None


class EIAPipeline:
    """fetch the three weekly EIA CSVs -> upsert energy series into ts_macro."""

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
        anchor: date | None = None
        n_crude = n_spot = n_gas = 0

        t1 = await _get_text(self._http, TABLE1_URL)
        if t1:
            anchor, crude_rows = parse_crude(t1)
            n_crude = await self._store(crude_rows)

        t11 = await _get_text(self._http, TABLE11_URL)
        if t11:
            n_spot = await self._store(parse_spot(t11, anchor or date.today()))

        ng = await _get_text(self._http, WNGSR_URL)
        if ng:
            n_gas = await self._store(parse_natgas(ng))

        log.info(
            "eia ingest: %s crude + %s spot + %s natgas points", n_crude, n_spot, n_gas
        )
