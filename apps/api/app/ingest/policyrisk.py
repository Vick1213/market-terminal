"""Phase 16 §11 rank 11 (legs 1+2) — geopolitical-risk + trade-policy-uncertainty.

Two keyless policy-risk indices the terminal otherwise lacks entirely. Both are
simple scalar time-series, so they land in ts_macro (no new table) alongside
every other macro print; the Fed-speech NLP leg of rank 11 is a separate
ingestor (a per-speech table + a derived scalar) — see app/ingest/fed_speeches.

  * GPR — Caldara & Iacoviello Geopolitical Risk index (monthly, 1985+). A
    legacy .xls; column ``month`` is a date and ``GPR`` / ``GPRT`` / ``GPRA``
    are the headline index and its Threats / Acts sub-indices. Threats lead
    Acts by 1-3 months, so carrying both gives an early-vs-realized read. Parsed
    via pandas/xlrd (io.BytesIO + BROWSER_HEADERS) like the ICI .xls.
  * TPU — Caldara et al. daily Trade-Policy-Uncertainty index. A keyless CSV
    with columns ``day,month,year,daily_tpu_index``. PLAN §11 claimed this file
    carries monetary/fiscal/trade/healthcare sub-categories — it does NOT; the
    public ``All_Daily_TPU_Data.csv`` is a single daily TPU index (the
    sub-category breakdown is a different EPU dataset). We store the one series.
    Parsed with the stdlib csv module (small file) like the EIA CSVs.

Same fail-soft contract as every ingestor: either leg can fail without blocking
the other, and a parse error is logged, never raised.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import date

from app.db.duck import DuckStore
from app.ingest.http import HttpClient
from app.ingest.macro import MacroRow, _d
from app.ingest.news import BROWSER_HEADERS

log = logging.getLogger("market.ingest.policyrisk")

GPR_URL = "https://www.matteoiacoviello.com/gpr_files/data_gpr_export.xls"
TPU_URL = "https://www.policyuncertainty.com/media/All_Daily_TPU_Data.csv"

# GPR .xls column -> ts_macro series_id. Headline + Threats + Acts sub-indices.
GPR_COLS = {
    "GPR": "GPR",
    "GPRT": "GPR_THREATS",
    "GPRA": "GPR_ACTS",
}


def _num(v) -> float | None:
    """A cell -> float, or None for blanks/NaN/non-numerics."""
    if v is None or v != v:  # None or NaN
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_gpr(df) -> list[MacroRow]:
    """GPR DataFrame -> monthly headline + Threats + Acts rows (source 'gpr')."""
    import pandas as pd

    if df is None or df.empty or "month" not in df.columns:
        return []
    rows: list[MacroRow] = []
    for _, r in df.iterrows():
        m = r["month"]
        if m is None or (isinstance(m, float) and m != m):
            continue
        ts = pd.Timestamp(m)
        if pd.isna(ts):
            continue
        stamp = _d(ts.date())
        for col, sid in GPR_COLS.items():
            if col not in df.columns:
                continue
            val = _num(r[col])
            if val is not None:
                rows.append(MacroRow(sid, stamp, round(val, 4), "gpr"))
    return rows


def parse_tpu(text: str) -> list[MacroRow]:
    """All_Daily_TPU_Data.csv -> daily TPU index rows (source 'tpu')."""
    rows: list[MacroRow] = []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    for rec in reader:
        try:
            day = date(int(rec["year"]), int(rec["month"]), int(rec["day"]))
        except (KeyError, ValueError, TypeError):
            continue
        val = _num(rec.get("daily_tpu_index"))
        if val is not None:
            rows.append(MacroRow("TPU", _d(day), round(val, 4), "tpu"))
    return rows


class PolicyRiskPipeline:
    """fetch GPR (.xls) + TPU (.csv) -> upsert policy-risk series into ts_macro."""

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

    async def _fetch_gpr(self) -> list[MacroRow]:
        import pandas as pd  # heavy import kept off module load

        try:
            resp = await self._http.get(GPR_URL, headers=BROWSER_HEADERS)
            loop = asyncio.get_running_loop()
            df = await loop.run_in_executor(
                None, lambda: pd.read_excel(io.BytesIO(resp.body), sheet_name=0)
            )
            return await loop.run_in_executor(None, parse_gpr, df)
        except Exception as exc:
            log.warning("gpr fetch/parse failed: %s", exc)
            return []

    async def _fetch_tpu(self) -> list[MacroRow]:
        try:
            text = await self._http.get_text(TPU_URL)
            return parse_tpu(text)
        except Exception as exc:
            log.warning("tpu fetch/parse failed: %s", exc)
            return []

    async def run(self) -> None:
        n_gpr = await self._store(await self._fetch_gpr())
        n_tpu = await self._store(await self._fetch_tpu())
        log.info("policy-risk ingest: %s GPR + %s TPU points", n_gpr, n_tpu)
