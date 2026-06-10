"""GET /api/series — one endpoint behind every chart.

Any panel can plot any combination of:
  * macro series   — plain ts_macro ids (VIXCLS, COMPOSITE_RISK, NET_LIQUIDITY…)
  * price series   — "PRICE:<symbol>" daily closes (Stooq on demand, cached)
  * sentiment      — "SENT:<symbol>" / "SENT:ALL" rolling FinBERT mean over
                     scored news items (window 7, PLAN §3a velocity tracking)

/api/series/catalog lists what's currently plottable so the chart's
"add series" picker can be populated without hardcoding panel knowledge.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.ingest.prices import ensure_daily_history

router = APIRouter(prefix="/api", tags=["series"])

SENT_WINDOW = 7  # rolling-mean window (items), matches the news detail view

# Friendly labels for the macro ids worth charting; anything else in ts_macro
# still appears in the catalog under its raw id.
MACRO_LABELS: dict[str, str] = {
    "COMPOSITE_RISK": "Composite Risk-On/Off",
    "VIXCLS": "VIX (FRED)",
    "VIX": "VIX (CBOE)",
    "VIX3M": "VIX3M",
    "BAMLH0A0HYM2": "HY OAS",
    "BAMLC0A0CM": "IG OAS",
    "NFCI": "Chicago Fed NFCI",
    "ANFCI": "Adjusted NFCI",
    "T10Y2Y": "2s10s spread",
    "T10Y3M": "3m10y spread",
    "DGS10": "10y yield",
    "DGS2": "2y yield",
    "DFII10": "10y real yield",
    "T10YIE": "10y breakeven",
    "DTWEXBGS": "Broad USD",
    "DCOILWTICO": "WTI crude",
    "M2SL": "M2",
    "DFF": "Fed funds",
    "SOFR": "SOFR",
    "WALCL": "Fed balance sheet",
    "RRPONTSYD": "Reverse repo",
    "WTREGEN": "Treasury TGA",
    "WRESBAL": "Bank reserves",
    "NAAIM_EXPOSURE": "NAAIM exposure",
    "AAII_BULL": "AAII bulls %",
    "AAII_BEAR": "AAII bears %",
    "PC_TOTAL": "Put/Call total",
    "PC_EQUITY": "Put/Call equity",
    "PC_INDEX": "Put/Call index",
    "FINRA_SHORT_RATIO": "Short-sale volume %",
}


class SeriesPoint(BaseModel):
    t: int  # unix seconds (UTC)
    v: float


class SeriesData(BaseModel):
    id: str
    label: str
    group: str  # macro | price | sentiment
    points: list[SeriesPoint]


class SeriesResponse(BaseModel):
    series: list[SeriesData]


class CatalogItem(BaseModel):
    id: str
    label: str


class CatalogGroup(BaseModel):
    key: str
    label: str
    items: list[CatalogItem]


class CatalogResponse(BaseModel):
    groups: list[CatalogGroup]


def _epoch(ts: datetime) -> int:
    return int(ts.replace(tzinfo=timezone.utc).timestamp())


def _watchlist(sqlite) -> list[tuple[str, str]]:
    return [
        (r["symbol"], r["asset_class"])
        for r in sqlite.fetchall("SELECT symbol, asset_class FROM watchlist ORDER BY sort_order")
    ]


@router.get("/series/catalog", response_model=CatalogResponse)
async def catalog(request: Request) -> CatalogResponse:
    duck = request.app.state.duck
    sqlite = request.app.state.sqlite
    loop = asyncio.get_running_loop()

    def _q():
        macro_ids = [
            r[0] for r in duck.fetchall("SELECT DISTINCT series_id FROM ts_macro ORDER BY series_id")
        ]
        sent_syms = [
            r[0]
            for r in duck.fetchall(
                "SELECT DISTINCT symbol FROM news_items "
                "WHERE symbol IS NOT NULL AND score IS NOT NULL ORDER BY symbol"
            )
        ]
        return macro_ids, sent_syms, _watchlist(sqlite)

    macro_ids, sent_syms, watchlist = await loop.run_in_executor(None, _q)

    return CatalogResponse(
        groups=[
            CatalogGroup(
                key="sentiment",
                label="Sentiment",
                items=[CatalogItem(id="SENT:ALL", label="News sentiment (all)")]
                + [CatalogItem(id=f"SENT:{s}", label=f"News sentiment {s}") for s in sent_syms],
            ),
            CatalogGroup(
                key="price",
                label="Price",
                items=[
                    CatalogItem(id=f"PRICE:{s}", label=f"{s} close") for s, _ in watchlist
                ],
            ),
            CatalogGroup(
                key="macro",
                label="Macro / Liquidity",
                items=[
                    CatalogItem(id=i, label=MACRO_LABELS.get(i, i))
                    # COMPOSITE first, then the labelled ones, then the rest.
                    for i in sorted(macro_ids, key=lambda x: (x != "COMPOSITE_RISK", x not in MACRO_LABELS, x))
                ],
            ),
        ]
    )


@router.get("/series", response_model=SeriesResponse)
async def series(
    request: Request,
    ids: str = Query(description="comma-separated series ids"),
    days: int = Query(default=365, le=5000),
) -> SeriesResponse:
    duck = request.app.state.duck
    sqlite = request.app.state.sqlite
    http = request.app.state.http
    loop = asyncio.get_running_loop()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)

    wanted = [s.strip() for s in ids.split(",") if s.strip()][:12]

    # Price series may need an on-demand Stooq fetch (cached in ts_price).
    asset_class = dict(await loop.run_in_executor(None, _watchlist, sqlite))
    for sid in wanted:
        if sid.startswith("PRICE:"):
            sym = sid.split(":", 1)[1]
            await ensure_daily_history(http, duck, sym, asset_class.get(sym, "equity"))

    def _q() -> list[SeriesData]:
        out: list[SeriesData] = []
        for sid in wanted:
            if sid.startswith("PRICE:"):
                sym = sid.split(":", 1)[1]
                rows = duck.fetchall(
                    "SELECT ts, close FROM ts_price WHERE source = 'yahoo' AND symbol = ? "
                    "AND ts >= ? AND close IS NOT NULL ORDER BY ts",
                    [sym, cutoff],
                )
                out.append(
                    SeriesData(
                        id=sid, label=f"{sym} close", group="price",
                        points=[SeriesPoint(t=_epoch(r[0]), v=r[1]) for r in rows],
                    )
                )
            elif sid.startswith("SENT:"):
                sym = sid.split(":", 1)[1]
                where, params = "", []
                if sym != "ALL":
                    where = "AND symbol = ?"
                    params.append(sym)
                rows = duck.fetchall(
                    f"SELECT published, score FROM news_items "
                    f"WHERE score IS NOT NULL AND published >= ? {where} ORDER BY published",
                    [cutoff, *params],
                )
                points: list[SeriesPoint] = []
                vals: list[float] = []
                for ts, score in rows:
                    vals.append(score)
                    window = vals[-SENT_WINDOW:]
                    points.append(SeriesPoint(t=_epoch(ts), v=sum(window) / len(window)))
                label = "News sentiment (all)" if sym == "ALL" else f"News sentiment {sym}"
                out.append(SeriesData(id=sid, label=label, group="sentiment", points=points))
            else:
                rows = duck.fetchall(
                    "SELECT ts, value FROM ts_macro WHERE series_id = ? AND ts >= ? "
                    "AND value IS NOT NULL ORDER BY ts",
                    [sid, cutoff],
                )
                out.append(
                    SeriesData(
                        id=sid, label=MACRO_LABELS.get(sid, sid), group="macro",
                        points=[SeriesPoint(t=_epoch(r[0]), v=r[1]) for r in rows],
                    )
                )
        return out

    return SeriesResponse(series=await loop.run_in_executor(None, _q))
