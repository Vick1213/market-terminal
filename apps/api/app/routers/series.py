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
    "RRPONTSYD": "Reverse repo (FRED, weekly-ish)",
    "WTREGEN": "Treasury TGA (FRED, weekly)",
    "WRESBAL": "Bank reserves",
    "NET_LIQUIDITY": "Fed net liquidity ($bn, daily)",
    "TGA_CLOSE": "Treasury TGA ($bn, daily)",
    "RRP_ON": "ON reverse repo ($bn, daily)",
    "NAAIM_EXPOSURE": "NAAIM exposure",
    "AAII_BULL": "AAII bulls %",
    "AAII_BEAR": "AAII bears %",
    "PC_TOTAL": "Put/Call total",
    "PC_EQUITY": "Put/Call equity",
    "PC_INDEX": "Put/Call index",
    "FINRA_SHORT_RATIO": "Short-sale volume %",
    "RETAIL_SENT": "Retail sentiment (-1..+1)",
    "RETAIL_CHATTER_Z": "Retail chatter z",
    "RETAIL_MENTIONS": "Retail mentions (total)",
    "CORR_GOLD_BTC": "Corr 90d: Gold↔BTC",
    "CORR_COPPERGOLD_10Y": "Corr 90d: Copper/Gold↔10y",
    "CORR_BTC_NDX": "Corr 90d: BTC↔Nasdaq",
    "CORR_USDJPY_RISK": "Corr 90d: USDJPY↔Nasdaq",
    "CORR_HY_SPX": "Corr 90d: HY OAS↔SPX",
    "CORR_CURVE_2S10S": "2s10s level (cookbook)",
    "CORR_GOLD_SILVER": "Gold/Silver ratio",
    "CORR_NETLIQ_BTC": "Corr 26w: Net liq↔BTC",
    "CORR_REAL_GOLD": "Corr 90d: Real yields↔Gold",
    "CORR_USD_COMMOD": "Corr 90d: DXY↔Gold",
    "CORR_VIX_TERM": "VIX3M/VIX ratio",
    "CORR_OIL_BREAKEVEN": "Corr 90d: Oil↔Breakevens",
    "BREADTH_PCT_ABOVE_200DMA": "Breadth: % sectors > 200DMA",
    "CN_CLI": "China leading indicator (OECD)",
    "G20_CLI": "G20 leading indicator (OECD)",
    "EA_BCI": "Euro-area business confidence",
    "CORR_CN_CLI_COPPER": "Corr 26w: China CLI↔Copper",
    "CORR_EA_BCI_EURUSD": "Corr 26w: EA conf↔EURUSD",
    "CORR_G20_CLI_BTC": "Corr 26w: G20 CLI↔BTC",
}

RETAIL_CATALOG_LIMIT = 30  # top movers only — the long tail isn't chartworthy


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
        latest = duck.fetchone(
            "SELECT max(ts) FROM ts_retail WHERE source LIKE 'apewisdom:%'"
        )
        retail_syms = (
            [
                r[0]
                for r in duck.fetchall(
                    "SELECT symbol FROM ts_retail WHERE source LIKE 'apewisdom:%' "
                    "AND ts = ? GROUP BY symbol ORDER BY SUM(mentions) DESC LIMIT ?",
                    [latest[0], RETAIL_CATALOG_LIMIT],
                )
            ]
            if latest and latest[0]
            else []
        )
        return macro_ids, sent_syms, retail_syms, _watchlist(sqlite)

    macro_ids, sent_syms, retail_syms, watchlist = await loop.run_in_executor(None, _q)

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
                key="retail",
                label="Retail mentions",
                items=[
                    CatalogItem(id=f"RETAIL:{s}", label=f"{s} mentions") for s in retail_syms
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
                    point = SeriesPoint(t=_epoch(ts), v=sum(window) / len(window))
                    # Charts need strictly ascending times; items scored in the
                    # same second collapse to the latest rolling value.
                    if points and points[-1].t == point.t:
                        points[-1] = point
                    else:
                        points.append(point)
                label = "News sentiment (all)" if sym == "ALL" else f"News sentiment {sym}"
                out.append(SeriesData(id=sid, label=label, group="sentiment", points=points))
            elif sid.startswith("RETAIL:"):
                sym = sid.split(":", 1)[1]
                rows = duck.fetchall(
                    "SELECT ts, SUM(mentions) FROM ts_retail "
                    "WHERE source LIKE 'apewisdom:%' AND symbol = ? AND ts >= ? "
                    "GROUP BY ts ORDER BY ts",
                    [sym, cutoff],
                )
                out.append(
                    SeriesData(
                        id=sid, label=f"{sym} mentions", group="retail",
                        points=[SeriesPoint(t=_epoch(r[0]), v=float(r[1] or 0)) for r in rows],
                    )
                )
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


# --- intraday 1-minute bars (for the day-trader trade chart) ------------------
# The daily PRICE: series is too coarse for a fast-loop trade. This serves the
# same batched 1-min bars the day trader itself uses (Alpaca data API), so the
# expandable TradeChart can render the actual intraday timeframe with the trade's
# entry / take-profit / stop-loss levels drawn against real candles.
class IntradayBar(BaseModel):
    t: int  # unix seconds (UTC)
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


class IntradayResponse(BaseModel):
    symbol: str
    bars: list[IntradayBar]


def _parse_ts(ts) -> int:
    """Alpaca bar ts -> unix seconds. Accepts ISO8601 ('...Z') or epoch."""
    if isinstance(ts, (int, float)):
        return int(ts)
    try:
        s = str(ts).replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp())
    except (ValueError, TypeError):
        return 0


@router.get("/series/intraday", response_model=IntradayResponse)
async def series_intraday(
    request: Request,
    symbol: str = Query(description="ticker, e.g. NVDA or ETH/USD"),
    minutes: int = Query(default=180, le=1440, description="lookback window in minutes"),
) -> IntradayResponse:
    settings = request.app.state.settings
    http = request.app.state.http
    key, secret = settings.alpaca_key_id, settings.alpaca_secret_key
    if not key or not secret:
        return IntradayResponse(symbol=symbol, bars=[])
    asset_class = "crypto" if "/USD" in symbol.upper() or symbol.upper().endswith("USD") else "equity"
    try:
        from app.ingest.alpaca import fetch_intraday_bars
        data = await fetch_intraday_bars(http, key, secret, [(symbol, asset_class)], minutes=minutes)
    except Exception:
        return IntradayResponse(symbol=symbol, bars=[])
    # fetch_intraday_bars keys by the Alpaca symbol; tolerate slash/no-slash.
    bars = (data.get(symbol) or data.get(symbol.replace("/", ""))
            or (next(iter(data.values())) if len(data) == 1 else []))
    out = [
        IntradayBar(t=_parse_ts(b.get("ts")), o=float(b["o"]), h=float(b["h"]),
                    l=float(b["l"]), c=float(b["c"]), v=float(b.get("v") or 0.0))
        for b in bars if b.get("c") is not None
    ]
    return IntradayResponse(symbol=symbol, bars=out)
