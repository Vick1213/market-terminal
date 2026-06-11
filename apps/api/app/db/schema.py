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

    # Seed the default watchlist once (idempotent).
    sqlite.executemany(
        "INSERT OR IGNORE INTO watchlist (symbol, asset_class, display_name, sort_order) "
        "VALUES (?, ?, ?, ?)",
        [(s, a, n, i) for i, (s, a, n) in enumerate(DEFAULT_WATCHLIST)],
    )


def init_all(duck: DuckStore, sqlite: SqliteStore) -> None:
    init_duckdb(duck)
    init_sqlite(sqlite)
