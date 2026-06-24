"""Application settings and resolved filesystem paths.

Everything configurable lives here so the rest of the app never reaches for
os.environ directly. Values are read from the environment (prefix ``MARKET_``)
and an optional ``.env`` file in apps/api.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# apps/api/app/config.py -> parents: [0]=app [1]=api [2]=apps [3]=<repo root>
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MARKET_",
        env_file=str(Path(__file__).resolve().parents[1] / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Market Terminal API"
    version: str = "0.1.0"

    # Contact email baked into the global User-Agent. SEC EDGAR REQUIRES a
    # descriptive UA or it returns 403; we use the same UA everywhere.
    contact_email: str = "saatvik1213@gmail.com"

    # Where DuckDB / SQLite / the HTTP cache live. Defaults to <repo>/data.
    data_dir: Path = Field(default=REPO_ROOT / "data")

    # Demo heartbeat cadence (seconds) for the Phase-0 WS round-trip.
    heartbeat_seconds: int = 5

    # --- Phase 1: sentiment + news ---
    # Optional Finnhub key (free tier, 60 calls/min). Empty -> ingestor disabled,
    # Yahoo RSS + EDGAR still run (graceful degradation per PLAN §3a).
    finnhub_api_key: str = ""

    # Optional FinBERT + distilroberta-financial ensemble (PLAN §3a accuracy-
    # without-size). Off by default: doubles load time/RAM for marginal gain.
    sentiment_ensemble: bool = False

    # Force inference device ("mps" | "cpu"); empty = auto-detect MPS.
    sentiment_device: str = ""

    # Poll cadences (minutes). Yahoo/Finnhub per-ticker RSS 5-15 min per plan;
    # EDGAR submissions refresh ~10 min server-side.
    news_poll_minutes: int = 10
    edgar_poll_minutes: int = 10
    # Only ingest EDGAR filings newer than this many days (first-run flood guard).
    edgar_lookback_days: int = 7
    # GDELT theme rotation (slow per plan — 1 req/5s budget) and the broad
    # CNBC/MarketWatch/Investing topic feeds (~5 min per plan).
    gdelt_poll_minutes: int = 15
    broad_rss_poll_minutes: int = 5

    # --- Phase 2: macro / liquidity + regime ---
    # Optional FRED key (free at fred.stlouisfed.org). Empty -> the keyless
    # fredgraph.csv endpoint is used instead (same data, politer cadence).
    fred_api_key: str = ""

    # Daily-ish sources: FRED updates ~16:30 ET, CBOE/FINRA after the close.
    # 6h intervals keep things fresh without hammering anyone; NAAIM/AAII are
    # weekly prints so 12h is plenty.
    fred_poll_minutes: int = 360
    cboe_poll_minutes: int = 360
    finra_poll_minutes: int = 360
    naaim_poll_minutes: int = 720
    aaii_poll_minutes: int = 720
    # Composite recompute safety-net (each ingest also recomputes on success).
    composite_poll_minutes: int = 60

    # --- Phase 4: multi-asset liquidity & major trades ---
    # CCXT Pro public WebSocket streams (no keys). Coinbase + Kraken work from
    # US IPs; add "binance" here if your region allows it.
    crypto_exchanges: list[str] = ["coinbase", "kraken"]
    crypto_symbols: list[str] = ["BTC/USD", "ETH/USD"]
    # A print is "large" at >= this notional (PLAN §3e: >$250k) OR at a rolling
    # z-score >= large_print_z, with a floor so a z-spike on a quiet tape can't
    # flag a $500 trade.
    large_print_notional: float = 250_000
    large_print_z: float = 4.0
    large_print_floor: float = 25_000
    # Stored large prints are pruned past this horizon (tape history).
    trades_retention_days: int = 14
    # gold-api.com spot poll (keyless, unlimited; plan says 30-60s).
    metals_poll_seconds: int = 60
    # FINRA per-ticker short-vol + CoinPaprika global aggregates (daily-ish).
    flows_poll_minutes: int = 360

    # --- Phase 5: retail market score ---
    # ApeWisdom filters to merge (all-stocks + all-crypto cover the board
    # without double-counting; add "wallstreetbets" etc. for sub-views).
    apewisdom_filters: list[str] = ["all-stocks", "all-crypto"]
    apewisdom_pages: int = 2  # 100 tickers/filter — plenty past the spike tail
    # ApeWisdom updates ~2x/hr server-side; 20 min keeps snapshots distinct.
    apewisdom_poll_minutes: int = 20
    # StockTwits/Bluesky text polls (spiking tickers ONLY, per PLAN cadence).
    retail_social_poll_minutes: int = 30
    retail_spike_top_n: int = 8
    retail_message_retention_days: int = 14
    # Tradestie WSB cross-confirmation (Cloudflare-gated — may never deliver).
    tradestie_poll_minutes: int = 30

    # --- Phase 6: correlation cookbook ---
    # Recompute cadence (PLAN §3f: every 15 min from cached series). The price
    # legs are ensured cache-first each run, so most runs cost no network.
    corr_poll_minutes: int = 15

    # --- Phase 7: edge extras + alerting + brief ---
    # ntfy.sh push: set a LONG RANDOM topic (it is the only auth). Empty ->
    # alerts stay in-app only (WS + panel), nothing leaves the machine.
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    # Alert sweep cadence; every rule also dedupes per-key with this cooldown.
    alerts_poll_minutes: int = 10
    alert_cooldown_hours: int = 24
    # Form 4 cluster buys: watchlist issuers only (bounded EDGAR requests).
    insider_poll_minutes: int = 240
    insider_lookback_days: int = 30
    insider_cluster_window_days: int = 14
    insider_cluster_min_buyers: int = 2
    insider_min_trade_value: float = 25_000
    # Market-wide Form 4 scanner: sweeps the EDGAR daily form index for
    # open-market buys beyond the watchlist (discovery). Each sweep fetches at
    # most max_filings submissions; a cursor resumes through the day's backlog.
    insider_scan_enabled: bool = True
    insider_scan_poll_minutes: int = 120
    insider_scan_max_filings: int = 300
    insider_scan_min_value: float = 50_000
    # "Lazy Prices" 10-K/10-Q risk-factor diffs for tracked tickers. Mostly
    # no-ops between filings; alert fires below the similarity threshold.
    filings_diff_poll_minutes: int = 720
    filings_diff_alert_similarity: float = 0.70
    # RS/RRG sector rotation + seasonality (pure math on cached daily bars).
    rotation_poll_minutes: int = 360
    sector_etfs: list[str] = [
        "XLK", "XLF", "XLE", "XLV", "XLI", "XLP",
        "XLY", "XLU", "XLB", "XLRE", "XLC",
    ]
    rrg_benchmark: str = "SPY"
    # CFTC COT (weekly print, Fridays 15:30 ET) — 12h poll is plenty.
    cot_poll_minutes: int = 720
    # CBOE delayed options JSON is 15-min delayed; fragile, isolated adapter.
    gex_poll_minutes: int = 30
    gex_symbols: list[str] = ["_SPX", "SPY"]
    # Daily auto-brief: pre-market cron (UTC) + on-demand POST /api/brief/run.
    brief_hour_utc: int = 11
    brief_minute_utc: int = 30
    # Local LLM for the brief narrative (Ollama). Unreachable -> deterministic
    # template fallback, the brief never silently fails.
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:8b"
    ollama_timeout_seconds: float = 180.0
    # Event Horizon calendar: earnings dates refresh (FOMC/CPI/NFP/OPEX legs
    # are static or computed locally and cost nothing).
    calendar_poll_minutes: int = 720

    # --- Phase 8: true net liquidity + smart money 2.0 ---
    # Daily TGA (FiscalData) + ON RRP (NY Fed) — both keyless. Together with
    # FRED's weekly WALCL they form the daily NET_LIQUIDITY series ($bn).
    liquidity_poll_minutes: int = 360
    # First-run history start. 2022-05 is when the DTS "TGA Closing Balance"
    # account label stabilised; earlier history comes from weekly WTREGEN.
    liquidity_start: str = "2022-05-01"
    # Senate eFD PTR scraper (electronic filings only; House PTRs are
    # PDF-only and skipped). Filings trickle in — 12h cadence is plenty.
    congress_poll_minutes: int = 720
    congress_lookback_days: int = 90
    # EDGAR 13F whale tracker: "CIK:Display Name" pairs (all verified CIKs).
    # Quarterly prints with a 45-day lag — daily poll is generous.
    whales_poll_minutes: int = 1440
    whale_top_holdings: int = 20
    whale_funds: list[str] = [
        "1067983:Berkshire Hathaway",
        "1350694:Bridgewater",
        "1336528:Pershing Square",
        "1649339:Scion Asset Mgmt",
        "1536411:Duquesne Family Office",
        "1167483:Tiger Global",
        "1061768:Baupost Group",
        "1040273:Third Point",
        "1029160:Soros Fund Mgmt",
        "1006438:Appaloosa",
    ]

    # --- Phase 9: breadth, intl macro, FOMC scrape, strategist, LLM providers ---
    # DBnomics international series (monthly prints — 12h poll is generous).
    # Econdb was the planned source but is hard Cloudflare-blocked for
    # non-browser clients (verified 2026-06-10); DBnomics is the same kind of
    # keyless aggregator and serves the OECD/Eurostat series directly.
    intl_poll_minutes: int = 720
    # Net-liquidity drain alert: 20-print (~1 month) change below this fires
    # a warn ($bn). Crossing-not-level semantics like every threshold rule.
    netliq_drain_alert_bn: float = -150.0
    # Strategist snapshot cadence (pure local reads + optional LLM notes).
    strategist_poll_minutes: int = 360

    # LLM provider for the brief/strategist narratives.
    #   ollama    — local, default ($0 tokens; uses ollama_url/ollama_model)
    #   openai    — api.openai.com (needs MARKET_LLM_API_KEY)
    #   deepseek  — api.deepseek.com (needs MARKET_LLM_API_KEY)
    #   anthropic — api.anthropic.com (needs MARKET_LLM_API_KEY)
    # Every provider falls back to the deterministic template on failure —
    # narratives never silently fail and never block the digest.
    llm_provider: str = "ollama"
    llm_model: str = ""        # empty -> per-provider default (see edge/llm.py)
    llm_api_key: str = ""
    llm_base_url: str = ""     # override, e.g. a self-hosted OpenAI-compatible server
    llm_timeout_seconds: float = 180.0

    # --- Phase 11: gap-audit backlog (PLAN §10) ---
    # FINRA true short interest publishes bi-monthly (~9-day lag); the daily
    # run is a partition-list diff and a no-op between publication days.
    short_interest_poll_minutes: int = 1440
    # Squeeze-watch alert: days-to-cover at/above this plus a retail mention
    # spike fires a warn; days-to-cover alone at this level fires an info.
    squeeze_days_to_cover: float = 5.0
    # FMP earnings calendar (market-wide Event Horizon — PLAN §9 #7). Empty
    # key = off and the watchlist yfinance leg keeps working; with a key the
    # calendar covers watchlist + news tickers + current strategist picks and
    # the blocking yfinance leg is skipped.
    fmp_api_key: str = ""
    # Alpaca market data (PLAN §10 #2 — price redundancy + live-ish quotes).
    # Free keys (no funded account) from app.alpaca.markets. Empty = off and
    # yfinance remains the sole price source; with keys, live watchlist quotes
    # come from one batched IEX snapshot call and daily history gains an
    # Alpaca fallback for when Yahoo breaks the way Stooq did.
    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""

    # --- Phase 12: paper trading bot (strategist proposal surface) ---
    # The bot turns the strategist's suggested allocation into Alpaca PAPER
    # orders, behind hard code-level guardrails. Everything below is OFF or
    # proposal-only by default — the bot never trades until explicitly enabled,
    # and live trading is blocked outright (see bot_allow_live_trading).
    #
    # Trading uses a SEPARATE Alpaca endpoint + (usually) a SEPARATE key pair
    # from market data: the data API (data.alpaca.markets) takes either key,
    # but the paper trading API only accepts PAPER-account keys. If your
    # MARKET_ALPACA_* keys are paper keys they are reused here; otherwise set
    # MARKET_ALPACA_PAPER_* to your paper key pair from app.alpaca.markets.
    alpaca_trading_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_paper_key_id: str = ""       # empty -> falls back to alpaca_key_id
    alpaca_paper_secret_key: str = ""   # empty -> falls back to alpaca_secret_key
    # HARD SAFETY GATE. The broker refuses to submit an order unless its base
    # URL is the paper endpoint, UNLESS this is explicitly flipped to True. The
    # research is unambiguous: never wire an autonomous LLM loop to a live key.
    bot_allow_live_trading: bool = False

    # Kill switch — the bot starts DISABLED. propose() (read-only) always works;
    # execute()/auto-run refuse while disabled. Persisted in bot_config and
    # toggled from the API; this is only the first-boot default.
    bot_enabled_default: bool = False
    # "proposal" — generate proposals, never submit (human approves each via the
    #   /api/bot/execute endpoint). "auto" — submit non-blocked proposals to the
    #   PAPER account automatically when /api/bot/run is called (still paper-only).
    bot_mode_default: str = "proposal"

    # Guardrails (enforced in code, not prompts — see trading/guardrails.py).
    bot_max_position_pct: float = 15.0       # cap any one symbol at this % of equity
    bot_max_position_notional: float = 5_000.0  # absolute $ cap per symbol
    bot_min_order_notional: float = 100.0    # skip dust trades below this $
    bot_daily_loss_limit_pct: float = 3.0    # halt NEW BUYS if account is down this % today
    bot_rebalance_band_pp: float = 2.0       # ignore drifts smaller than this (pp of equity)
    # Per-asset assumed adverse move used only to surface an illustrative
    # max-loss estimate on each proposal (NOT a real stop order).
    bot_stop_assumption_pct: dict[str, float] = {
        "equities": 8.0, "metals": 10.0, "crypto": 20.0, "cash": 1.0,
    }
    # Shared broker-read cache TTL (seconds). status()/both bots/optimizer all
    # read account+positions+orders through one cache so a burst of UI polls or
    # bot ticks collapses to a single paper-api call — the main IP-block guard.
    broker_cache_ttl_seconds: float = 4.0

    # --- Phase 13: portfolio optimizer + two-sleeve bots ---
    # The optimizer splits capital between a long-term SWING sleeve (the
    # strategist allocator) and a short-term DAY sleeve (the fast trader),
    # by market conditions. Day stays a small slice; swing gets the rest.
    day_alloc_min_pct: float = 5.0    # day sleeve floor (% of equity)
    day_alloc_max_pct: float = 10.0   # day sleeve ceiling (% of equity)
    # Day trader. Small liquid universe keeps the data footprint to ~2 batched
    # snapshot calls per tick. Crypto runs 24/7; equities are market-hours gated.
    day_universe: list[str] = ["SPY", "QQQ", "NVDA", "TSLA", "BTC/USD", "ETH/USD"]
    day_enabled_default: bool = False  # the day bot starts HALTED too
    day_poll_minutes: int = 3          # cadence (equity legs gated to market hours)
    day_intraday_lookback_min: int = 30  # 1-min bars window for the signal
    # Day-trade signal thresholds.
    day_breakout_buffer_pct: float = 0.05   # price within this % of the window high = breakout
    day_momentum_min_pct: float = 0.30      # min intraday move to call it momentum
    day_reversion_z: float = 2.0            # |z| vs intraday VWAP to call mean-reversion
    day_min_signal: float = 1.0             # min combined signal strength to act
    # Day guardrails (sized against the DAY SLEEVE budget, not whole equity).
    day_max_position_pct: float = 40.0      # one name <= this % of the day sleeve
    day_min_order_notional: float = 50.0
    day_daily_loss_limit_pct: float = 2.0   # halt day BUYS if the day sleeve is down this % today
    # Major-news override: only act on news this fresh / strong / corroborated.
    day_news_max_age_min: int = 30
    day_news_min_abs_score: float = 0.6
    day_news_min_outlets: int = 2

    # CORS origins allowed to call the API (the Next.js dev server).
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    @property
    def user_agent(self) -> str:
        return f"MarketTerminal/{self.version} ({self.contact_email})"

    @property
    def paper_trading_key_id(self) -> str:
        """Paper trading key — dedicated paper key if set, else the data key."""
        return self.alpaca_paper_key_id or self.alpaca_key_id

    @property
    def paper_trading_secret_key(self) -> str:
        return self.alpaca_paper_secret_key or self.alpaca_secret_key

    @property
    def duckdb_path(self) -> Path:
        return self.data_dir / "market.duckdb"

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def http_cache_dir(self) -> Path:
        return self.data_dir / "http_cache"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.http_cache_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
