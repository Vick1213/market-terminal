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
from app.corr.pipeline import CorrPipeline
from app.edge.alerts import AlertEngine
from app.edge.brief import BriefService
from app.edge.calendar import CalendarPipeline
from app.edge.cot import CotPipeline
from app.edge.gex import GexAdapter
from app.edge.insider import InsiderPipeline
from app.edge.rotation import RotationPipeline
from app.ingest.macro import MacroPipeline
from app.ingest.multiasset import MultiAssetPipeline
from app.ingest.news import NewsPipeline
from app.ingest.retail import RetailPipeline
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
    multiasset_pipeline: MultiAssetPipeline | None = None,
    retail_pipeline: RetailPipeline | None = None,
    corr_pipeline: CorrPipeline | None = None,
    alert_engine: AlertEngine | None = None,
    insider_pipeline: InsiderPipeline | None = None,
    rotation_pipeline: RotationPipeline | None = None,
    cot_pipeline: CotPipeline | None = None,
    gex_adapter: GexAdapter | None = None,
    calendar_pipeline: CalendarPipeline | None = None,
    brief_service: BriefService | None = None,
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

    if multiasset_pipeline is not None:
        soon = datetime.now(timezone.utc)
        # Metals spot is a fast keyless poll; jitter omitted so the ~60s
        # cadence stays even. FINRA/paprika are daily-cadence backfills.
        scheduler.add_job(
            multiasset_pipeline.run_metals,
            trigger="interval",
            seconds=settings.metals_poll_seconds,
            next_run_time=soon + timedelta(seconds=10),
            id="multiasset_metals",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
        common = dict(max_instances=1, coalesce=True, misfire_grace_time=300, jitter=30)
        scheduler.add_job(
            multiasset_pipeline.run_short_vol,
            trigger="interval",
            minutes=settings.flows_poll_minutes,
            next_run_time=soon + timedelta(seconds=290),
            id="multiasset_short_vol",
            **common,
        )
        scheduler.add_job(
            multiasset_pipeline.run_paprika,
            trigger="interval",
            minutes=settings.flows_poll_minutes,
            next_run_time=soon + timedelta(seconds=320),
            id="multiasset_paprika",
            **common,
        )

    if retail_pipeline is not None:
        common = dict(max_instances=1, coalesce=True, misfire_grace_time=300, jitter=30)
        soon = datetime.now(timezone.utc)
        # ApeWisdom snapshots first so the social poll has a spike list; the
        # social text run waits for FinBERT to be warm (news jobs load it).
        scheduler.add_job(
            retail_pipeline.run_apewisdom,
            trigger="interval",
            minutes=settings.apewisdom_poll_minutes,
            next_run_time=soon + timedelta(seconds=65),
            id="retail_apewisdom",
            **common,
        )
        scheduler.add_job(
            retail_pipeline.run_social,
            trigger="interval",
            minutes=settings.retail_social_poll_minutes,
            next_run_time=soon + timedelta(seconds=180),
            id="retail_social",
            **common,
        )
        scheduler.add_job(
            retail_pipeline.run_tradestie,
            trigger="interval",
            minutes=settings.tradestie_poll_minutes,
            next_run_time=soon + timedelta(seconds=210),
            id="retail_tradestie",
            **common,
        )

    if corr_pipeline is not None:
        # First run waits for the FRED ingest (140s) so cards land with both
        # legs; the price-leg ensure inside the run is cache-first either way.
        scheduler.add_job(
            corr_pipeline.run,
            trigger="interval",
            minutes=settings.corr_poll_minutes,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=350),
            id="corr_cookbook",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            jitter=30,
        )

    # --- Phase 7: edge extras + alerting + brief ---
    common = dict(max_instances=1, coalesce=True, misfire_grace_time=300, jitter=30)
    soon = datetime.now(timezone.utc)
    if calendar_pipeline is not None:
        # Cheap (mostly date math) and feeds the brief — runs early.
        scheduler.add_job(
            calendar_pipeline.run,
            trigger="interval",
            minutes=settings.calendar_poll_minutes,
            next_run_time=soon + timedelta(seconds=380),
            id="edge_calendar",
            **common,
        )
    if cot_pipeline is not None:
        scheduler.add_job(
            cot_pipeline.run,
            trigger="interval",
            minutes=settings.cot_poll_minutes,
            next_run_time=soon + timedelta(seconds=410),
            id="edge_cot",
            **common,
        )
    if gex_adapter is not None:
        scheduler.add_job(
            gex_adapter.run,
            trigger="interval",
            minutes=settings.gex_poll_minutes,
            next_run_time=soon + timedelta(seconds=440),
            id="edge_gex",
            **common,
        )
    if rotation_pipeline is not None:
        scheduler.add_job(
            rotation_pipeline.run,
            trigger="interval",
            minutes=settings.rotation_poll_minutes,
            next_run_time=soon + timedelta(seconds=470),
            id="edge_rotation",
            **common,
        )
    if insider_pipeline is not None:
        scheduler.add_job(
            insider_pipeline.run,
            trigger="interval",
            minutes=settings.insider_poll_minutes,
            next_run_time=soon + timedelta(seconds=500),
            id="edge_insider",
            **common,
        )
    if alert_engine is not None:
        # First sweep waits for the first round of ingests so it has data
        # to judge but fires before the user's first coffee refill.
        scheduler.add_job(
            alert_engine.run,
            trigger="interval",
            minutes=settings.alerts_poll_minutes,
            next_run_time=soon + timedelta(seconds=560),
            id="edge_alerts",
            **common,
        )
    if brief_service is not None:
        # Pre-market cron (UTC); /api/brief/run regenerates on demand.
        scheduler.add_job(
            brief_service.run,
            trigger="cron",
            hour=settings.brief_hour_utc,
            minute=settings.brief_minute_utc,
            id="edge_brief",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )

    return scheduler
