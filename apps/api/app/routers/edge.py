"""Phase 7 REST surface: /api/alerts, /api/rotation, /api/cot, /api/gex,
/api/insider, /api/calendar, /api/brief (+ /api/brief/run, /api/alerts/test).

All reads come straight from DuckDB snapshots the pipelines maintain; the two
POST endpoints trigger work on demand (regenerate the brief, test ntfy)."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query, Request

from app.ingest.cftc_tff import CONTRACTS as _TFF_CONTRACTS

router = APIRouter(prefix="/api", tags=["edge"])


@router.get("/alerts")
async def alerts(request: Request, limit: int = Query(50, le=200)) -> dict:
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(
        None, duck.fetchall,
        "SELECT id, ts, rule, severity, title, body, symbol, value, regime, pushed "
        "FROM alerts ORDER BY ts DESC LIMIT ?",
        [limit],
    )
    return {
        "ntfy_enabled": request.app.state.ntfy.enabled,
        "alerts": [
            {
                "id": r[0], "ts": str(r[1]), "rule": r[2], "severity": r[3],
                "title": r[4], "body": r[5], "symbol": r[6],
                "value": float(r[7]) if r[7] is not None else None,
                "regime": r[8], "pushed": bool(r[9]),
            }
            for r in rows
        ],
    }


@router.post("/alerts/test")
async def alerts_test(request: Request) -> dict:
    """Manual ntfy round-trip so topic setup is verifiable from the UI."""
    ntfy = request.app.state.ntfy
    if not ntfy.enabled:
        return {"ok": False, "detail": "set MARKET_NTFY_TOPIC to enable push"}
    ok = await ntfy.publish(
        "Market terminal test", "ntfy push is wired up correctly.", "info", tags="tada"
    )
    return {"ok": ok}


@router.get("/rotation")
async def rotation(request: Request) -> dict:
    from app.edge.rotation import compute_rrg, compute_seasonality

    duck = request.app.state.duck
    pipeline = request.app.state.rotation_pipeline
    loop = asyncio.get_running_loop()
    rrg = await loop.run_in_executor(
        None, compute_rrg, duck, pipeline.sectors, pipeline.benchmark
    )
    seasonality = await loop.run_in_executor(
        None, compute_seasonality, duck, pipeline.watchlist_symbols()
    )
    return {**rrg, "seasonality": seasonality}


@router.get("/cot")
async def cot(request: Request) -> dict:
    from app.edge.cot import cot_summary

    loop = asyncio.get_running_loop()
    markets = await loop.run_in_executor(None, cot_summary, request.app.state.duck)
    return {"markets": markets}


@router.get("/short-interest")
async def short_interest(request: Request) -> dict:
    from app.edge.short_interest import short_interest_summary

    loop = asyncio.get_running_loop()
    symbols = await loop.run_in_executor(
        None, short_interest_summary, request.app.state.duck
    )
    return {"symbols": symbols}


@router.get("/gex")
async def gex(request: Request) -> dict:
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(
        None, duck.fetchall,
        "SELECT symbol, ts, spot, total_gex, flip, call_wall, put_wall, detail "
        "FROM gex_snapshots gs WHERE ts = "
        "(SELECT max(ts) FROM gex_snapshots WHERE symbol = gs.symbol)",
        None,
    )
    out = []
    for r in rows:
        detail = json.loads(r[7]) if r[7] else {}
        out.append({
            "symbol": r[0], "ts": str(r[1]),
            "spot": float(r[2]) if r[2] is not None else None,
            "total_gex_bn": round(float(r[3]) / 1e9, 2) if r[3] is not None else None,
            "flip": float(r[4]) if r[4] is not None else None,
            "call_wall": float(r[5]) if r[5] is not None else None,
            "put_wall": float(r[6]) if r[6] is not None else None,
            "profile": detail.get("profile", []),
            "contracts": detail.get("contracts"),
        })
    return {"snapshots": out}


@router.get("/insider")
async def insider(request: Request, days: int = Query(30, le=120)) -> dict:
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()
    trades = await loop.run_in_executor(
        None, duck.fetchall,
        "SELECT accession, symbol, issuer_name, insider_name, insider_title, "
        "is_ceo_cfo, filed_at, trade_date, shares, price, value, url "
        "FROM insider_trades WHERE filed_at >= current_date - INTERVAL (?) DAY "
        "ORDER BY filed_at DESC LIMIT 100",
        [days],
    )
    clusters = await loop.run_in_executor(
        None, duck.fetchall,
        "SELECT symbol, count(DISTINCT insider_name), sum(value), max(filed_at), "
        "max(CASE WHEN is_ceo_cfo THEN 1 ELSE 0 END) "
        "FROM insider_trades WHERE filed_at >= current_date - INTERVAL (?) DAY "
        "GROUP BY symbol ORDER BY sum(value) DESC",
        [days],
    )
    return {
        "trades": [
            {
                "accession": t[0], "symbol": t[1], "issuer": t[2],
                "insider": t[3], "title": t[4], "is_ceo_cfo": bool(t[5]),
                "filed_at": str(t[6])[:10],
                "trade_date": str(t[7])[:10] if t[7] else None,
                "shares": float(t[8]) if t[8] is not None else None,
                "price": float(t[9]) if t[9] is not None else None,
                "value": float(t[10]) if t[10] is not None else None,
                "url": t[11],
            }
            for t in trades
        ],
        "clusters": [
            {
                "symbol": c[0], "buyers": int(c[1]),
                "total_value": float(c[2]) if c[2] is not None else None,
                "last_filed": str(c[3])[:10], "has_ceo_cfo": bool(c[4]),
            }
            for c in clusters
        ],
    }


@router.get("/congress")
async def congress(request: Request, days: int = Query(90, le=365), limit: int = Query(60, le=200)) -> dict:
    """Recent Senate PTR stock trades (electronic filings), newest first.
    Watchlist tickers are flagged so the panel can highlight overlap."""
    duck = request.app.state.duck
    sqlite = request.app.state.sqlite
    loop = asyncio.get_running_loop()

    def _q():
        trades = duck.fetchall(
            "SELECT ptr_id, row_no, senator, filed_at, tx_date, ticker, asset, "
            "asset_type, side, amount_min, amount_max, url "
            "FROM congress_trades WHERE filed_at >= current_date - INTERVAL (?) DAY "
            "ORDER BY filed_at DESC, ptr_id, row_no LIMIT ?",
            [days, limit],
        )
        watch = {
            r[0].upper() for r in sqlite.fetchall("SELECT symbol FROM watchlist")
        }
        return trades, watch

    trades, watch = await loop.run_in_executor(None, _q)
    return {
        "trades": [
            {
                "ptr_id": t[0], "row": t[1], "senator": t[2],
                "filed_at": str(t[3])[:10],
                "tx_date": str(t[4])[:10] if t[4] else None,
                "ticker": t[5], "asset": t[6], "asset_type": t[7], "side": t[8],
                "amount_min": float(t[9]) if t[9] is not None else None,
                "amount_max": float(t[10]) if t[10] is not None else None,
                "url": t[11],
                "on_watchlist": bool(t[5] and t[5].upper() in watch),
            }
            for t in trades
        ],
    }


@router.get("/whales")
async def whales(request: Request) -> dict:
    """13F whale funds: latest filing, top holdings, QoQ changes."""
    from app.edge.whales import whales_summary

    loop = asyncio.get_running_loop()
    funds = await loop.run_in_executor(None, whales_summary, request.app.state.duck)
    return {"funds": funds}


@router.post("/congress/run")
async def congress_run(request: Request) -> dict:
    """Trigger the Senate PTR ingest now instead of waiting for the
    scheduled sweep (first run ~10 min after boot, then every 12h)."""
    stored = await request.app.state.congress_pipeline.run()
    return {"stored_transactions": stored}


@router.post("/whales/run")
async def whales_run(request: Request) -> dict:
    """Trigger the 13F whale ingest now (scheduled run is daily)."""
    stored = await request.app.state.whales_pipeline.run()
    return {"stored_filings": stored}


@router.get("/treasury/auctions")
async def treasury_auctions(
    request: Request, limit: int = Query(40, le=200)
) -> dict:
    """Recent Treasury auction demand (Phase 16 §11 rank 7). dealer_pct high =
    real money stayed away (yield-spike risk); low bid_to_cover = weak demand."""
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    def _q():
        return duck.fetchall(
            "SELECT auction_date, security_type, security_term, bid_to_cover, "
            "high_yield, offering_amt, total_accepted, dealer_pct, indirect_pct, "
            "direct_pct FROM treasury_auctions ORDER BY auction_date DESC, cusip "
            "LIMIT ?",
            [limit],
        )

    rows = await loop.run_in_executor(None, _q)
    auctions = [
        {
            "auction_date": str(r[0])[:10],
            "security_type": r[1],
            "security_term": r[2],
            "bid_to_cover": r[3],
            "high_yield": r[4],
            "offering_amt": r[5],
            "total_accepted": r[6],
            "dealer_pct": r[7],
            "indirect_pct": r[8],
            "direct_pct": r[9],
        }
        for r in rows
    ]
    return {"auctions": auctions}


@router.post("/treasury/auctions/run")
async def treasury_auctions_run(request: Request) -> dict:
    """Trigger the Treasury auction ingest now (scheduled run is 12h)."""
    await request.app.state.treasury_pipeline.run()
    return {"ok": True}


@router.get("/kalshi")
async def kalshi(request: Request) -> dict:
    """Kalshi FOMC/CPI distributions (Phase 16 §11 rank 8). Per tracked series:
    the nearest-closing event's strike ladder differenced into a per-bucket
    implied probability distribution, plus the modal bucket."""
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    def _q():
        series = [
            r[0]
            for r in duck.fetchall(
                "SELECT DISTINCT series_ticker FROM kalshi_markets ORDER BY series_ticker"
            )
        ]
        out = []
        for st in series:
            ev = duck.fetchone(
                "SELECT event_ticker, min(close_time) FROM kalshi_markets "
                "WHERE series_ticker = ? GROUP BY event_ticker ORDER BY 2 LIMIT 1",
                [st],
            )
            if not ev:
                continue
            ladder = duck.fetchall(
                "SELECT floor_strike, yes_sub_title, yes_prob, close_time FROM "
                "kalshi_markets WHERE series_ticker = ? AND event_ticker = ? "
                "AND yes_prob IS NOT NULL ORDER BY floor_strike",
                [st, ev[0]],
            )
            # Difference the cumulative "Above X" ladder into per-bucket mass.
            buckets = []
            for i, row in enumerate(ladder):
                lo, sub, p, _ = row
                nxt = ladder[i + 1][2] if i + 1 < len(ladder) else 0.0
                mass = max(0.0, (p or 0.0) - (nxt or 0.0))
                buckets.append(
                    {"floor_strike": lo, "label": sub, "cum_prob": p, "bucket_prob": round(mass, 4)}
                )
            modal = max(buckets, key=lambda b: b["bucket_prob"], default=None) if buckets else None
            out.append(
                {
                    "series_ticker": st,
                    "event_ticker": ev[0],
                    "close_time": str(ev[1])[:10] if ev[1] else None,
                    "strikes": buckets,
                    "modal": modal,
                }
            )
        # The derived FOMC expected-rate scalar (charted via ts_macro).
        fr = duck.fetchone(
            "SELECT ts, value FROM ts_macro WHERE series_id = 'KALSHI_FOMC_RATE' "
            "AND value IS NOT NULL ORDER BY ts DESC LIMIT 1"
        )
        return out, (
            {"ts": str(fr[0])[:10], "expected_rate": fr[1]} if fr else None
        )

    events, expected = await loop.run_in_executor(None, _q)
    return {"events": events, "fomc_expected_rate": expected}


@router.post("/kalshi/run")
async def kalshi_run(request: Request) -> dict:
    """Trigger the Kalshi ingest now (scheduled run is every few hours)."""
    pipe = request.app.state.kalshi_pipeline
    if pipe is None:
        return {"ok": False, "reason": "kalshi disabled"}
    await pipe.run()
    return {"ok": True}


# ICI series -> short JSON keys, grouped by panel (Phase 16 §11 rank 5).
_ICI_FLOW_COLS = {
    "ICI_FLOW_TOTAL": "total",
    "ICI_FLOW_EQUITY": "equity",
    "ICI_FLOW_BOND": "bond",
    "ICI_FLOW_HYBRID": "hybrid",
    "ICI_FLOW_COMMODITY": "commodity",
}
_ICI_MMF_COLS = {
    "ICI_MMF_TOTAL": "total",
    "ICI_MMF_GOVT": "govt",
    "ICI_MMF_TREASURY": "treasury",
    "ICI_MMF_PRIME": "prime",
    "ICI_MMF_TAXEXEMPT": "taxexempt",
}


@router.get("/ici")
async def ici(request: Request, weeks: int = Query(26, le=260)) -> dict:
    """ICI weekly fund flows + money-market fund assets (Phase 16 §11 rank 5),
    pivoted to one row per week. flows = estimated weekly net flows by asset
    class ($mn); mmf = money-market total net assets by category ($mn).
    Persistent equity outflows + bond inflows = risk-off rotation in real
    dollars; a prime->government MMF rotation is an early stress tell."""
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    def _pivot(col_map: dict[str, str]) -> list[dict]:
        rows = duck.fetchall(
            "SELECT ts, series_id, value FROM ts_macro WHERE series_id IN "
            f"({','.join('?' for _ in col_map)}) AND value IS NOT NULL ORDER BY ts",
            list(col_map),
        )
        by_date: dict[str, dict] = {}
        for ts, sid, val in rows:
            d = str(ts)[:10]
            by_date.setdefault(d, {"date": d})[col_map[sid]] = val
        return sorted(by_date.values(), key=lambda r: r["date"])[-weeks:]

    def _q():
        return _pivot(_ICI_FLOW_COLS), _pivot(_ICI_MMF_COLS)

    flows, mmf = await loop.run_in_executor(None, _q)
    return {"flows": flows, "mmf": mmf}


@router.post("/ici/run")
async def ici_run(request: Request) -> dict:
    """Trigger the ICI ingest now (scheduled run is 12h)."""
    await request.app.state.ici_pipeline.run()
    return {"ok": True}


_BIS_AREAS = {"US": "Fed", "JP": "BoJ", "XM": "ECB", "CN": "PBoC"}


@router.get("/bis/cbta")
async def bis_cbta(request: Request, quarters: int = Query(28, le=80)) -> dict:
    """BIS global central-bank balance sheets (Phase 16 §11 rank 14), one row
    per quarter. assets_usd = total assets per CB ($bn, USD-converted) plus the
    GLOBAL sum; gdp_ratio = assets as a ratio to GDP. Pair the GLOBAL sum with
    FRED WALCL for a global-vs-Fed liquidity divergence."""
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    usd_ids = {f"BIS_CB_ASSETS_{a}": a for a in _BIS_AREAS}
    usd_ids["BIS_CB_ASSETS_GLOBAL"] = "GLOBAL"
    gdp_ids = {f"BIS_CB_GDP_{a}": a for a in _BIS_AREAS}

    def _pivot(id_map: dict[str, str]) -> list[dict]:
        rows = duck.fetchall(
            "SELECT ts, series_id, value FROM ts_macro WHERE series_id IN "
            f"({','.join('?' for _ in id_map)}) AND value IS NOT NULL ORDER BY ts",
            list(id_map),
        )
        by_date: dict[str, dict] = {}
        for ts, sid, val in rows:
            d = str(ts)[:10]
            by_date.setdefault(d, {"date": d})[id_map[sid]] = val
        return sorted(by_date.values(), key=lambda r: r["date"])[-quarters:]

    def _q():
        return _pivot(usd_ids), _pivot(gdp_ids)

    assets_usd, gdp_ratio = await loop.run_in_executor(None, _q)
    return {"assets_usd": assets_usd, "gdp_ratio": gdp_ratio, "areas": _BIS_AREAS}


@router.post("/bis/cbta/run")
async def bis_cbta_run(request: Request) -> dict:
    """Trigger the BIS CBTA ingest now (scheduled run is daily)."""
    await request.app.state.bis_pipeline.run()
    return {"ok": True}


# EIA series -> short JSON keys, grouped by sub-panel (Phase 16 §11 rank 10).
_EIA_STOCKS = {
    "EIA_CRUDE_COMMERCIAL": "crude_commercial",
    "EIA_CRUDE_COMMERCIAL_CHG": "crude_commercial_chg",
    "EIA_CRUDE_SPR": "crude_spr",
    "EIA_GASOLINE_STOCKS": "gasoline",
    "EIA_DISTILLATE_STOCKS": "distillate",
}
_EIA_SPOT = {
    "EIA_WTI": "wti",
    "EIA_BRENT": "brent",
    "EIA_RBOB": "rbob",
    "EIA_HEATING_OIL": "heating_oil",
    "EIA_CRACK_321": "crack_321",
}
_EIA_NATGAS = {
    "EIA_NATGAS_STORAGE": "storage",
    "EIA_NATGAS_CHANGE": "change",
    "EIA_NATGAS_VS_5YR_PCT": "vs_5yr_pct",
    "EIA_NATGAS_VS_YRAGO_PCT": "vs_yrago_pct",
}
# Cleveland Fed inflation nowcast — nowcast + realized "actual" per metric.
_CLEV_MOM = {
    "CLEV_CPI_MOM": "cpi", "CLEV_CPI_MOM_ACTUAL": "cpi_actual",
    "CLEV_CORECPI_MOM": "core_cpi", "CLEV_CORECPI_MOM_ACTUAL": "core_cpi_actual",
    "CLEV_PCE_MOM": "pce", "CLEV_PCE_MOM_ACTUAL": "pce_actual",
    "CLEV_COREPCE_MOM": "core_pce", "CLEV_COREPCE_MOM_ACTUAL": "core_pce_actual",
}
_CLEV_YOY = {
    "CLEV_CPI_YOY": "cpi", "CLEV_CPI_YOY_ACTUAL": "cpi_actual",
    "CLEV_CORECPI_YOY": "core_cpi", "CLEV_CORECPI_YOY_ACTUAL": "core_cpi_actual",
    "CLEV_PCE_YOY": "pce", "CLEV_PCE_YOY_ACTUAL": "pce_actual",
    "CLEV_COREPCE_YOY": "core_pce", "CLEV_COREPCE_YOY_ACTUAL": "core_pce_actual",
}
# Policy-risk surface (Phase 16 §11 rank 11). GPR = monthly geopolitical-risk
# headline + Threats / Acts sub-indices; TPU = daily trade-policy uncertainty.
_GPR_COLS = {"GPR": "gpr", "GPR_THREATS": "threats", "GPR_ACTS": "acts"}
_TPU_COLS = {"TPU": "tpu"}


@router.get("/eia/energy")
async def eia_energy(request: Request, limit: int = Query(52, le=520)) -> dict:
    """EIA weekly energy fundamentals (Phase 16 §11 rank 10). stocks = weekly
    crude/product inventory levels + build/draw ($mmbbl); spot = daily WTI /
    Brent / RBOB / heating-oil prices + the 3:2:1 crack spread ($/bbl);
    natgas = working-gas storage (Bcf) with weekly change and surplus/deficit
    vs the 5-year average. Crude builds vs draws and the crack spread are the
    primary catalysts for energy names."""
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    def _pivot(col_map: dict[str, str], n: int) -> list[dict]:
        rows = duck.fetchall(
            "SELECT ts, series_id, value FROM ts_macro WHERE series_id IN "
            f"({','.join('?' for _ in col_map)}) AND value IS NOT NULL ORDER BY ts",
            list(col_map),
        )
        by_date: dict[str, dict] = {}
        for ts, sid, val in rows:
            d = str(ts)[:10]
            by_date.setdefault(d, {"date": d})[col_map[sid]] = val
        return sorted(by_date.values(), key=lambda r: r["date"])[-n:]

    def _q():
        # spot is daily, so allow ~5x the weekly window to cover `limit` weeks.
        return _pivot(_EIA_STOCKS, limit), _pivot(_EIA_SPOT, limit * 5), _pivot(
            _EIA_NATGAS, limit
        )

    stocks, spot, natgas = await loop.run_in_executor(None, _q)
    return {"stocks": stocks, "spot": spot, "natgas": natgas}


@router.post("/eia/energy/run")
async def eia_energy_run(request: Request) -> dict:
    """Trigger the EIA energy ingest now (scheduled run is 12h)."""
    await request.app.state.eia_pipeline.run()
    return {"ok": True}


@router.get("/cftc/tff")
async def cftc_tff(request: Request) -> dict:
    """CFTC Traders-in-Financial-Futures positioning (Phase 16 §11 rank 6).

    Per macro contract: latest Asset-Manager / Leveraged-Money / Dealer net
    positions, the AM-vs-LM divergence (the signal the legacy commercial /
    non-commercial split hides — real money vs hedge funds on opposite sides),
    its 3-year percentile index, and the 13-week change. Sorted by absolute
    divergence index extremity so the most-stretched contracts surface first."""
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    def _q() -> list[dict]:
        rows = duck.fetchall(
            "SELECT series_id, ts, value FROM ts_macro "
            "WHERE series_id LIKE 'TFF\\_%' ESCAPE '\\' AND value IS NOT NULL "
            "ORDER BY ts",
            [],
        )
        # ticker -> field -> list[(ts, value)] in date order
        hist: dict[str, dict[str, list]] = {}
        for sid, ts, val in rows:
            # TFF_<TICKER>_<FIELD>  where FIELD is AM_NET / LM_NET / DEALER_NET
            body = sid[len("TFF_"):]
            ticker, _, field = body.partition("_")
            hist.setdefault(ticker, {}).setdefault(field, []).append((ts, val))

        out: list[dict] = []
        for ticker in _TFF_CONTRACTS.values():
            series = hist.get(ticker)
            if not series or "AM_NET" not in series or "LM_NET" not in series:
                continue
            am = {str(t)[:10]: v for t, v in series["AM_NET"]}
            lm = {str(t)[:10]: v for t, v in series["LM_NET"]}
            de = {str(t)[:10]: v for t, v in series.get("DEALER_NET", [])}
            # divergence per common date, in date order
            dates = sorted(d for d in am if d in lm)
            if not dates:
                continue
            div = [am[d] - lm[d] for d in dates]
            last_d = dates[-1]
            last_div = div[-1]
            lo, hi = min(div), max(div)
            idx = 100 * (last_div - lo) / (hi - lo) if hi != lo else None
            div_13w = div[-14] if len(div) >= 14 else None
            out.append({
                "ticker": ticker,
                "report_date": last_d,
                "am_net": am[last_d],
                "lm_net": lm[last_d],
                "dealer_net": de.get(last_d),
                "am_lm_div": last_div,
                "div_index": round(idx, 1) if idx is not None else None,
                "div_13w_ago": div_13w,
                "weeks": len(div),
            })
        # surface the most stretched (furthest from the neutral 50) first
        out.sort(
            key=lambda r: abs((r["div_index"] or 50) - 50), reverse=True
        )
        return out

    markets = await loop.run_in_executor(None, _q)
    return {"markets": markets}


@router.post("/cftc/tff/run")
async def cftc_tff_run(request: Request) -> dict:
    """Trigger the CFTC TFF ingest now (scheduled run is 12h)."""
    await request.app.state.cftc_tff_pipeline.run()
    return {"ok": True}


@router.get("/cleveland/nowcast")
async def cleveland_nowcast(request: Request, limit: int = Query(120, le=730)) -> dict:
    """Cleveland Fed daily inflation nowcast (Phase 16 §11 rank 9). mom / yoy
    each carry the latest nowcast and realized "actual" for CPI, core CPI, PCE
    and core PCE, indexed by the nowcast's as-of date so the day-over-day drift
    (and the nowcast-vs-actual gap) is the pre-CPI / pre-PCE surprise signal."""
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    def _pivot(col_map: dict[str, str], n: int) -> list[dict]:
        rows = duck.fetchall(
            "SELECT ts, series_id, value FROM ts_macro WHERE series_id IN "
            f"({','.join('?' for _ in col_map)}) AND value IS NOT NULL ORDER BY ts",
            list(col_map),
        )
        by_date: dict[str, dict] = {}
        for ts, sid, val in rows:
            d = str(ts)[:10]
            by_date.setdefault(d, {"date": d})[col_map[sid]] = val
        return sorted(by_date.values(), key=lambda r: r["date"])[-n:]

    def _q():
        return _pivot(_CLEV_MOM, limit), _pivot(_CLEV_YOY, limit)

    mom, yoy = await loop.run_in_executor(None, _q)
    return {"mom": mom, "yoy": yoy}


@router.post("/cleveland/nowcast/run")
async def cleveland_nowcast_run(request: Request) -> dict:
    """Trigger the Cleveland nowcast ingest now (scheduled run is 6h)."""
    await request.app.state.cleveland_pipeline.run()
    return {"ok": True}


@router.get("/policy-risk")
async def policy_risk(request: Request, limit: int = Query(120, le=2000)) -> dict:
    """Policy-risk surface (Phase 16 §11 rank 11). gpr = monthly geopolitical-
    risk headline + Threats / Acts sub-indices (Threats lead Acts by 1-3 months);
    tpu = daily trade-policy-uncertainty index. Returned as separate series since
    their cadences differ (monthly vs daily)."""
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    def _pivot(col_map: dict[str, str], n: int) -> list[dict]:
        rows = duck.fetchall(
            "SELECT ts, series_id, value FROM ts_macro WHERE series_id IN "
            f"({','.join('?' for _ in col_map)}) AND value IS NOT NULL ORDER BY ts",
            list(col_map),
        )
        by_date: dict[str, dict] = {}
        for ts, sid, val in rows:
            d = str(ts)[:10]
            by_date.setdefault(d, {"date": d})[col_map[sid]] = val
        return sorted(by_date.values(), key=lambda r: r["date"])[-n:]

    def _q():
        # tpu is daily, so allow a wider window than the monthly gpr series.
        return _pivot(_GPR_COLS, limit), _pivot(_TPU_COLS, limit * 21)

    gpr, tpu = await loop.run_in_executor(None, _q)
    return {"gpr": gpr, "tpu": tpu}


@router.post("/policy-risk/run")
async def policy_risk_run(request: Request) -> dict:
    """Trigger the GPR + TPU ingest now (scheduled run is 12h)."""
    await request.app.state.policyrisk_pipeline.run()
    return {"ok": True}


@router.get("/fed/speeches")
async def fed_speeches(request: Request, limit: int = Query(30, le=200)) -> dict:
    """Fed speeches scored hawk/dove (Phase 16 §11 rank 11). speeches = recent
    speeches with per-speech FinBERT sentiment + the hawk/dove proxy (>0 hawkish,
    <0 dovish); index = the latest rolling FED_HAWK_DOVE value from ts_macro.

    Hawk/dove is a FinBERT-sentiment proxy (restrictive tone reads negative), a
    directional tell — not a calibrated rate forecast."""
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    def _q():
        speeches = duck.fetchall(
            "SELECT url, speech_date, speaker, title, score, hawk_dove, "
            "confidence, chunks FROM fed_speeches ORDER BY speech_date DESC LIMIT ?",
            [limit],
        )
        idx = duck.fetchone(
            "SELECT ts, value FROM ts_macro WHERE series_id = 'FED_HAWK_DOVE' "
            "ORDER BY ts DESC LIMIT 1"
        )
        return speeches, idx

    rows, idx = await loop.run_in_executor(None, _q)
    speeches = [
        {
            "url": r[0],
            "date": str(r[1])[:10],
            "speaker": r[2],
            "title": r[3],
            "score": r[4],
            "hawk_dove": r[5],
            "confidence": r[6],
            "chunks": r[7],
        }
        for r in rows
    ]
    index = {"date": str(idx[0])[:10], "value": idx[1]} if idx else None
    return {"speeches": speeches, "index": index}


@router.post("/fed/speeches/run")
async def fed_speeches_run(request: Request) -> dict:
    """Trigger the Fed speech fetch + hawk/dove scoring now (scheduled run is 12h)."""
    await request.app.state.fed_speeches_pipeline.run()
    return {"ok": True}


@router.get("/edgar/8k")
async def edgar_8k(request: Request, limit: int = Query(60, le=300)) -> dict:
    """8-K item-code catalysts (Phase 16 §11 rank 12) — material cybersecurity
    incidents (1.05), impairments (2.06), executive departures (5.02) and buyback
    programs (8.01), newest first."""
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    def _q():
        return duck.fetchall(
            "SELECT accession, ticker, company, item_code, catalyst, items, "
            "filed_at, url FROM edgar_8k_events ORDER BY filed_at DESC, accession DESC "
            "LIMIT ?",
            [limit],
        )

    rows = await loop.run_in_executor(None, _q)
    return {
        "events": [
            {
                "accession": r[0],
                "ticker": r[1],
                "company": r[2],
                "item_code": r[3],
                "catalyst": r[4],
                "items": (r[5] or "").split(",") if r[5] else [],
                "filed_at": str(r[6])[:10],
                "url": r[7],
            }
            for r in rows
        ]
    }


@router.post("/edgar/8k/run")
async def edgar_8k_run(request: Request) -> dict:
    """Trigger the 8-K item-code scan now (scheduled run is every few hours)."""
    n = await request.app.state.edgar_8k_pipeline.run()
    return {"ok": True, "stored": n}


@router.get("/edgar/13d")
async def edgar_13d(request: Request, limit: int = Query(60, le=300)) -> dict:
    """SC 13D / 13G beneficial-ownership stakes (Phase 16 §11 rank 13). 13D =
    activist (intent to influence); 13G = passive. pct_owned / shares / purpose
    come from the Dec-2024 structured XML and are null for older HTML-only
    filings (the filing is still listed)."""
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    def _q():
        return duck.fetchall(
            "SELECT accession, subject_ticker, subject_name, filer_name, "
            "filing_type, is_activist, pct_owned, shares, purpose, filed_at, url "
            "FROM edgar_13d_filings ORDER BY filed_at DESC, accession DESC LIMIT ?",
            [limit],
        )

    rows = await loop.run_in_executor(None, _q)
    return {
        "filings": [
            {
                "accession": r[0],
                "subject_ticker": r[1],
                "subject_name": r[2],
                "filer_name": r[3],
                "filing_type": r[4],
                "is_activist": r[5],
                "pct_owned": r[6],
                "shares": r[7],
                "purpose": r[8],
                "filed_at": str(r[9])[:10],
                "url": r[10],
            }
            for r in rows
        ]
    }


@router.post("/edgar/13d/run")
async def edgar_13d_run(request: Request) -> dict:
    """Trigger the SC 13D/13G scan now (scheduled run is every few hours)."""
    n = await request.app.state.edgar_13d_pipeline.run()
    return {"ok": True, "stored": n}


@router.get("/calendar")
async def calendar(request: Request, days: int = Query(30, le=120)) -> dict:
    from app.edge.calendar import upcoming_events

    loop = asyncio.get_running_loop()
    events = await loop.run_in_executor(
        None, upcoming_events, request.app.state.duck, days
    )
    return {"events": events}


@router.get("/strategist")
async def strategist(request: Request) -> dict:
    """Latest stored strategist snapshot (the daily job fills it ~12 min
    after boot; POST /api/strategist/run regenerates on demand)."""
    from app.edge.strategist import latest_snapshot

    loop = asyncio.get_running_loop()
    snap = await loop.run_in_executor(None, latest_snapshot, request.app.state.duck)
    if snap is None:
        return {"status": "warming-up"}
    return snap


@router.post("/strategist/run")
async def strategist_run(request: Request) -> dict:
    return await request.app.state.strategist_service.run()


@router.get("/filings/diff")
async def filings_diff(request: Request, limit: int = Query(50, le=200)) -> dict:
    """Lazy Prices feed: latest 10-K/10-Q risk-factor diffs per tracked
    ticker (low similarity = the language changed = historical red flag)."""
    from app.edge.filings_diff import latest_diffs

    loop = asyncio.get_running_loop()
    diffs = await loop.run_in_executor(
        None, latest_diffs, request.app.state.duck, limit
    )
    return {"diffs": diffs}


@router.post("/filings/diff/run")
async def filings_diff_run(request: Request) -> dict:
    return {"stored": await request.app.state.filings_diff_pipeline.run()}


@router.get("/strategist/report")
async def strategist_report(request: Request) -> dict:
    """Report card: forward-return scoring of stored snapshots, the regime
    backtest, and per-signal hit rates — all from stored data."""
    from app.edge.report_card import compute_report_card
    from app.edge.strategist import BASE_ALLOCATION

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, compute_report_card, request.app.state.duck, BASE_ALLOCATION
    )


@router.get("/brief")
async def brief(request: Request) -> dict:
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()
    row = await loop.run_in_executor(
        None, duck.fetchone,
        "SELECT ts, regime, text, model, detail FROM briefs ORDER BY ts DESC LIMIT 1",
        None,
    )
    if row is None:
        return {"date": None, "regime": None, "text": None, "model": None}
    return {
        "date": str(row[0])[:10], "regime": row[1], "text": row[2],
        "model": row[3], "digest": json.loads(row[4]) if row[4] else {},
    }


@router.post("/brief/run")
async def brief_run(request: Request) -> dict:
    result = await request.app.state.brief_service.run(push=False)
    return {k: v for k, v in result.items() if k != "digest"}


# --- Phase 15: narrative-vs-money divergence + Polymarket front-running ---


@router.get("/divergence")
async def divergence(request: Request, history_days: int = Query(60, le=180)) -> dict:
    """Latest divergence snapshot + score history + the realised SPY weekend
    gaps the score is meant to anticipate (the forward-outcome track record)."""
    from app.edge.divergence import divergence_history, latest_divergence, weekend_gaps

    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    def _q():
        return (
            latest_divergence(duck),
            divergence_history(duck, history_days),
            weekend_gaps(duck, 10),
        )

    latest, history, gaps = await loop.run_in_executor(None, _q)
    if latest is None:
        return {"status": "warming-up", "history": [], "weekend_gaps": gaps}
    return {"latest": latest, "history": history, "weekend_gaps": gaps}


@router.post("/divergence/run")
async def divergence_run(request: Request) -> dict:
    pipeline = request.app.state.divergence_pipeline
    if pipeline is None:
        return {"status": "disabled"}
    return await pipeline.run()


@router.get("/polymarket")
async def polymarket(request: Request) -> dict:
    """Tracked Polymarket geopolitical markets: latest odds, the move vs ~24h
    ago, escalation sign and holder concentration — biggest tells first."""
    from app.ingest.polymarket import polymarket_summary

    loop = asyncio.get_running_loop()
    markets = await loop.run_in_executor(
        None, polymarket_summary, request.app.state.duck
    )
    return {"markets": markets}


@router.post("/polymarket/run")
async def polymarket_run(request: Request) -> dict:
    pipeline = request.app.state.polymarket_pipeline
    if pipeline is None:
        return {"status": "disabled"}
    return {"tracked": await pipeline.run()}
