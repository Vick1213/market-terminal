"""/api/multiasset — Panel (e): live crypto + honest delayed proxies.

One snapshot endpoint. The crypto leg reads the CryptoStreamer's in-memory
state (quotes, depth, stream health) plus the persisted large-print tape; the
metals leg joins gold-api spot against the cached futures daily close for a
spot-vs-futures basis; the equity leg computes the PLAN §3e "accumulation
score" = blend of unusual-volume z and FINRA short-vol-ratio trend, with the
components exposed so the blend is transparent. Every leg carries an explicit
freshness label — LIVE crypto vs delayed/EOD proxies is a feature, not a bug.
"""

from __future__ import annotations

import asyncio
import math
from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api", tags=["multiasset"])

PRINTS_LIMIT = 60
VOLUME_Z_WINDOW = 60  # trailing daily bars behind the unusual-volume z


class CryptoQuote(BaseModel):
    exchange: str
    symbol: str
    price: float | None
    ts: str | None
    trades_seen: int
    connected: bool


class DepthSnapshot(BaseModel):
    exchange: str
    symbol: str
    ts: str
    imbalance: float  # (bid-ask)/(bid+ask) over top levels, -1..+1
    bid_depth: float
    ask_depth: float
    mid: float
    spread_bp: float


class TradePrint(BaseModel):
    exchange: str
    symbol: str
    ts: str
    price: float
    size: float
    side: str | None
    notional: float


class CryptoSection(BaseModel):
    live: bool
    quotes: list[CryptoQuote]
    books: list[DepthSnapshot]
    prints: list[TradePrint]
    btc_dominance: float | None = None
    volume_24h_usd: float | None = None


class MetalRow(BaseModel):
    symbol: str  # XAU | XAG
    spot: float | None = None
    spot_ts: str | None = None
    futures_close: float | None = None
    futures_ts: str | None = None
    basis: float | None = None  # futures - spot
    basis_pct: float | None = None
    etf_symbol: str | None = None  # GLD | SLV flow proxy
    etf_volume_z: float | None = None


class EquityFlow(BaseModel):
    symbol: str
    volume_z: float | None = None  # latest daily volume vs trailing 60d
    short_ratio: float | None = None  # latest short_vol/total_vol
    short_trend_z: float | None = None  # 5d avg ratio vs 20d baseline
    accumulation: float | None = None  # blend, + = accumulation
    ts: str | None = None


class MultiAssetResponse(BaseModel):
    crypto: CryptoSection
    metals: list[MetalRow]
    equities: list[EquityFlow]
    freshness: dict[str, str]


def _latest_macro(duck, series_id: str) -> float | None:
    row = duck.fetchone(
        "SELECT value FROM ts_macro WHERE series_id = ? ORDER BY ts DESC LIMIT 1", [series_id]
    )
    return row[0] if row else None


def _volume_z(duck, symbol: str) -> float | None:
    vols = [
        r[0]
        for r in duck.fetchall(
            "SELECT volume FROM ts_price WHERE source = 'yahoo' AND symbol = ? "
            "AND volume IS NOT NULL AND volume > 0 ORDER BY ts DESC LIMIT ?",
            [symbol, VOLUME_Z_WINDOW + 1],
        )
    ]
    if len(vols) < 20:
        return None
    latest, rest = vols[0], vols[1:]
    mean = sum(rest) / len(rest)
    var = sum((v - mean) ** 2 for v in rest) / len(rest)
    std = math.sqrt(var)
    if std <= 0:
        return None
    return max(-3.0, min(3.0, (latest - mean) / std))


def _metal_row(duck, spot_sym: str, etf_sym: str) -> MetalRow:
    m = MetalRow(symbol=spot_sym, etf_symbol=etf_sym)
    spot = duck.fetchone(
        "SELECT close, ts FROM ts_price WHERE source = 'gold-api' AND symbol = ? "
        "ORDER BY ts DESC LIMIT 1",
        [spot_sym],
    )
    if spot:
        m.spot, m.spot_ts = spot[0], spot[1].isoformat()
    fut = duck.fetchone(
        "SELECT close, ts FROM ts_price WHERE source = 'yahoo' AND symbol = ? "
        "AND close IS NOT NULL ORDER BY ts DESC LIMIT 1",
        [spot_sym],  # watchlist XAU/XAG cache GC=F / SI=F daily bars
    )
    if fut:
        m.futures_close, m.futures_ts = fut[0], fut[1].isoformat()
    if m.spot and m.futures_close:
        m.basis = m.futures_close - m.spot
        m.basis_pct = m.basis / m.spot * 100
    m.etf_volume_z = _volume_z(duck, etf_sym)
    return m


def _equity_flow(duck, symbol: str) -> EquityFlow:
    f = EquityFlow(symbol=symbol)
    f.volume_z = _volume_z(duck, symbol)

    rows = duck.fetchall(
        "SELECT ts, short_volume, total_volume FROM ts_short_vol "
        "WHERE symbol = ? AND total_volume > 0 ORDER BY ts DESC LIMIT 20",
        [symbol],
    )
    ratios = [r[1] / r[2] for r in rows]
    if rows:
        f.ts = rows[0][0].isoformat()
        f.short_ratio = ratios[0]
    if len(ratios) >= 10:
        recent = sum(ratios[:5]) / 5
        base = sum(ratios) / len(ratios)
        var = sum((x - base) ** 2 for x in ratios) / len(ratios)
        std = math.sqrt(var)
        if std > 0:
            f.short_trend_z = max(-3.0, min(3.0, (recent - base) / std))

    # Accumulation: unusual volume confirms, a RISING short-sell share detracts.
    comps = [c for c in (f.volume_z, -f.short_trend_z if f.short_trend_z is not None else None)
             if c is not None]
    if comps:
        f.accumulation = sum(comps) / len(comps)
    return f


def _prints(duck) -> list[TradePrint]:
    rows = duck.fetchall(
        "SELECT source, symbol, ts, price, size, side, notional FROM trades "
        "ORDER BY ts DESC LIMIT ?",
        [PRINTS_LIMIT],
    )
    return [
        TradePrint(
            exchange=r[0], symbol=r[1], ts=r[2].isoformat(), price=r[3],
            size=r[4], side=r[5] or None, notional=r[6],
        )
        for r in rows
    ]


@router.get("/multiasset", response_model=MultiAssetResponse)
async def multiasset(request: Request) -> MultiAssetResponse:
    duck = request.app.state.duck
    sqlite = request.app.state.sqlite
    streamer = getattr(request.app.state, "crypto_streamer", None)
    loop = asyncio.get_running_loop()

    def _snapshot() -> MultiAssetResponse:
        crypto = CryptoSection(
            live=streamer.is_live() if streamer else False,
            quotes=[
                CryptoQuote(**{**q, "ts": q["ts"].isoformat() if q["ts"] else None})
                for q in (streamer.quotes() if streamer else [])
            ],
            books=[DepthSnapshot(**b) for b in (streamer.books() if streamer else [])],
            prints=_prints(duck),
            btc_dominance=_latest_macro(duck, "BTC_DOMINANCE"),
            volume_24h_usd=_latest_macro(duck, "CRYPTO_VOL24H"),
        )
        metals = [_metal_row(duck, "XAU", "GLD"), _metal_row(duck, "XAG", "SLV")]
        equities = [
            _equity_flow(duck, r["symbol"])
            for r in sqlite.fetchall(
                "SELECT symbol FROM watchlist WHERE asset_class = 'equity' ORDER BY sort_order"
            )
        ]
        return MultiAssetResponse(
            crypto=crypto,
            metals=metals,
            equities=equities,
            freshness={
                "crypto": "LIVE (public WS)",
                "metals": "spot ~60s · futures EOD",
                "equities": "EOD volume · FINRA T+1",
            },
        )

    return await loop.run_in_executor(None, _snapshot)
