"""Idempotent schema bootstrap for both stores.

DuckDB holds the time-series the analytical panels query; SQLite holds tiny
transactional app state. All DDL is CREATE ... IF NOT EXISTS so startup is safe
to run on every boot. Phase 0 seeds a default watchlist so later panels have
something to render.
"""

from __future__ import annotations

import sqlite3

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore

DEFAULT_WATCHLIST = [
    # (symbol, asset_class, display_name)
    ("SPY", "equity", "S&P 500 ETF"),
    ("QQQ", "equity", "Nasdaq 100 ETF"),
    ("AAPL", "equity", "Apple"),
    ("NVDA", "equity", "NVIDIA"),
    ("BTC/USD", "crypto", "Bitcoin"),
    ("ETH/USD", "crypto", "Ethereum"),
    ("GLD", "metal", "Gold ETF"),
    ("SLV", "metal", "Silver ETF"),
    ("XAU", "metal", "Gold spot"),
    ("XAG", "metal", "Silver spot"),
]


def init_duckdb(duck: DuckStore) -> None:
    # OHLCV / quotes for every asset class (stocks, crypto, metals, futures, FX).
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS ts_price (
            source       VARCHAR NOT NULL,
            symbol       VARCHAR NOT NULL,
            asset_class  VARCHAR NOT NULL,
            ts           TIMESTAMP NOT NULL,
            open         DOUBLE,
            high         DOUBLE,
            low          DOUBLE,
            close        DOUBLE,
            volume       DOUBLE,
            PRIMARY KEY (source, symbol, ts)
        );
        """
    )
    # Macro / indicator series (FRED, CBOE-derived, breadth, sentiment surveys).
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS ts_macro (
            series_id  VARCHAR NOT NULL,
            ts         TIMESTAMP NOT NULL,
            value      DOUBLE,
            source     VARCHAR DEFAULT 'fred',
            PRIMARY KEY (series_id, ts)
        );
        """
    )
    # Point-in-time (ALFRED) vintages for *revised* FRED series. One row per
    # observation per vintage that changed its value: release_date is the actual
    # publication date that value became known. Lets the ML feature matrix use
    # the value that was knowable as-of each decision date (revision-leak-safe)
    # instead of ts_macro's latest-revised value. See app/ml/dataset.py.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS ts_macro_vintage (
            series_id    VARCHAR NOT NULL,
            ref_date     DATE NOT NULL,
            release_date DATE NOT NULL,
            value        DOUBLE,
            PRIMARY KEY (series_id, ref_date, release_date)
        );
        """
    )
    # Sentiment scores, keyed by text hash so a re-seen article is never rescored.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS ts_sentiment (
            text_hash   VARCHAR NOT NULL,
            source      VARCHAR NOT NULL,
            symbol      VARCHAR,
            ts          TIMESTAMP NOT NULL,
            score       DOUBLE,
            confidence  DOUBLE,
            label       VARCHAR,
            model       VARCHAR,
            PRIMARY KEY (text_hash, symbol)
        );
        """
    )
    # Deduped, FinBERT-scored news timeline (Panel a). id = normalized-URL hash.
    # outlets / outlet_names: multi-outlet convergence — when a dup of this
    # story arrives from a DIFFERENT source, the counter grows instead of the
    # item being silently dropped (PLAN §3a "multi-outlet convergence" badge).
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS news_items (
            id           VARCHAR PRIMARY KEY,
            source       VARCHAR NOT NULL,
            symbol       VARCHAR,
            title        VARCHAR NOT NULL,
            summary      VARCHAR,
            url          VARCHAR,
            published    TIMESTAMP NOT NULL,
            ingested     TIMESTAMP NOT NULL,
            score        DOUBLE,
            confidence   DOUBLE,
            label        VARCHAR,
            model        VARCHAR,
            outlets      INTEGER DEFAULT 1,
            outlet_names VARCHAR
        );
        """
    )
    # Migrate pre-convergence DBs in place (DuckDB supports IF NOT EXISTS here).
    duck.execute("ALTER TABLE news_items ADD COLUMN IF NOT EXISTS outlets INTEGER DEFAULT 1;")
    duck.execute("ALTER TABLE news_items ADD COLUMN IF NOT EXISTS outlet_names VARCHAR;")
    # Daily composite Risk-On/Off snapshots (Panel c). detail = JSON with the
    # sub-bucket z-scores/contributions + regime dials so the UI can show
    # *what* drives the regime; one row per day (intraday recompute replaces).
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS macro_composite (
            ts      TIMESTAMP PRIMARY KEY,
            score   DOUBLE,
            regime  VARCHAR,
            detail  VARCHAR
        );
        """
    )
    # Retail mention/volume snapshots (ApeWisdom, StockTwits, Bluesky).
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS ts_retail (
            source          VARCHAR NOT NULL,
            symbol          VARCHAR NOT NULL,
            ts              TIMESTAMP NOT NULL,
            mentions        BIGINT,
            mentions_prev   BIGINT,
            rank            INTEGER,
            upvotes         BIGINT,
            sentiment_score DOUBLE,
            PRIMARY KEY (source, symbol, ts)
        );
        """
    )
    # ApeWisdom ships rank_24h_ago; persisting it gives the leaderboard its
    # rank-velocity term without a second snapshot lookup.
    duck.execute("ALTER TABLE ts_retail ADD COLUMN IF NOT EXISTS rank_prev INTEGER;")
    # Individual scored social messages (StockTwits/Bluesky) behind the
    # per-symbol drill-down. Only spiking tickers are ever polled, and rows
    # age out on a short retention, so this stays small.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS retail_messages (
            id      VARCHAR PRIMARY KEY,  -- source-prefixed message id
            source  VARCHAR NOT NULL,     -- stocktwits | bluesky
            symbol  VARCHAR NOT NULL,
            ts      TIMESTAMP NOT NULL,
            text    VARCHAR,
            url     VARCHAR,
            score   DOUBLE,               -- FinBERT -1..+1
            label   VARCHAR,
            tag     VARCHAR               -- human Bullish/Bearish (StockTwits)
        );
        """
    )
    # Individual large prints / trade tape (CCXT crypto streams, proxies).
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            source       VARCHAR NOT NULL,
            symbol       VARCHAR NOT NULL,
            asset_class  VARCHAR NOT NULL,
            ts           TIMESTAMP NOT NULL,
            price        DOUBLE,
            size         DOUBLE,
            side         VARCHAR,
            notional     DOUBLE
        );
        """
    )
    # Correlation Cookbook snapshots (Panel f): one row per card per day, plus
    # a '_meta' row holding the full computed result (regime, stress radar,
    # heatmap) so /api/corr is a single read. detail = JSON.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS corr_snapshots (
            card_id  VARCHAR NOT NULL,
            ts       TIMESTAMP NOT NULL,
            status   VARCHAR,
            corr30   DOUBLE,
            corr90   DOUBLE,
            z        DOUBLE,
            value    DOUBLE,
            detail   VARCHAR,
            PRIMARY KEY (card_id, ts)
        );
        """
    )
    # Per-ticker FINRA daily short-sale volume (Panel e accumulation proxy).
    # Short SELL volume incl. MM hedging, NOT short interest (PLAN §3e).
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS ts_short_vol (
            symbol        VARCHAR NOT NULL,
            ts            TIMESTAMP NOT NULL,
            short_volume  DOUBLE,
            total_volume  DOUBLE,
            PRIMARY KEY (symbol, ts)
        );
        """
    )
    # TRUE short interest (shares held short, bi-monthly) from the FINRA
    # Query API — Phase 11 #5. Distinct from ts_short_vol above.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS ts_short_interest (
            settlement_date   TIMESTAMP NOT NULL,
            symbol            VARCHAR NOT NULL,
            shares_short      DOUBLE,
            shares_short_prev DOUBLE,
            change_pct        DOUBLE,
            avg_daily_volume  DOUBLE,
            days_to_cover     DOUBLE,
            PRIMARY KEY (settlement_date, symbol)
        );
        """
    )
    # --- Phase 7: edge extras ---
    # Fired alerts (the in-app feed; ntfy push is fan-out only). id = rule:key:day
    # hash so a re-evaluated rule can never double-insert.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id        VARCHAR PRIMARY KEY,
            ts        TIMESTAMP NOT NULL,
            rule      VARCHAR NOT NULL,     -- macro_z | corr_break | regime_flip | ...
            severity  VARCHAR NOT NULL,     -- info | warn | critical
            title     VARCHAR NOT NULL,
            body      VARCHAR,
            symbol    VARCHAR,
            value     DOUBLE,
            regime    VARCHAR,              -- regime at fire time (gating context)
            pushed    BOOLEAN DEFAULT FALSE -- delivered to ntfy
        );
        """
    )
    # SEC Form 4 open-market BUYS (transaction code P) for watchlist issuers.
    # One row per (accession, insider); clusters are detected at read time.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS insider_trades (
            accession     VARCHAR NOT NULL,
            symbol        VARCHAR NOT NULL,
            issuer_name   VARCHAR,
            insider_name  VARCHAR NOT NULL,
            insider_title VARCHAR,
            is_officer    BOOLEAN,
            is_director   BOOLEAN,
            is_ceo_cfo    BOOLEAN,
            filed_at      TIMESTAMP NOT NULL,
            trade_date    TIMESTAMP,
            shares        DOUBLE,
            price         DOUBLE,
            value         DOUBLE,            -- shares * price
            url           VARCHAR,
            PRIMARY KEY (accession, insider_name)
        );
        """
    )
    # Phase 16 §11 rank 12 — 8-K item-code catalysts (cyber 1.05, impairment
    # 2.06, exec departure 5.02, buyback 8.01) from EDGAR full-text search. One
    # row per filing, stored under its highest-priority matching catalyst.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS edgar_8k_events (
            accession   VARCHAR PRIMARY KEY,
            cik         VARCHAR,
            ticker      VARCHAR,
            company     VARCHAR,
            item_code   VARCHAR,        -- the catalyst item (1.05/2.06/5.02/8.01)
            catalyst    VARCHAR,        -- human label for item_code
            items       VARCHAR,        -- all item codes on the filing, comma-joined
            filed_at    TIMESTAMP,
            url         VARCHAR
        );
        """
    )
    # Phase 16 §11 rank 13 — SC 13D (activist, intent to influence) and SC 13G
    # (passive) beneficial-ownership stakes. Structured fields (pct/shares/
    # purpose) populated from the Dec-2024 primary_doc.xml when present; NULL for
    # older HTML-only filings (the filing-level row is still kept).
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS edgar_13d_filings (
            accession      VARCHAR PRIMARY KEY,
            subject_cik    VARCHAR,
            subject_ticker VARCHAR,
            subject_name   VARCHAR,
            filer_name     VARCHAR,     -- the reporting person(s) / activist
            filing_type    VARCHAR,     -- SCHEDULE 13D, SCHEDULE 13D/A, 13G, 13G/A
            is_activist    BOOLEAN,     -- 13D = true (intent), 13G = false (passive)
            pct_owned      DOUBLE,      -- % of class (nullable)
            shares         DOUBLE,      -- aggregate amount owned (nullable)
            purpose        VARCHAR,     -- stated transaction purpose (nullable)
            filed_at       TIMESTAMP,
            url            VARCHAR
        );
        """
    )
    # Weekly CFTC legacy COT prints for the tracked futures markets. The COT
    # Index (3y percentile of net non-commercial) is computed at read time.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS ts_cot (
            market_code    VARCHAR NOT NULL,  -- CFTC contract market code
            market         VARCHAR,           -- short label (GOLD, ES, BTC ...)
            ts             TIMESTAMP NOT NULL, -- report date
            noncomm_long   DOUBLE,
            noncomm_short  DOUBLE,
            comm_long      DOUBLE,
            comm_short     DOUBLE,
            open_interest  DOUBLE,
            PRIMARY KEY (market_code, ts)
        );
        """
    )
    # Self-computed dealer-gamma snapshots from the fragile CBOE delayed
    # options JSON (one isolated adapter; rows only on schema-valid pulls).
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS gex_snapshots (
            symbol      VARCHAR NOT NULL,
            ts          TIMESTAMP NOT NULL,
            spot        DOUBLE,
            total_gex   DOUBLE,              -- $bn per 1% move, calls + puts
            flip        DOUBLE,              -- strike where cumulative GEX crosses 0
            call_wall   DOUBLE,
            put_wall    DOUBLE,
            detail      VARCHAR,             -- JSON: per-strike profile
            PRIMARY KEY (symbol, ts)
        );
        """
    )
    # --- Phase 8: true net liquidity + smart money 2.0 ---
    # Senate PTR (Periodic Transaction Report) stock trades scraped from the
    # official eFD site. Electronic filings only — paper PTRs are scanned
    # images. House PTRs are PDF-only and not ingested.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS congress_trades (
            ptr_id      VARCHAR NOT NULL,   -- eFD report uuid
            row_no      INTEGER NOT NULL,   -- row inside the PTR table
            senator     VARCHAR NOT NULL,
            filed_at    TIMESTAMP NOT NULL,
            tx_date     TIMESTAMP,
            ticker      VARCHAR,            -- NULL for non-equity assets
            asset       VARCHAR,
            asset_type  VARCHAR,
            side        VARCHAR,            -- buy | sell | exchange
            tx_type     VARCHAR,            -- raw eFD label (Sale (Full) ...)
            amount_min  DOUBLE,             -- disclosed band, USD
            amount_max  DOUBLE,             -- NULL on open-ended bands
            url         VARCHAR,
            PRIMARY KEY (ptr_id, row_no)
        );
        """
    )
    # 13F-HR filings for the tracked whale funds (one row per filing) plus
    # their top holdings. Holdings are aggregated by (cusip, class, put/call)
    # across the filing's infoTable entries; only the top N by value are kept.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS whale_filings (
            accession    VARCHAR PRIMARY KEY,
            cik          BIGINT NOT NULL,
            fund         VARCHAR NOT NULL,
            period       TIMESTAMP NOT NULL,  -- report quarter end
            filed_at     TIMESTAMP NOT NULL,
            total_value  DOUBLE,              -- USD, whole portfolio
            positions    INTEGER              -- distinct holdings in the filing
        );
        """
    )
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS whale_holdings (
            accession  VARCHAR NOT NULL,
            cusip      VARCHAR NOT NULL,
            cls        VARCHAR NOT NULL,     -- titleOfClass (COM, CL A ...)
            put_call   VARCHAR NOT NULL,     -- '' | Put | Call
            issuer     VARCHAR,
            value      DOUBLE,               -- USD
            shares     DOUBLE,
            pct        DOUBLE,               -- value / filing total_value
            rank       INTEGER,              -- 1 = largest position
            PRIMARY KEY (accession, cusip, cls, put_call)
        );
        """
    )
    # Daily pre-market auto-brief (one row per day; re-runs replace).
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS briefs (
            ts      TIMESTAMP PRIMARY KEY,
            regime  VARCHAR,
            text    VARCHAR,               -- markdown narrative
            model   VARCHAR,               -- LLM used, or 'template'
            detail  VARCHAR                -- JSON digest the narrative grounds on
        );
        """
    )
    # --- Phase 9: strategist ---
    # Daily strategist snapshots (one row per day; re-runs replace). detail =
    # full JSON output (allocation, reasons, signals) so suggestions are
    # reviewable in hindsight against what actually happened.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS strategist_snapshots (
            ts      TIMESTAMP PRIMARY KEY,
            regime  VARCHAR,
            model   VARCHAR,               -- LLM used for notes, or 'template'
            detail  VARCHAR                -- JSON: buckets, signals, notes
        );
        """
    )
    # "Lazy Prices" 10-K/10-Q risk-factor diffs: one row per newer filing,
    # similarity = 5-word-shingle Jaccard of Item 1A vs the previous filing
    # of the same form (NULL when either section is too short to score).
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS filings_diff (
            accession      VARCHAR PRIMARY KEY,
            symbol         VARCHAR NOT NULL,
            form           VARCHAR NOT NULL,    -- 10-K | 10-Q
            filed_at       TIMESTAMP NOT NULL,
            prev_accession VARCHAR,
            prev_filed     TIMESTAMP,
            similarity     DOUBLE,
            chars_new      INTEGER,
            chars_prev     INTEGER,
            url            VARCHAR,
            detail         VARCHAR              -- JSON: sample new sentences
        );
        """
    )
    # Event Horizon calendar: forward market-moving events (FOMC, CPI, NFP,
    # OPEX/quad-witching, COT prints, watchlist earnings). Upserted on refresh.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id      VARCHAR PRIMARY KEY,   -- kind:date(:symbol)
            ts      TIMESTAMP NOT NULL,    -- event date (UTC midnight)
            kind    VARCHAR NOT NULL,      -- fomc | cpi | nfp | opex | cot | earnings
            title   VARCHAR NOT NULL,
            symbol  VARCHAR,
            source  VARCHAR
        );
        """
    )
    # --- Phase 15: narrative-vs-money divergence + Polymarket front-running ---
    # Tracked Polymarket geopolitical/news markets (keyword-matched from Gamma).
    # escalation_sign: +1 when a "Yes" resolution means MORE conflict/danger
    # (war/strike/attack), -1 when "Yes" means de-escalation (ceasefire/deal),
    # 0 when undirected — used to turn an odds move into risk pressure.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_markets (
            id              VARCHAR PRIMARY KEY,   -- conditionId
            slug            VARCHAR,
            question        VARCHAR NOT NULL,
            category        VARCHAR,               -- geopolitics | politics | macro
            escalation_sign INTEGER DEFAULT 0,
            yes_token       VARCHAR,
            end_date        TIMESTAMP,
            closed          BOOLEAN DEFAULT FALSE,
            volume          DOUBLE,
            liquidity       DOUBLE,
            updated_at      TIMESTAMP NOT NULL
        );
        """
    )
    # Odds snapshots over time — the jump-detection series. One row per
    # (market, ts); yes_prob is the implied probability of the "Yes" outcome.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_odds (
            market_id  VARCHAR NOT NULL,
            ts         TIMESTAMP NOT NULL,
            yes_prob   DOUBLE,
            volume     DOUBLE,
            PRIMARY KEY (market_id, ts)
        );
        """
    )
    # Best-effort holder concentration (Data API). top_share = largest single
    # holder as a fraction of the tracked side; high concentration on a sharp
    # move is the smart/insider-wallet fingerprint. detail = JSON top holders.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS polymarket_holders (
            market_id  VARCHAR NOT NULL,
            ts         TIMESTAMP NOT NULL,
            n_holders  INTEGER,
            top_share  DOUBLE,
            top_holder VARCHAR,
            detail     VARCHAR,
            PRIMARY KEY (market_id, ts)
        );
        """
    )
    # Narrative-vs-money divergence snapshots: one row per engine run (replace
    # by minute). score 0..100 = "officials/news project calm while money
    # hedges". detail = JSON breakdown (per-proxy legs, matched markets, news).
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS divergence_snapshots (
            ts           TIMESTAMP PRIMARY KEY,
            score        DOUBLE,
            narrative    DOUBLE,   -- news/official tone -1..1 (+ = calm)
            riskoff_z    DOUBLE,   -- equity/bond/vol risk-off basket z
            pm_pressure  DOUBLE,   -- polymarket escalation pressure -1..1
            regime       VARCHAR,
            headline     VARCHAR,
            detail       VARCHAR
        );
        """
    )
    # Treasury auction demand (Phase 16 §11 rank 7, FiscalData auctions_query).
    # One row per auctioned CUSIP. Bidder-class takedown shares are the signal:
    # high dealer_pct (real money stayed away) = yield-spike risk; low
    # bid_to_cover / tail (high_yield above when-issued) = weak demand.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS treasury_auctions (
            cusip          VARCHAR PRIMARY KEY,
            auction_date   TIMESTAMP NOT NULL,
            security_type  VARCHAR,   -- Bill | Note | Bond | TIPS | FRN | CMB
            security_term  VARCHAR,
            bid_to_cover   DOUBLE,
            high_yield     DOUBLE,
            offering_amt   DOUBLE,    -- $
            total_accepted DOUBLE,    -- $
            dealer_pct     DOUBLE,    -- primary_dealer_accepted / total_accepted
            indirect_pct   DOUBLE,    -- indirect_bidder_accepted / total_accepted
            direct_pct     DOUBLE,    -- direct_bidder_accepted / total_accepted
            updated_at     TIMESTAMP NOT NULL
        );
        """
    )
    duck.execute(
        "CREATE INDEX IF NOT EXISTS idx_treasury_auctions_date "
        "ON treasury_auctions (auction_date);"
    )
    # Kalshi prediction-market strikes (Phase 16 §11 rank 8). One row per
    # market (a single "Above X%" / range strike), replaced each poll. Grouped
    # by event_ticker = one FOMC meeting / CPI print; yes_prob is the implied
    # probability of that strike. The endpoint differences the cumulative
    # ladder into a per-bucket distribution; the ingestor also writes a
    # probability-weighted expected-rate scalar to ts_macro (KALSHI_FOMC_RATE)
    # so it charts and the divergence engine can contrast it vs FRED EFFR.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS kalshi_markets (
            market_ticker VARCHAR PRIMARY KEY,
            series_ticker VARCHAR NOT NULL,
            event_ticker  VARCHAR NOT NULL,
            yes_sub_title VARCHAR,
            floor_strike  DOUBLE,
            yes_prob      DOUBLE,        -- implied probability 0..1
            volume        DOUBLE,
            close_time    TIMESTAMP,
            updated_at    TIMESTAMP NOT NULL
        );
        """
    )
    duck.execute(
        "CREATE INDEX IF NOT EXISTS idx_kalshi_event "
        "ON kalshi_markets (series_ticker, event_ticker);"
    )
    # Fed speeches hawk/dove (Phase 16 §11 rank 11, leg 3). One row per speech
    # (URL primary key = the fetch+score cache, so each speech is scored once),
    # carrying the FinBERT sentiment, the hawk/dove proxy (= -sentiment; >0
    # hawkish, <0 dovish), and how many text chunks were averaged. The pipeline
    # also writes a rolling FED_HAWK_DOVE index scalar to ts_macro.
    duck.execute(
        """
        CREATE TABLE IF NOT EXISTS fed_speeches (
            url         VARCHAR PRIMARY KEY,
            speech_date TIMESTAMP NOT NULL,
            speaker     VARCHAR,
            title       VARCHAR,
            score       DOUBLE,     -- FinBERT sentiment, p_pos - p_neg, -1..+1
            hawk_dove   DOUBLE,     -- -score: >0 hawkish, <0 dovish
            confidence  DOUBLE,
            chunks      INTEGER,    -- # text chunks averaged for the score
            scored_at   TIMESTAMP NOT NULL
        );
        """
    )
    duck.execute(
        "CREATE INDEX IF NOT EXISTS idx_fed_speeches_date "
        "ON fed_speeches (speech_date);"
    )


def init_sqlite(sqlite: SqliteStore) -> None:
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            symbol       TEXT PRIMARY KEY,
            asset_class  TEXT NOT NULL,
            display_name TEXT,
            sort_order   INTEGER DEFAULT 0,
            added_at     TEXT DEFAULT (datetime('now'))
        );
        """
    )
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_layout (
            id      TEXT PRIMARY KEY,   -- one row per named layout
            layout  TEXT NOT NULL,      -- JSON: react-grid-layout positions
            updated_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    # Per-source cursor (last id / last timestamp pulled) for incremental ingest.
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS scraper_cursor (
            source     TEXT NOT NULL,
            key        TEXT NOT NULL,   -- e.g. symbol or feed url
            cursor     TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (source, key)
        );
        """
    )
    # Conditional-GET metadata (ETag / Last-Modified) for polite caching.
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS http_cache_meta (
            url           TEXT PRIMARY KEY,
            etag          TEXT,
            last_modified TEXT,
            fetched_at    TEXT DEFAULT (datetime('now'))
        );
        """
    )
    # News dedupe: store a content hash so the same story across outlets counts
    # once. item_id/source point back at the kept news_items row so a dup from
    # a different outlet can bump that row's convergence counter.
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS news_dedupe (
            content_hash TEXT PRIMARY KEY,
            first_seen   TEXT DEFAULT (datetime('now')),
            item_id      TEXT,
            source       TEXT
        );
        """
    )
    # Migrate pre-convergence DBs in place (SQLite has no IF NOT EXISTS here).
    for col in ("item_id TEXT", "source TEXT"):
        try:
            sqlite.execute(f"ALTER TABLE news_dedupe ADD COLUMN {col};")
        except sqlite3.OperationalError:
            pass  # duplicate column — already migrated
    # Persistent chart annotations: pinned to a chart_key at a time (+optional
    # price/series), shared by every chart instance with that key.
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS chart_markers (
            id         TEXT PRIMARY KEY,
            chart_key  TEXT NOT NULL,
            t          INTEGER NOT NULL,   -- unix seconds
            price      REAL,
            series_id  TEXT,
            text       TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    sqlite.execute(
        "CREATE INDEX IF NOT EXISTS idx_chart_markers_key ON chart_markers (chart_key);"
    )
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS app_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    # Extra tickers tracked by the news ingestors only (added from the News
    # panel) — news coverage without putting the symbol on the watchlist.
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS news_tickers (
            symbol   TEXT PRIMARY KEY,
            added_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    # Alert dedupe/cooldown: one row per rule-instance key. last_value lets
    # threshold rules fire on the CROSSING, not on every sweep above it.
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_state (
            rule_key   TEXT PRIMARY KEY,
            last_fired TEXT,
            last_value REAL,
            last_text  TEXT
        );
        """
    )
    # --- Phase 11 #3: portfolio / holdings layer ---
    # Manually-entered (or CSV-imported) positions. Stays local+private in
    # SQLite — the only table holding "what I actually own". One row per lot
    # (id PK) so the same symbol can carry multiple cost bases; P&L, exposure
    # and strategist-drift are all computed at read time from cached prices.
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol       TEXT NOT NULL,
            asset_class  TEXT NOT NULL DEFAULT 'equity',
            quantity     REAL NOT NULL,
            cost_basis   REAL,              -- per-share/unit entry cost (NULL = unknown)
            display_name TEXT,
            opened_at    TEXT,              -- optional entry date (YYYY-MM-DD)
            note         TEXT,
            added_at     TEXT DEFAULT (datetime('now')),
            updated_at   TEXT DEFAULT (datetime('now'))
        );
        """
    )

    # Source-health watchdog: one row per outbound host, written by HttpClient
    # on every final request outcome (post-retries). The consecutive-failure
    # streak is the dead-source signal; counters are lifetime totals.
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS source_health (
            host                 TEXT PRIMARY KEY,
            last_success         TEXT,
            last_failure         TEXT,
            last_error           TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            success_count        INTEGER NOT NULL DEFAULT 0,
            failure_count        INTEGER NOT NULL DEFAULT 0
        );
        """
    )

    # --- Phase 12: paper trading bot ---
    # Single-row mutable config (kill switch + mode). id is always 1. Caps live
    # in settings (env) so they can't be widened from the API; only the kill
    # switch and proposal/auto mode are runtime-toggleable here.
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_config (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            enabled    INTEGER NOT NULL DEFAULT 0,   -- kill switch (0 = halted)
            mode       TEXT NOT NULL DEFAULT 'proposal',  -- proposal | auto
            updated_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    # One row per proposed trade from a propose() run. status walks:
    # proposed -> approved -> submitted -> filled (or blocked/skipped/rejected/
    # canceled). blocks = JSON list of guardrail reasons (non-empty => blocked).
    # rationale = JSON: the strategist evidence + bear_case + invalidation.
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_proposals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          TEXT NOT NULL,        -- groups one propose() sweep
            created_at      TEXT NOT NULL,
            symbol          TEXT NOT NULL,        -- Alpaca order symbol (BTC/USD ...)
            strategist_sym  TEXT,                 -- the strategist's symbol form
            bucket          TEXT,                 -- equities | metals | crypto | cash
            side            TEXT NOT NULL,        -- buy | sell
            order_type      TEXT NOT NULL DEFAULT 'market',
            qty             REAL,                 -- set for sells (by shares)
            notional        REAL,                 -- set for buys (by dollars)
            target_pct      REAL,                 -- strategist target % of equity
            current_pct     REAL,                 -- current % of equity (broker truth)
            target_value    REAL,
            current_value   REAL,
            delta_value     REAL,                 -- target - current (signed $)
            conviction      REAL,                 -- strategist score (picks only)
            max_loss_est    REAL,                 -- illustrative downside $ estimate
            rationale       TEXT,                 -- JSON: evidence/bear_case/invalidation
            status          TEXT NOT NULL DEFAULT 'proposed',
            blocks          TEXT,                 -- JSON list of guardrail reasons
            strategist_asof TEXT,
            updated_at      TEXT DEFAULT (datetime('now'))
        );
        """
    )
    sqlite.execute(
        "CREATE INDEX IF NOT EXISTS idx_bot_proposals_run ON bot_proposals (run_id);"
    )
    # One row per order the bot actually sent to Alpaca paper. broker_order_id /
    # status / filled_* are reconciled FROM the broker (ground truth), never
    # trusted from our own optimistic write. client_order_id is deterministic
    # (bot-<proposal_id>) so a retry can never double-submit.
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_orders (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id      INTEGER,
            client_order_id  TEXT UNIQUE,
            broker_order_id  TEXT,
            symbol           TEXT NOT NULL,
            side             TEXT NOT NULL,
            order_type       TEXT,
            qty              REAL,
            notional         REAL,
            status           TEXT,               -- broker status (new/filled/...)
            filled_qty       REAL,
            filled_avg_price REAL,
            submitted_at     TEXT,
            reconciled_at    TEXT,
            error            TEXT,               -- broker rejection reason, if any
            raw              TEXT                -- JSON: last broker order payload
        );
        """
    )
    # Seed the singleton bot_config row (idempotent — never clobbers a toggle).
    sqlite.execute("INSERT OR IGNORE INTO bot_config (id, enabled, mode) VALUES (1, 0, 'proposal');")

    # --- Phase 13: two sleeves (swing + day) + portfolio optimizer ---
    # Tag every proposal/order with its sleeve so per-sleeve holdings can be
    # tracked independently on the one shared paper account (existing rows
    # default to 'swing'). bot_config gains a separate kill switch for the day
    # bot (it is always auto-exec, so no mode column).
    for stmt in (
        "ALTER TABLE bot_proposals ADD COLUMN sleeve TEXT NOT NULL DEFAULT 'swing'",
        "ALTER TABLE bot_orders ADD COLUMN sleeve TEXT NOT NULL DEFAULT 'swing'",
        "ALTER TABLE bot_config ADD COLUMN day_enabled INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            sqlite.execute(stmt)
        except sqlite3.OperationalError:
            pass  # duplicate column — already migrated
    # Optimizer snapshots: the capital split between sleeves, one row per run
    # (recompute replaces by minute). detail = JSON of the driving signals.
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS optimizer_snapshots (
            ts         TEXT PRIMARY KEY,
            swing_pct  REAL NOT NULL,
            day_pct    REAL NOT NULL,
            regime     TEXT,
            reason     TEXT,
            detail     TEXT
        );
        """
    )

    # --- Phase 2/3: day-trader decision journal + end-of-day review ---
    # Every SIGNAL the day trader sees each tick (acted, skipped, or blocked) is
    # journaled with the full market + portfolio context it decided against — the
    # transparency layer AND the training substrate for the learning loop. Nothing
    # is thrown away, so the analyzer can mine systematic mistakes after the fact.
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS day_signal_journal (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT NOT NULL,        -- decision time (UTC iso)
            run_id       TEXT NOT NULL,        -- one fast-loop tick sweep
            trade_date   TEXT NOT NULL,        -- YYYY-MM-DD session, for daily review
            symbol       TEXT NOT NULL,
            asset_class  TEXT,
            sector       TEXT,
            signal_kind  TEXT,                 -- momentum | reversion | none
            direction    TEXT,                 -- buy | sell | none
            strength     REAL,
            z            REAL,
            last_price   REAL,
            vwap         REAL,
            regime       TEXT,
            vol_pctile   REAL,
            market_bias  TEXT,                 -- plan bias (risk-on/off)
            decision     TEXT NOT NULL,        -- acted | skipped | blocked | hedge
            side         TEXT,
            notional     REAL,
            qty          REAL,
            conviction   REAL,                 -- segmentation rank score
            reason       TEXT,
            context      TEXT,                 -- JSON: market ctx + portfolio snapshot
            proposal_id  INTEGER,              -- bot_proposals link when acted
            outcome      TEXT,                 -- filled by review: win|loss|flat|open
            pnl          REAL                  -- realized/marked P&L, filled by review
        );
        """
    )
    sqlite.execute(
        "CREATE INDEX IF NOT EXISTS idx_day_journal_date ON day_signal_journal (trade_date);"
    )
    # One row per end-of-day review pass: stats, detected mistakes, and concrete
    # strategy-parameter suggestions (LLM + heuristics over the journal above).
    sqlite.execute(
        """
        CREATE TABLE IF NOT EXISTS day_review (
            trade_date  TEXT PRIMARY KEY,
            created_at  TEXT NOT NULL,
            stats       TEXT,                  -- JSON: win rates by setup/regime/symbol
            findings    TEXT,                  -- JSON: list of systematic-mistake findings
            suggestions TEXT,                  -- JSON: [{param, current, proposed, rationale}]
            summary     TEXT,                  -- narrative (LLM or deterministic fallback)
            model       TEXT
        );
        """
    )

    # Seed the default watchlist once (idempotent).
    sqlite.executemany(
        "INSERT OR IGNORE INTO watchlist (symbol, asset_class, display_name, sort_order) "
        "VALUES (?, ?, ?, ?)",
        [(s, a, n, i) for i, (s, a, n) in enumerate(DEFAULT_WATCHLIST)],
    )


def init_all(duck: DuckStore, sqlite: SqliteStore) -> None:
    init_duckdb(duck)
    init_sqlite(sqlite)
