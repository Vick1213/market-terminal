"""/api/retail — Panel (b): Retail Market Score.

GET /api/retail            market-wide gauge + spike leaderboard
GET /api/retail/{symbol}   drill-down: mention/rank history, per-source
                           sentiment, scored messages, related headlines

All math lives in app/retail/score.py; this layer only shapes responses
(and runs the blocking DuckDB reads in an executor).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.retail.score import TEXT_SOURCES, compute_gauge, compute_leaderboard

router = APIRouter(prefix="/api", tags=["retail"])

MESSAGES_LIMIT = 40
HEADLINES_LIMIT = 6
HISTORY_DAYS = 14


class RetailGauge(BaseModel):
    score: int | None  # -100..+100 mention-weighted bull/bear
    sentiment: float | None
    chatter_z: float | None
    total_mentions: int | None
    scored_symbols: int
    computed_at: str | None


class RetailLeader(BaseModel):
    symbol: str
    asset_class: str
    mentions: int
    mentions_24h_ago: int | None
    mention_z: float
    rank: int | None
    rank_velocity: int | None
    upvotes: int | None
    sentiment: float | None
    sentiment_sources: list[str]
    sources: int  # cross-source confirmation count (apewisdom + text sources)
    divergence: bool
    spike_score: float


class RetailResponse(BaseModel):
    gauge: RetailGauge
    leaderboard: list[RetailLeader]
    freshness: dict[str, str | None]


class RetailHistoryPoint(BaseModel):
    t: int  # unix seconds
    mentions: int
    rank: int | None


class RetailSourceStat(BaseModel):
    source: str
    ts: str
    mentions: int | None
    sentiment: float | None


class RetailMessage(BaseModel):
    source: str
    ts: str
    text: str
    url: str | None
    score: float | None
    label: str | None
    tag: str | None


class RetailHeadline(BaseModel):
    title: str
    url: str | None
    published: str
    score: float | None
    label: str | None


class RetailSymbolResponse(BaseModel):
    symbol: str
    history: list[RetailHistoryPoint]
    sources: list[RetailSourceStat]
    messages: list[RetailMessage]
    headlines: list[RetailHeadline]


def _epoch(ts: datetime) -> int:
    return int(ts.replace(tzinfo=timezone.utc).timestamp())


def _freshness(duck) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    row = duck.fetchone("SELECT max(ts) FROM ts_retail WHERE source LIKE 'apewisdom:%'")
    out["apewisdom"] = row[0].isoformat() if row and row[0] else None
    for src in TEXT_SOURCES:
        row = duck.fetchone("SELECT max(ts) FROM ts_retail WHERE source = ?", [src])
        out[src] = row[0].isoformat() if row and row[0] else None
    return out


@router.get("/retail", response_model=RetailResponse)
async def retail(request: Request, limit: int = Query(default=30, le=100)) -> RetailResponse:
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    def _snapshot() -> RetailResponse:
        return RetailResponse(
            gauge=RetailGauge(**compute_gauge(duck)),
            leaderboard=[RetailLeader(**r) for r in compute_leaderboard(duck, limit=limit)],
            freshness=_freshness(duck),
        )

    return await loop.run_in_executor(None, _snapshot)


@router.get("/retail/{symbol}", response_model=RetailSymbolResponse)
async def retail_symbol(request: Request, symbol: str) -> RetailSymbolResponse:
    duck = request.app.state.duck
    sym = symbol.upper()
    loop = asyncio.get_running_loop()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).replace(tzinfo=None)

    def _drill() -> RetailSymbolResponse:
        hist_rows = duck.fetchall(
            "SELECT ts, SUM(mentions), MIN(rank) FROM ts_retail "
            "WHERE source LIKE 'apewisdom:%' AND symbol = ? AND ts >= ? "
            "GROUP BY ts ORDER BY ts",
            [sym, cutoff],
        )
        src_rows = duck.fetchall(
            "SELECT source, max(ts) FROM ts_retail WHERE symbol = ? GROUP BY source", [sym]
        )
        sources: list[RetailSourceStat] = []
        for source, ts in src_rows:
            row = duck.fetchone(
                "SELECT mentions, sentiment_score FROM ts_retail "
                "WHERE source = ? AND symbol = ? AND ts = ?",
                [source, sym, ts],
            )
            sources.append(
                RetailSourceStat(
                    source=source, ts=ts.isoformat(),
                    mentions=row[0] if row else None,
                    sentiment=row[1] if row else None,
                )
            )
        msg_rows = duck.fetchall(
            "SELECT source, ts, text, url, score, label, tag FROM retail_messages "
            "WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
            [sym, MESSAGES_LIMIT],
        )
        head_rows = duck.fetchall(
            "SELECT title, url, published, score, label FROM news_items "
            "WHERE symbol = ? ORDER BY published DESC LIMIT ?",
            [sym, HEADLINES_LIMIT],
        )
        return RetailSymbolResponse(
            symbol=sym,
            history=[
                RetailHistoryPoint(t=_epoch(r[0]), mentions=int(r[1] or 0), rank=r[2])
                for r in hist_rows
            ],
            sources=sources,
            messages=[
                RetailMessage(
                    source=r[0], ts=r[1].isoformat(), text=r[2], url=r[3],
                    score=r[4], label=r[5], tag=r[6],
                )
                for r in msg_rows
            ],
            headlines=[
                RetailHeadline(
                    title=r[0], url=r[1], published=r[2].isoformat(), score=r[3], label=r[4]
                )
                for r in head_rows
            ],
        )

    return await loop.run_in_executor(None, _drill)
