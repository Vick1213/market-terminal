"""Phase 8 — true daily Fed net liquidity (PLAN: Phase 8 §1).

Two keyless daily sources upgrade the weekly FRED-only proxy:
  * Treasury FiscalData DTS  -> TGA closing balance ($mn, daily)
  * NY Fed markets API       -> ON reverse-repo accepted ($, daily)

Combined with FRED's weekly WALCL (already ingested by MacroPipeline):

    NET_LIQUIDITY [$bn] = WALCL/1000 (ffill) − TGA_CLOSE − RRP_ON

All three legs land in ts_macro ($bn), so the series is chartable, the corr
cookbook's netliq card picks up the daily resolution, and history before the
daily legs exist still comes from the weekly FRED derivation (the composite
and corr engines splice the two — see macro/composite.py, corr/engine.py).

Same contract as the other ingestors: every fetch is allowed to fail without
blocking the rest.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

import pandas as pd

from app.db.duck import DuckStore
from app.ingest.http import HttpClient
from app.ingest.macro import MacroRow, _parse_date

log = logging.getLogger("market.ingest.liquidity")

FISCALDATA_DTS = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
    "/v1/accounting/dts/operating_cash_balance"
)
# Stable label since ~Apr 2022 (verified 2026-06-10); values are $ millions.
TGA_ACCOUNT = "Treasury General Account (TGA) Closing Balance"

NYFED_RRP = "https://markets.newyorkfed.org/api/rp/reverserepo/propositions/search.json"

# WALCL is a weekly (Wed) print — let it carry across one print gap plus
# holidays, never further (a stalled FRED ingest must not fake a level).
WALCL_FFILL_DAYS = 10


def _d(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


async def fetch_tga(http: HttpClient, start_iso: str) -> list[MacroRow]:
    """Daily TGA closing balance -> $bn rows. One page covers years of daily
    prints (4 account rows/day, page cap 10k)."""
    try:
        data = await http.get_json(
            FISCALDATA_DTS,
            params={
                "filter": f"record_date:gte:{start_iso}",
                "fields": "record_date,account_type,open_today_bal",
                "page[size]": "10000",
            },
            conditional=False,
        )
        records = data.get("data", [])
    except Exception as exc:
        log.warning("fiscaldata tga failed: %s", exc)
        return []
    rows: list[MacroRow] = []
    for r in records:
        if r.get("account_type") != TGA_ACCOUNT:
            continue
        day = _parse_date(r.get("record_date", ""))
        if day is None:
            continue
        try:
            mn = float(r.get("open_today_bal"))
        except (TypeError, ValueError):
            continue
        rows.append(MacroRow("TGA_CLOSE", _d(day), mn / 1000.0, "fiscaldata"))
    return rows


async def fetch_rrp(http: HttpClient, start_iso: str) -> list[MacroRow]:
    """Daily ON RRP total accepted -> $bn rows (operation amounts are USD)."""
    try:
        data = await http.get_json(
            NYFED_RRP, params={"startDate": start_iso}, conditional=False
        )
        ops = data.get("repo", {}).get("operations", [])
    except Exception as exc:
        log.warning("nyfed rrp failed: %s", exc)
        return []
    by_day: dict[date, float] = {}
    for op in ops:
        if op.get("operationType") != "Reverse Repo":
            continue
        day = _parse_date(op.get("operationDate", ""))
        amt = op.get("totalAmtAccepted")
        if day is None or amt is None:
            continue
        try:
            by_day[day] = by_day.get(day, 0.0) + float(amt) / 1e9
        except (TypeError, ValueError):
            continue
    return [MacroRow("RRP_ON", _d(day), v, "nyfed") for day, v in by_day.items()]


def compute_net_liquidity(duck: DuckStore) -> int:
    """Recompute NET_LIQUIDITY ($bn) on the daily TGA/RRP spine and store it.
    Blocking (pandas + DuckDB) — callers dispatch via run_in_executor."""
    df = duck.fetchdf(
        "SELECT series_id, ts, value FROM ts_macro "
        "WHERE series_id IN ('WALCL', 'TGA_CLOSE', 'RRP_ON') AND value IS NOT NULL "
        "ORDER BY ts"
    )
    if df is None or df.empty:
        return 0
    df["date"] = pd.to_datetime(df["ts"]).dt.normalize()
    legs = {
        sid: g.groupby("date")["value"].last().sort_index()
        for sid, g in df.groupby("series_id")
    }
    walcl, tga, rrp = legs.get("WALCL"), legs.get("TGA_CLOSE"), legs.get("RRP_ON")
    if walcl is None or tga is None or rrp is None:
        return 0
    idx = tga.index.union(rrp.index)  # daily spine = the daily legs' dates
    net = (
        walcl.reindex(walcl.index.union(idx)).ffill(limit=WALCL_FFILL_DAYS).reindex(idx)
        / 1000.0
        - tga.reindex(idx)
        - rrp.reindex(idx)
    ).dropna()
    if net.empty:
        return 0
    duck.executemany(
        "INSERT OR REPLACE INTO ts_macro (series_id, ts, value, source) "
        "VALUES ('NET_LIQUIDITY', ?, ?, 'computed')",
        [(ts.to_pydatetime(), float(v)) for ts, v in net.items()],
    )
    return len(net)


class LiquidityPipeline:
    """fetch TGA + RRP -> store -> recompute the stored NET_LIQUIDITY series."""

    def __init__(self, duck: DuckStore, http: HttpClient, *, start: str) -> None:
        self._duck = duck
        self._http = http
        self._start = start

    def _series_start(self, series_id: str) -> str:
        row = self._duck.fetchone(
            "SELECT max(ts) FROM ts_macro WHERE series_id = ?", [series_id]
        )
        if row and row[0]:
            return (row[0] - timedelta(days=14)).strftime("%Y-%m-%d")
        return self._start

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
        loop = asyncio.get_running_loop()
        tga_start = await loop.run_in_executor(None, self._series_start, "TGA_CLOSE")
        rrp_start = await loop.run_in_executor(None, self._series_start, "RRP_ON")
        n_tga = await self._store(await fetch_tga(self._http, tga_start))
        n_rrp = await self._store(await fetch_rrp(self._http, rrp_start))
        n_net = await loop.run_in_executor(None, compute_net_liquidity, self._duck)
        log.info(
            "liquidity ingest: %s tga + %s rrp points -> %s net-liquidity days",
            n_tga, n_rrp, n_net,
        )
