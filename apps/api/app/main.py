"""FastAPI application entrypoint.

Wires the whole single-process backend together in the lifespan:
  startup  -> ensure data dirs, open DuckDB + SQLite, init schema, build the
              shared HttpClient, start APScheduler, stash everything on app.state
  shutdown -> stop scheduler, close HTTP client, close DB connections

Run (dev): cd apps/api && uv run uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.corr.pipeline import CorrPipeline
from app.db.duck import DuckStore
from app.db.schema import init_all
from app.db.sqlite import SqliteStore
from app.ingest.crypto import CryptoStreamer
from app.ingest.http import HttpClient
from app.ingest.macro import MacroPipeline
from app.ingest.multiasset import MultiAssetPipeline
from app.ingest.news import NewsPipeline
from app.ingest.retail import RetailPipeline
from app.routers import corr as corr_router
from app.routers import health as health_router
from app.routers import macro as macro_router
from app.routers import markers as markers_router
from app.routers import multiasset as multiasset_router
from app.routers import news as news_router
from app.routers import retail as retail_router
from app.routers import series as series_router
from app.routers import sentiment as sentiment_router
from app.routers import watchlist as watchlist_router
from app.routers import ws as ws_router
from app.scheduler.jobs import build_scheduler
from app.sentiment import SentimentService
from app.ws.hub import hub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("market.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    log.info("starting %s v%s — data dir: %s", settings.app_name, settings.version, settings.data_dir)

    duck = DuckStore(settings.duckdb_path)
    sqlite = SqliteStore(settings.sqlite_path)
    init_all(duck, sqlite)
    log.info("schema ready — duckdb tables: %s", list(duck.table_counts().keys()))

    http = HttpClient(user_agent=settings.user_agent, cache_dir=str(settings.http_cache_dir))

    # FinBERT loads lazily on first score (in its own thread-pool, off the loop).
    sentiment = SentimentService(
        duck, ensemble=settings.sentiment_ensemble, device=settings.sentiment_device
    )
    news_pipeline = NewsPipeline(
        duck,
        sqlite,
        sentiment,
        hub,
        http,
        finnhub_key=settings.finnhub_api_key,
        edgar_lookback_days=settings.edgar_lookback_days,
    )

    macro_pipeline = MacroPipeline(duck, hub, http, fred_api_key=settings.fred_api_key)
    multiasset_pipeline = MultiAssetPipeline(duck, sqlite, http)
    retail_pipeline = RetailPipeline(
        duck,
        sqlite,
        sentiment,
        hub,
        http,
        filters=settings.apewisdom_filters,
        pages=settings.apewisdom_pages,
        spike_top_n=settings.retail_spike_top_n,
        message_retention_days=settings.retail_message_retention_days,
    )

    corr_pipeline = CorrPipeline(duck, hub)

    scheduler = build_scheduler(
        settings, news_pipeline, macro_pipeline, multiasset_pipeline, retail_pipeline,
        corr_pipeline,
    )
    scheduler.start()
    log.info("scheduler started — jobs: %s", [j.id for j in scheduler.get_jobs()])

    # Long-running CCXT Pro streams live outside the scheduler (asyncio tasks).
    crypto_streamer = CryptoStreamer(
        duck,
        hub,
        exchanges=settings.crypto_exchanges,
        symbols=settings.crypto_symbols,
        large_notional=settings.large_print_notional,
        large_z=settings.large_print_z,
        large_floor=settings.large_print_floor,
        retention_days=settings.trades_retention_days,
    )
    crypto_streamer.start()

    app.state.settings = settings
    app.state.duck = duck
    app.state.sqlite = sqlite
    app.state.http = http
    app.state.sentiment = sentiment
    app.state.news_pipeline = news_pipeline
    app.state.macro_pipeline = macro_pipeline
    app.state.multiasset_pipeline = multiasset_pipeline
    app.state.retail_pipeline = retail_pipeline
    app.state.corr_pipeline = corr_pipeline
    app.state.crypto_streamer = crypto_streamer
    app.state.scheduler = scheduler
    app.state.hub = hub

    try:
        yield
    finally:
        log.info("shutting down")
        await crypto_streamer.stop()
        scheduler.shutdown(wait=False)
        sentiment.close()
        await http.aclose()
        duck.close()
        sqlite.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router.router)
    app.include_router(sentiment_router.router)
    app.include_router(news_router.router)
    app.include_router(macro_router.router)
    app.include_router(series_router.router)
    app.include_router(markers_router.router)
    app.include_router(multiasset_router.router)
    app.include_router(retail_router.router)
    app.include_router(corr_router.router)
    app.include_router(watchlist_router.router)
    app.include_router(ws_router.router)

    @app.get("/", tags=["meta"])
    async def root():
        return {
            "app": settings.app_name,
            "version": settings.version,
            "docs": "/docs",
            "health": "/api/health",
            "ws": "/ws/heartbeat",
        }

    return app


app = create_app()
