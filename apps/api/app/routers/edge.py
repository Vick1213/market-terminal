"""Phase 7 REST surface: /api/alerts, /api/rotation, /api/cot, /api/gex,
/api/insider, /api/calendar, /api/brief (+ /api/brief/run, /api/alerts/test).

All reads come straight from DuckDB snapshots the pipelines maintain; the two
POST endpoints trigger work on demand (regenerate the brief, test ntfy)."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query, Request

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
