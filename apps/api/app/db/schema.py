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

    # Seed the default watchlist once (idempotent).
    sqlite.executemany(
        "INSERT OR IGNORE INTO watchlist (symbol, asset_class, display_name, sort_order) "
        "VALUES (?, ?, ?, ?)",
        [(s, a, n, i) for i, (s, a, n) in enumerate(DEFAULT_WATCHLIST)],
    )


def init_all(duck: DuckStore, sqlite: SqliteStore) -> None:
    init_duckdb(duck)
    init_sqlite(sqlite)
