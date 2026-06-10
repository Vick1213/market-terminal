"""Scheduler wiring.

An ``AsyncIOScheduler`` runs inside the FastAPI event loop (started/stopped in
the app lifespan). Phase 0 registered the heartbeat; Phase 1 adds the news
ingestors (Yahoo per-ticker RSS + EDGAR submissions + optional Finnhub), each
staggered and isolated so one fragile source never blocks the others.

Jobs use ``max_instances=1`` + ``misfire_grace_time`` + jitter so a slow run
never stacks up, and blocking work is offloaded to an executor by the job body.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import Settings
from app.ingest.macro import MacroPipeline
from app.ingest.news import NewsPipeline
from app.ws.hub import hub

log = logging.getLogger("market.scheduler")

# Bumped each tick; exposed in the heartbeat payload so the frontend can show
# a monotonically increasing counter and detect dropped connections.
_tick = {"n": 0}


async def _heartbeat() -> None:
    _tick["n"] += 1
    payload = {
        "type": "heartbeat",
        "n": _tick["n"],
        "ts": datetime.now(timezone.utc).isoformat(),
        "ws_clients": hub.client_count(),
    }
    await hub.broadcast("heartbeat", payload)
    log.debug("heartbeat %s -> %s clients", _tick["n"], payload["ws_clients"])


def build_scheduler(
    settings: Settings,
    news_pipeline: NewsPipeline | None = None,
    macro_pipeline: MacroPipeline | None = None,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        _heartbeat,
        trigger="interval",
        seconds=settings.heartbeat_seconds,
        id="heartbeat",
        max_instances=1,
        misfire_grace_time=10,
        coalesce=True,
        jitter=1,
    )

    if news_pipeline is not None:
        common = dict(max_instances=1, coalesce=True, misfire_grace_time=120, jitter=30)
        # First runs staggered shortly after boot so the panel fills without
        # waiting a full interval (and so the FinBERT lazy-load happens early,
        # in its thread-pool, off the event loop).
        soon = datetime.now(timezone.utc)
        scheduler.add_job(
            news_pipeline.run_yahoo,
            trigger="interval",
            minutes=settings.news_poll_minutes,
            next_run_time=soon + timedelta(seconds=5),
            id="news_yahoo",
            **common,
        )
        scheduler.add_job(
            news_pipeline.run_edgar,
            trigger="interval",
            minutes=settings.edgar_poll_minutes,
            next_run_time=soon + timedelta(seconds=20),
            id="news_edgar",
            **common,
        )
        if settings.finnhub_api_key:
            scheduler.add_job(
                news_pipeline.run_finnhub,
                trigger="interval",
                minutes=settings.news_poll_minutes,
                next_run_time=soon + timedelta(seconds=35),
                id="news_finnhub",
                **common,
            )
        else:
            log.info("finnhub ingestor disabled (no MARKET_FINNHUB_API_KEY)")
        scheduler.add_job(
            news_pipeline.run_broad_rss,
            trigger="interval",
            minutes=settings.broad_rss_poll_minutes,
            next_run_time=soon + timedelta(seconds=50),
            id="news_broad_rss",
            **common,
        )
        scheduler.add_job(
            news_pipeline.run_gdelt,
            trigger="interval",
            minutes=settings.gdelt_poll_minutes,
            next_run_time=soon + timedelta(seconds=80),
            id="news_gdelt",
            **common,
        )
        scheduler.add_job(
            news_pipeline.run_seekingalpha,
            trigger="interval",
            minutes=settings.news_poll_minutes,
            next_run_time=soon + timedelta(seconds=110),
            id="news_seekingalpha",
            **common,
        )

    if macro_pipeline is not None:
        common = dict(max_instances=1, coalesce=True, misfire_grace_time=300, jitter=30)
        soon = datetime.now(timezone.utc)
        # Daily-cadence sources, staggered after the news ingestors. FRED runs
        # first since the composite leans on it hardest.
        if settings.fred_api_key:
            scheduler.add_job(
                macro_pipeline.run_fred,
                trigger="interval",
                minutes=settings.fred_poll_minutes,
                next_run_time=soon + timedelta(seconds=140),
                id="macro_fred",
                **common,
            )
        else:
            log.info("fred ingestor disabled (no MARKET_FRED_API_KEY — free at "
                     "fred.stlouisfed.org/docs/api/api_key.html)")
        scheduler.add_job(
            macro_pipeline.run_cboe,
            trigger="interval",
            minutes=settings.cboe_poll_minutes,
            next_run_time=soon + timedelta(seconds=170),
            id="macro_cboe",
            **common,
        )
        scheduler.add_job(
            macro_pipeline.run_finra,
            trigger="interval",
            minutes=settings.finra_poll_minutes,
            next_run_time=soon + timedelta(seconds=200),
            id="macro_finra",
            **common,
        )
        scheduler.add_job(
            macro_pipeline.run_naaim,
            trigger="interval",
            minutes=settings.naaim_poll_minutes,
            next_run_time=soon + timedelta(seconds=230),
            id="macro_naaim",
            **common,
        )
        scheduler.add_job(
            macro_pipeline.run_aaii,
            trigger="interval",
            minutes=settings.aaii_poll_minutes,
            next_run_time=soon + timedelta(seconds=260),
            id="macro_aaii",
            **common,
        )
        # Safety-net recompute (each ingest already recomputes on success).
        scheduler.add_job(
            macro_pipeline.recompute_composite,
            trigger="interval",
            minutes=settings.composite_poll_minutes,
            id="macro_composite",
            **common,
        )

    return scheduler
