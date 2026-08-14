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
from app.edge.alerts import AlertEngine
from app.edge.brief import BriefService
from app.edge.calendar import CalendarPipeline
from app.edge.congress import CongressPipeline
from app.edge.cot import CotPipeline
from app.edge.divergence import DivergencePipeline
from app.edge.short_interest import ShortInterestPipeline
from app.edge.gex import GexAdapter
from app.edge.filings_diff import FilingsDiffPipeline
from app.edge.fomc_diff import FomcDiffPipeline
from app.edge.insider import InsiderPipeline
from app.edge.insider_scan import InsiderScanPipeline
from app.edge.llm import LlmClient
from app.edge.ntfy import NtfyPublisher
from app.edge.rotation import RotationPipeline
from app.edge.strategist import StrategistService
from app.edge.whales import WhalesPipeline
from app.forecast import KronosForecastService
from app.ingest.crypto import CryptoStreamer
from app.ingest.http import HttpClient
from app.ingest.intl import IntlPipeline
from app.ingest.liquidity import LiquidityPipeline
from app.ingest.treasury import TreasuryAuctionPipeline
from app.ingest.kalshi import KalshiPipeline
from app.ingest.ici import ICIPipeline
from app.ingest.bis import BISPipeline
from app.ingest.cftc_tff import CftcTffPipeline
from app.ingest.cleveland import ClevelandPipeline
from app.ingest.policyrisk import PolicyRiskPipeline
from app.ingest.fed_speeches import FedSpeechPipeline
from app.ingest.edgar_events import Edgar8KPipeline, Edgar13DPipeline
from app.ingest.eia import EIAPipeline
from app.ingest.macro import MacroPipeline
from app.ingest.multiasset import MultiAssetPipeline
from app.ingest.news import NewsPipeline
from app.ingest.polymarket import PolymarketPipeline
from app.ingest.retail import RetailPipeline
from app.ingest.source_health import SourceHealthRegistry
from app.profile import apply_profile_gates
from app.routers import corr as corr_router
from app.routers import edge as edge_router
from app.routers import forecast as forecast_router
from app.routers import health as health_router
from app.routers import macro as macro_router
from app.routers import markers as markers_router
from app.routers import multiasset as multiasset_router
from app.routers import news as news_router
from app.routers import retail as retail_router
from app.routers import series as series_router
from app.routers import sentiment as sentiment_router
from app.routers import settings as settings_router
from app.routers import trading as trading_router
from app.routers import watchlist as watchlist_router
from app.routers import ws as ws_router
from app.scheduler.jobs import build_scheduler
from app.sentiment import SentimentService
from app.settings_store import apply_overlay
from app.trading.bot import TradingBotService
from app.trading.broker import AlpacaPaperBroker
from app.trading.broker_cache import BrokerState
from app.trading.ibkr import IbkrBroker
from app.trading.daytrader import DayTraderService
from app.trading.guardrails import GuardrailConfig
from app.trading.optimizer import PortfolioOptimizer
from app.ws.hub import hub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("market.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # M3: apply the BYO settings store (apps/api/data/settings.json) on top of
    # env/.env BEFORE any pipeline/broker/LLM client is constructed below, so
    # a key saved through the settings UI is live from the very first request.
    # No-op if the store file doesn't exist yet (pure env/.env behavior).
    apply_overlay(settings)
    settings.ensure_dirs()
    log.info("starting %s v%s — data dir: %s", settings.app_name, settings.version, settings.data_dir)

    duck = DuckStore(settings.duckdb_path)
    sqlite = SqliteStore(settings.sqlite_path)
    init_all(duck, sqlite)
    log.info("schema ready — duckdb tables: %s", list(duck.table_counts().keys()))

    source_health = SourceHealthRegistry(sqlite)
    # M2.5: MARKET_PROFILE=commercial pre-flags every RED source in
    # source_health as "disabled (profile)" (+ Tiingo as "needs key" if
    # unset) so /api/health/sources explains empty panels immediately. No-op
    # in the default "personal" profile.
    apply_profile_gates(settings, source_health)
    http = HttpClient(
        user_agent=settings.user_agent,
        cache_dir=str(settings.http_cache_dir),
        health=source_health,
    )

    # FinBERT loads lazily on first score (in its own thread-pool, off the loop).
    sentiment = SentimentService(
        duck, ensemble=settings.sentiment_ensemble, device=settings.sentiment_device
    )
    # Kronos loads lazily on the first /api/forecast call (own thread-pool).
    forecast_service = KronosForecastService(
        duck,
        model_id=settings.forecast_model_id,
        tokenizer_id=settings.forecast_tokenizer_id,
        device=settings.forecast_device,
        max_context=settings.forecast_max_context,
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

    # Phase 7: edge extras + alerting + brief.
    ntfy = NtfyPublisher(settings.ntfy_server, settings.ntfy_topic)
    if not ntfy.enabled:
        log.info("ntfy push disabled (no MARKET_NTFY_TOPIC — alerts stay in-app)")
    alert_engine = AlertEngine(
        duck, sqlite, hub, ntfy,
        cooldown_hours=settings.alert_cooldown_hours,
        large_print_notional=settings.large_print_notional,
        insider_cluster_min_buyers=settings.insider_cluster_min_buyers,
        insider_cluster_window_days=settings.insider_cluster_window_days,
        netliq_drain_bn=settings.netliq_drain_alert_bn,
        filing_similarity_alert=settings.filings_diff_alert_similarity,
        squeeze_days_to_cover=settings.squeeze_days_to_cover,
        divergence_warn=settings.divergence_warn_score,
        divergence_critical=settings.divergence_critical_score,
        polymarket_jump=settings.polymarket_jump_alert,
    )
    insider_pipeline = InsiderPipeline(
        duck, sqlite, http,
        lookback_days=settings.insider_lookback_days,
        min_trade_value=settings.insider_min_trade_value,
    )
    insider_scan_pipeline = (
        InsiderScanPipeline(
            duck, sqlite, http,
            max_filings_per_sweep=settings.insider_scan_max_filings,
            min_trade_value=settings.insider_scan_min_value,
        )
        if settings.insider_scan_enabled else None
    )
    filings_diff_pipeline = FilingsDiffPipeline(duck, sqlite, http)
    fomc_diff_pipeline = FomcDiffPipeline(duck, http)
    rotation_pipeline = RotationPipeline(
        duck, sqlite, settings.sector_etfs, settings.rrg_benchmark
    )
    cot_pipeline = CotPipeline(duck, http)
    short_interest_pipeline = ShortInterestPipeline(duck, sqlite, http)
    gex_adapter = GexAdapter(duck, http, settings.gex_symbols)
    calendar_pipeline = CalendarPipeline(
        duck, sqlite, http,
        fred_api_key=settings.fred_api_key,
        fmp_api_key=settings.fmp_api_key,
    )
    # Phase 9: one LLM client behind both narratives (Ollama by default;
    # OpenAI/DeepSeek/Anthropic via MARKET_LLM_PROVIDER + MARKET_LLM_API_KEY).
    llm = LlmClient(
        provider=settings.llm_provider,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        ollama_url=settings.ollama_url,
        ollama_model=settings.ollama_model,
        timeout=settings.llm_timeout_seconds,
    )
    log.info("llm narrative provider: %s (template fallback always on)", llm.label)
    brief_service = BriefService(duck, sqlite, hub, ntfy, llm)

    # Phase 8: true net liquidity + smart money 2.0.
    liquidity_pipeline = LiquidityPipeline(duck, http, start=settings.liquidity_start)
    congress_pipeline = CongressPipeline(
        duck, sqlite, http, lookback_days=settings.congress_lookback_days
    )
    whales_pipeline = WhalesPipeline(
        duck, http, settings.whale_funds, top_holdings=settings.whale_top_holdings
    )

    # Phase 16 §11 rank 7: Treasury auction demand (bid-to-cover + takedown).
    treasury_pipeline = TreasuryAuctionPipeline(duck, http)
    # Phase 16 §11 rank 8: Kalshi FOMC/CPI prediction-market distribution.
    kalshi_pipeline = KalshiPipeline(duck, http) if settings.kalshi_enabled else None
    # Phase 16 §11 rank 5: ICI weekly fund flows + money-market fund assets.
    ici_pipeline = ICIPipeline(duck, http)
    # Phase 16 §11 rank 14: BIS global central-bank balance sheets.
    bis_pipeline = BISPipeline(duck, http)
    # Phase 16 §11 rank 10: EIA weekly energy fundamentals (WPSR + nat-gas).
    eia_pipeline = EIAPipeline(duck, http)
    # Phase 16 §11 rank 6: CFTC TFF asset-manager vs leveraged-money positioning.
    cftc_tff_pipeline = CftcTffPipeline(duck, http)
    # Phase 16 §11 rank 9: Cleveland Fed daily inflation nowcast (CPI/PCE).
    cleveland_pipeline = ClevelandPipeline(duck, http)
    # Phase 16 §11 rank 11: policy-risk trio — GPR + TPU (scalars) and Fed
    # speeches scored hawk/dove by the existing FinBERT service.
    policyrisk_pipeline = PolicyRiskPipeline(duck, http)
    fed_speeches_pipeline = FedSpeechPipeline(
        duck, http, sentiment,
        max_items=settings.fed_speeches_max,
        index_window=settings.fed_speeches_index_window,
    )
    # Phase 16 §11 ranks 12-13: EDGAR corporate-event scanners (EFTS full-text).
    edgar_8k_pipeline = Edgar8KPipeline(
        duck, http, lookback_days=settings.edgar_8k_lookback_days
    )
    edgar_13d_pipeline = Edgar13DPipeline(
        duck, http,
        lookback_days=settings.edgar_13d_lookback_days,
        max_xml_fetches=settings.edgar_13d_max_xml_fetches,
    )

    # Phase 15: narrative-vs-money divergence + Polymarket front-running.
    polymarket_pipeline = (
        PolymarketPipeline(
            duck, sqlite, http,
            max_markets=settings.polymarket_max_markets,
            fetch_limit=settings.polymarket_fetch_limit,
            holders=settings.polymarket_holders,
        )
        if settings.polymarket_enabled else None
    )
    divergence_pipeline = (
        DivergencePipeline(
            duck, sqlite, http, hub,
            news_lookback_hours=settings.divergence_news_lookback_hours,
            pm_lookback_hours=settings.divergence_pm_lookback_hours,
        )
        if settings.divergence_enabled else None
    )

    # Phase 9: international macro + strategist synthesis.
    intl_pipeline = IntlPipeline(duck, http)

    # Phase 12: paper trading bot — a guarded execution surface for the
    # strategist's suggestions. Paper-only by default; live trading is blocked
    # in the broker; the kill switch starts OFF. Not on the scheduler.
    # Broker backend: Alpaca paper (default) or Interactive Brokers (a REAL
    # funded account read through a locally-run Client Portal Gateway; reads-only,
    # order writes stubbed to refuse). Both expose the same surface, so BrokerState
    # and every downstream reader are backend-agnostic.
    # Built ahead of strategist_service (below) so the strategist's read-only
    # 'portfolio' tool can share this same TTL-cached view.
    if settings.broker_backend.lower() == "ibkr":
        broker = IbkrBroker(
            base_url=settings.ibkr_base_url,
            account_id=settings.ibkr_account_id,
            allow_live=settings.ibkr_allow_live,
        )
        log.info("trading backend: IBKR (reads-only) base=%s", settings.ibkr_base_url)
    else:
        broker = AlpacaPaperBroker(
            http,
            key_id=settings.paper_trading_key_id,
            secret=settings.paper_trading_secret_key,
            base_url=settings.alpaca_trading_base_url,
            allow_live=settings.bot_allow_live_trading,
        )
    # One shared, TTL-cached broker view for every reader (status polls + both
    # bots + optimizer + the strategist's portfolio tool) — collapses bursts to
    # a single paper-api call.
    broker_state = BrokerState(broker, ttl=settings.broker_cache_ttl_seconds)

    strategist_service = StrategistService(
        duck, sqlite, hub, llm,
        sectors=settings.sector_etfs, benchmark=settings.rrg_benchmark,
        tools_enabled=settings.strategist_tools,
        tool_calls=settings.strategist_tool_calls,
        broker_state=broker_state,
    )

    # Phase 13: optimizer decides the swing/day capital split.
    optimizer = PortfolioOptimizer(
        duck, sqlite, hub,
        day_min=settings.day_alloc_min_pct, day_max=settings.day_alloc_max_pct,
    )
    trading_bot = TradingBotService(
        duck, sqlite, hub, broker_state,
        GuardrailConfig.from_settings(settings), optimizer,
        stop_pct=settings.bot_stop_assumption_pct,
        default_mode=settings.bot_mode_default,
        settings=settings,
    )
    # Phase 13: fast day-trade sleeve (small universe, auto-exec paper). Uses
    # the DATA keys for intraday bars and the shared broker view for execution.
    day_trader = DayTraderService(
        duck, sqlite, hub, broker_state, optimizer,
        http=http,
        data_key_id=settings.alpaca_key_id,
        data_secret=settings.alpaca_secret_key,
        settings=settings,
    )
    if settings.bot_enabled_default:
        await trading_bot.set_enabled(True)
    if settings.day_enabled_default:
        await day_trader.set_enabled(True)
    log.info("paper bots: broker=%s paper=%s (both kill switches off by default)",
             "configured" if broker.enabled else "no-keys", broker.is_paper)

    scheduler = build_scheduler(
        settings, news_pipeline, macro_pipeline, multiasset_pipeline, retail_pipeline,
        corr_pipeline, alert_engine, insider_pipeline, rotation_pipeline,
        cot_pipeline, gex_adapter, calendar_pipeline, brief_service,
        liquidity_pipeline, congress_pipeline, whales_pipeline,
        intl_pipeline, strategist_service, insider_scan_pipeline,
        filings_diff_pipeline, fomc_diff_pipeline,
        short_interest_pipeline=short_interest_pipeline,
        optimizer=optimizer, day_trader=day_trader, trading_bot=trading_bot,
        polymarket_pipeline=polymarket_pipeline,
        divergence_pipeline=divergence_pipeline,
        treasury_pipeline=treasury_pipeline,
        kalshi_pipeline=kalshi_pipeline,
        ici_pipeline=ici_pipeline,
        bis_pipeline=bis_pipeline,
        eia_pipeline=eia_pipeline,
        cftc_tff_pipeline=cftc_tff_pipeline,
        cleveland_pipeline=cleveland_pipeline,
        policyrisk_pipeline=policyrisk_pipeline,
        fed_speeches_pipeline=fed_speeches_pipeline,
        edgar_8k_pipeline=edgar_8k_pipeline,
        edgar_13d_pipeline=edgar_13d_pipeline,
        duck=duck,
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
    app.state.source_health = source_health
    app.state.sentiment = sentiment
    app.state.forecast = forecast_service
    app.state.news_pipeline = news_pipeline
    app.state.macro_pipeline = macro_pipeline
    app.state.multiasset_pipeline = multiasset_pipeline
    app.state.retail_pipeline = retail_pipeline
    app.state.corr_pipeline = corr_pipeline
    app.state.crypto_streamer = crypto_streamer
    app.state.scheduler = scheduler
    app.state.hub = hub
    app.state.ntfy = ntfy
    app.state.alert_engine = alert_engine
    app.state.insider_pipeline = insider_pipeline
    app.state.insider_scan_pipeline = insider_scan_pipeline
    app.state.filings_diff_pipeline = filings_diff_pipeline
    app.state.fomc_diff_pipeline = fomc_diff_pipeline
    app.state.rotation_pipeline = rotation_pipeline
    app.state.cot_pipeline = cot_pipeline
    app.state.short_interest_pipeline = short_interest_pipeline
    app.state.gex_adapter = gex_adapter
    app.state.calendar_pipeline = calendar_pipeline
    app.state.brief_service = brief_service
    app.state.liquidity_pipeline = liquidity_pipeline
    app.state.treasury_pipeline = treasury_pipeline
    app.state.kalshi_pipeline = kalshi_pipeline
    app.state.ici_pipeline = ici_pipeline
    app.state.bis_pipeline = bis_pipeline
    app.state.eia_pipeline = eia_pipeline
    app.state.cftc_tff_pipeline = cftc_tff_pipeline
    app.state.cleveland_pipeline = cleveland_pipeline
    app.state.policyrisk_pipeline = policyrisk_pipeline
    app.state.fed_speeches_pipeline = fed_speeches_pipeline
    app.state.edgar_8k_pipeline = edgar_8k_pipeline
    app.state.edgar_13d_pipeline = edgar_13d_pipeline
    app.state.congress_pipeline = congress_pipeline
    app.state.whales_pipeline = whales_pipeline
    app.state.llm = llm
    app.state.intl_pipeline = intl_pipeline
    app.state.polymarket_pipeline = polymarket_pipeline
    app.state.divergence_pipeline = divergence_pipeline
    app.state.strategist_service = strategist_service
    app.state.broker = broker
    app.state.broker_state = broker_state
    app.state.optimizer = optimizer
    app.state.trading_bot = trading_bot
    app.state.day_trader = day_trader

    try:
        yield
    finally:
        log.info("shutting down")
        await crypto_streamer.stop()
        scheduler.shutdown(wait=False)
        sentiment.close()
        forecast_service.close()
        await ntfy.aclose()
        await http.aclose()
        duck.close()
        sqlite.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        # M3: the packaged Tauri desktop shell serves the frontend from its
        # own fixed custom-scheme origin, not localhost:3000 — added
        # unconditionally (not user-configurable via settings.cors_origins)
        # since it's a property of how Tauri packages the app, not a data
        # source. See apps/desktop/README.md.
        allow_origins=[*settings.cors_origins, "tauri://localhost", "http://tauri.localhost"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router.router)
    app.include_router(sentiment_router.router)
    app.include_router(news_router.router)
    app.include_router(macro_router.router)
    app.include_router(series_router.router)
    app.include_router(forecast_router.router)
    app.include_router(markers_router.router)
    app.include_router(multiasset_router.router)
    app.include_router(retail_router.router)
    app.include_router(corr_router.router)
    app.include_router(edge_router.router)
    app.include_router(watchlist_router.router)
    app.include_router(trading_router.router)
    app.include_router(settings_router.router)
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
