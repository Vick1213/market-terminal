"""Phase 16 §11 rank 14 — BIS CBTA global central-bank balance sheets.

One keyless BIS Stats CSV assembles total assets for the Fed, ECB, BoJ and PBoC
on a common, break-adjusted basis — the only free source that lets us build a
true *global* QE/QT score. The DBnomics intl cards give individual CB slices but
never the aggregate. Pairing the global USD sum against FRED's WALCL gives a
global-vs-Fed liquidity divergence: when the rest of the world is still easing
while the Fed tightens (or vice versa), the net global impulse the S&P actually
trades on diverges from the Fed-only story.

Per central bank we keep two views in ts_macro (quarterly scalars, source "bis"):
  * BIS_CB_ASSETS_<area>  — total assets, USD-converted, $bn (UNIT_MULT 9)
  * BIS_CB_GDP_<area>     — total assets as a ratio to GDP
plus BIS_CB_ASSETS_GLOBAL — the USD sum across all four (only quarters where
every CB reports, so the aggregate is never silently partial).

The plan's example endpoint is annual (FREQ=A) which yields ~6 points; we pull
FREQ=Q instead — through the latest quarter, ~1 quarter lag — so the series is
granular enough to chart against weekly WALCL. Keyless CSV, same fail-soft
contract as every other ingestor.
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import date, datetime

from app.db.duck import DuckStore
from app.ingest.http import HttpClient
from app.ingest.macro import MacroRow, _d

log = logging.getLogger("market.ingest.bis")

# FREQ=Q, the four largest balance sheets; "..": wildcard the remaining
# dimensions (COMP_METHOD / UNIT_MEASURE / CURRENCY / TRANSFORMATION) so the one
# CSV carries the domestic-currency, USD-converted and GDP-ratio variants.
CBTA_URL = (
    "https://stats.bis.org/api/v1/data/WS_CBTA/Q.JP+US+XM+CN..?startPeriod=2018&format=csv"
)
AREAS = ("US", "JP", "XM", "CN")
# Quarter-end month for stamping a "YYYY-Qn" period onto a ts_macro date.
_Q_MONTH = {"1": 3, "2": 6, "3": 9, "4": 12}
_Q_DAY = {3: 31, 6: 30, 9: 30, 12: 31}


def _num(v) -> float | None:
    if v is None or v != v:  # None or NaN
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _quarter_date(period: str) -> date | None:
    """'2026-Q1' -> 2026-03-31 (quarter-end)."""
    try:
        year, q = str(period).split("-Q")
        month = _Q_MONTH[q.strip()]
        return date(int(year), month, _Q_DAY[month])
    except (ValueError, KeyError):
        return None


def _parse(body: bytes) -> list[MacroRow]:
    """BIS CBTA CSV -> per-CB USD + GDP-ratio rows + the global USD sum.

    Selecting the USD-converted, break-adjusted (TRANSFORMATION 'B') series per
    area: for the US the domestic currency *is* USD so its USD value lives under
    UNIT_MEASURE 'XDC'; for JP/XM/CN the converted value is UNIT_MEASURE 'USD'.
    GDP ratio is UNIT_MEASURE 'XDF_R_B1GQ' for all areas."""
    import pandas as pd  # heavy import kept off module load

    df = pd.read_csv(io.BytesIO(body))
    rows: list[MacroRow] = []
    usd_by_period: dict[str, dict[str, float]] = {}

    for area in AREAS:
        usd_unit = "XDC" if area == "US" else "USD"
        usd = df[
            (df.REF_AREA == area)
            & (df.UNIT_MEASURE == usd_unit)
            & (df.TRANSFORMATION == "B")
        ]
        for _, r in usd.iterrows():
            day = _quarter_date(r["TIME_PERIOD"])
            val = _num(r["OBS_VALUE"])
            if day is not None and val is not None:
                rows.append(MacroRow(f"BIS_CB_ASSETS_{area}", _d(day), val, "bis"))
                usd_by_period.setdefault(str(r["TIME_PERIOD"]), {})[area] = val

        gdp = df[
            (df.REF_AREA == area)
            & (df.UNIT_MEASURE == "XDF_R_B1GQ")
            & (df.TRANSFORMATION == "B")
        ]
        for _, r in gdp.iterrows():
            day = _quarter_date(r["TIME_PERIOD"])
            val = _num(r["OBS_VALUE"])
            if day is not None and val is not None:
                rows.append(MacroRow(f"BIS_CB_GDP_{area}", _d(day), val, "bis"))

    for period, vals in usd_by_period.items():
        if len(vals) == len(AREAS):  # never a silently-partial aggregate
            day = _quarter_date(period)
            if day is not None:
                rows.append(
                    MacroRow("BIS_CB_ASSETS_GLOBAL", _d(day), sum(vals.values()), "bis")
                )
    return rows


async def fetch_cbta(http: HttpClient) -> list[MacroRow]:
    try:
        resp = await http.get(CBTA_URL)
    except Exception as exc:
        log.warning("bis cbta fetch failed: %s", exc)
        return []
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _parse, resp.body)
    except Exception as exc:
        log.warning("bis cbta parse failed: %s", exc)
        return []


class BISPipeline:
    """fetch BIS CBTA -> upsert per-CB + global balance-sheet series in ts_macro."""

    def __init__(self, duck: DuckStore, http: HttpClient) -> None:
        self._duck = duck
        self._http = http

    async def run(self) -> None:
        rows = await fetch_cbta(self._http)
        if rows:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                self._duck.executemany,
                "INSERT OR REPLACE INTO ts_macro (series_id, ts, value, source) "
                "VALUES (?, ?, ?, ?)",
                [(r.series_id, r.ts, r.value, r.source) for r in rows],
            )
        log.info("bis cbta ingest: %s points", len(rows))
