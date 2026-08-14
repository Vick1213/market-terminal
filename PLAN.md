<!-- Generated 2026-06-09 from an 8-agent research workflow + adversarial fact-check of every fragile free data source. Corrections from the fact-check are folded in. -->

# Personal Market Intelligence Terminal — Implementation Plan

*Lead architect synthesis. Target: solo retail investor, single-user, local-only on Apple Silicon (MPS). 100% free / scraping-only. Verified as of 2026-06-09.*

---

## 1. Executive Summary

We are building a **local-first, fully private market-intelligence terminal**: a Next.js dashboard backed by a single Python FastAPI process that ingests free public data (RSS, FRED, SEC EDGAR, CFTC, CBOE, FINRA, CCXT crypto streams, Reddit/StockTwits/Bluesky social), scores it with **local FinBERT + a local Qwen3 LLM** (zero cloud token cost), and presents six interlocking panels — News+Sentiment, Retail Market Score, Liquidity & Macro, Custom Watchlist, Multi-Asset Liquidity & Major Trades, and a Correlation Cookbook.

**Guiding principles:**
- **Free / scraping-only.** No paid APIs, ever. Every source below is verified free-and-working in June 2026, with a named fallback for every fragile one.
- **Local & private.** All inference (FinBERT, Qwen3) runs on the Mac's MPS. No data, headlines, or positions leave the machine. The only outbound traffic is read requests to public data sources.
- **Honest about latency & fragility.** Crypto microstructure is genuinely real-time; equities are delayed/aggregated proxies. The UI must badge each datum with its true freshness and source reliability. This is a *research/intelligence* terminal, **not** an execution system.

---

## 2. System Architecture

### 2.1 Topology

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                          MAC (Apple Silicon, MPS)                              │
│                                                                                │
│  ┌────────────────────────────┐         ┌──────────────────────────────────┐  │
│  │   FRONTEND  (Next.js 15,    │         │   BACKEND  (single Python proc)  │  │
│  │   App Router, :3000)        │         │   FastAPI + uvicorn  (:8000)     │  │
│  │                             │  REST   │                                  │  │
│  │  react-grid-layout (grid)   │◀───────▶│  ┌────────────────────────────┐  │  │
│  │  lightweight-charts (price) │  /api/* │  │ REST routers (per panel)   │  │  │
│  │  Recharts (gauges/heatmap)  │         │  └────────────────────────────┘  │  │
│  │  TanStack Query (poll/cache)│         │  ┌────────────────────────────┐  │  │
│  │  Zustand (UI state)         │   WS    │  │ WebSocket fan-out hub      │  │  │
│  │  useWebSocket hook          │◀═══════▶│  │ (ConnectionManager, topics)│  │  │
│  └────────────────────────────┘  /ws    │  └────────────────────────────┘  │  │
│                                          │  ┌────────────────────────────┐  │  │
│                                          │  │ APScheduler (AsyncIO)      │  │  │
│                                          │  │  cron/interval ingestors   │  │  │
│                                          │  └─────────────┬──────────────┘  │  │
│                                          │  ┌─────────────▼──────────────┐  │  │
│                                          │  │ Ingestion layer            │  │  │
│                                          │  │ httpx + aiolimiter +       │  │  │
│                                          │  │ tenacity + feedparser +    │  │  │
│                                          │  │ diskcache (ETag/cond-GET)  │  │  │
│                                          │  └─────────────┬──────────────┘  │  │
│                                          │  ┌─────────────▼──────────────┐  │  │
│                                          │  │ Inference (ThreadPool)     │  │  │
│                                          │  │  FinBERT (MPS, in-proc)    │  │  │
│                                          │  │  Qwen3 via MCP/Ollama      │  │  │
│                                          │  └─────────────┬──────────────┘  │  │
│                                          │  ┌─────────────▼──────────────┐  │  │
│                                          │  │ CCXT Pro asyncio tasks     │  │  │
│                                          │  │  watch_trades/order_book   │  │  │
│                                          │  └────────────────────────────┘  │  │
│                                          └────────────────┬─────────────────┘  │
│                                                           ▼                     │
│                              ┌──────────────────────────────────────────────┐  │
│                              │  STORAGE (embedded, no server)               │  │
│                              │  DuckDB  /data/market.duckdb  (time-series)  │  │
│                              │  SQLite  /data/app.db (WAL: state/dedupe/cur)│  │
│                              └──────────────────────────────────────────────┘  │
│                                                                                 │
│  OUTBOUND (read-only): FRED · SEC EDGAR · CFTC · CBOE CDN · FINRA CDN ·          │
│  Yahoo/Stooq · CCXT exchanges · Reddit/ApeWisdom · StockTwits · Bluesky · GDELT │
│  OUTBOUND (push): ntfy.sh (alerts)                                              │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Component decisions (opinionated)

| Layer | Choice | Why |
|---|---|---|
| **Repo** | uv (Python) + pnpm workspace monorepo: `/apps/web`, `/apps/api`, `/packages/shared`, `/data` (gitignored) | Keeps panel↔endpoint contracts in sync; generate TS types from FastAPI OpenAPI via `openapi-typescript`. |
| **Frontend** | Next.js 15 App Router; `react-grid-layout`; `lightweight-charts` (45 KB, TradingView, price/time-series); `recharts` (gauges/bars/heatmap); `@tanstack/react-query` (per-panel `refetchInterval`); `zustand` (UI state); thin native `useWebSocket` hook | All free/open. Lightweight-charts is financial-grade and tiny; TanStack Query's per-query staleTime maps 1:1 to panel cadences. |
| **Backend** | FastAPI + uvicorn, **single process** | One user → no Redis/Celery/Docker. Hosts scheduler, inference, WS hub, CCXT tasks together. |
| **Scheduler** | `APScheduler` `AsyncIOScheduler` in FastAPI `lifespan`; `max_instances=1` + `misfire_grace_time` + jitter per job | In-process, no broker. Blocking work (scrape/inference) offloaded to `run_in_executor`. |
| **Storage** | **DuckDB** (`/data/market.duckdb`) for ALL time-series (OHLCV, trades, FRED series, sentiment scores, retail-score history, COT) + **SQLite WAL** (`/data/app.db`) for app state (watchlist, dashboard layout, scraper cursors, ETags, news dedupe hashes) | DuckDB is columnar — sub-second rolling-window/correlation/resample queries that the Cookbook & Liquidity panels need, reads Parquet/Pandas directly. SQLite for tiny transactional state where DuckDB's single-writer model is awkward. **Serialize all DuckDB writes through the scheduler thread; use read-only connections for API reads.** |
| **Sentiment model serving** | `ProsusAI/finbert` loaded once at startup via `transformers` pipeline on `device='mps'`; results cached by text-hash in DuckDB so re-scoring is free. Qwen3 4B via existing MCP tool `mcp__aitochip-local-llm__ask_local_llm` (or Ollama `qwen3:4b` / MLX) for aspect/entity-level and low-confidence escalation. | Meets local+free constraint. See §3a for the routing recipe. |
| **HTTP/ingest** | `httpx` AsyncClient + `aiolimiter` (per-host token buckets) + `tenacity` (exp backoff on 429/5xx) + `feedparser` (all RSS) + `diskcache` (HTTP cache w/ ETag / If-Modified-Since). Global descriptive User-Agent: `MarketTerminal saatvik1213@gmail.com` | The single most important reliability investment. Conditional GET avoids re-downloading unchanged RSS; SEC mandates the UA. |
| **Alerting** | `ntfy.sh` (long random private topic) via `apprise` fan-out; daily LLM brief via Qwen3 | Free, no-signup, phone+desktop push. |

### 2.3 Data flow

1. **Scheduler** fires an ingestor on its cadence → 2. **Ingestion layer** does a rate-limited, cached, conditional GET → 3. raw rows written to **DuckDB** (single writer); cursors/ETags to **SQLite** → 4. **Inference** scores new text (FinBERT batch on MPS; escalate ambiguous/multi-entity to Qwen3) → scores persisted by text-hash → 5. **REST routers** serve panel snapshots from DuckDB on demand; **WS hub** pushes live crypto prints + freshly-scored news to subscribed channels → 6. **Frontend** renders; TanStack Query polls REST per-panel cadence, `useWebSocket` updates live charts; **alert bus** (z-score/correlation/insider/COT triggers) → ntfy + daily brief.

**Concurrency safety:** FinBERT + scrapes run on `ThreadPoolExecutor` so the asyncio loop (and WS streaming) never stalls; one DuckDB writer; supervised restart via `launchd`/`honcho`.

---

## 3a. Local Sentiment Model — the engine behind every panel

> **⚡ Model strategy — small, accurate, fast (decided 2026-06-09: NO large model on the bulk path).** Throughput beats parameter count here. Two workloads, two tools:
> - **Bulk path (~95% of items — every headline/post/comment, thousands/min):** a **small encoder only** — `ProsusAI/finbert` (110M): one forward pass → 3-class softmax. A generative LLM is the wrong tool at *any* size because token generation is 10–100× slower per item than an encoder classification. The M5 Pro just lets us batch large on MPS **and** run an ONNX-int8 copy across CPU cores in parallel → comfortably thousands of items/min.
> - **Accuracy without size:** ensemble two *small* models — FinBERT + `mrm8488/distilroberta-financial` (82M) — and confidence-gate (agree → trust; disagree → flag). Both <120M, both fast. Accuracy comes from **ensembling + calibration + explicit neutral handling**, not from a bigger model.
> - **Per-company sentiment with NO LLM:** *entity-localized scoring* — extract the sentence window around each ticker mention and run the same fast encoder on that span (see below). Covers the multi-entity drill-down cheaply.
> - **Tiny LLM only for the rare hard case (optional, off the hot path):** genuine sarcasm/irony on low-confidence *social* posts → a **≤4B** model (Qwen3-4B), run async on just the flagged minority — never the firehose.
> - **Large local model only where throughput is irrelevant:** the **once-a-day** auto-brief / cookbook narrative may use a bigger model since it runs once. Optional, fully isolated from every scoring loop.
> - Plus a small **embedding model** (`bge-small-en-v1.5`, 33M) for semantic dedup/clustering — tiny and fast. (Local **Whisper** for FOMC/earnings-call tone stays an optional batch job, never a throughput path.)

**Primary (document/sentence-level):** `ProsusAI/finbert` (110M params). Load with `AutoModelForSequenceClassification`, `device='mps'`. Batch 16–64 texts: `tokenizer(texts, padding=True, truncation=True, max_length=512, return_tensors='pt')`. **Always read `model.config.id2label`** before normalizing (finbert-tone has a *different* label order and will sign-flip).
- **Normalized score:** `score = p_positive − p_negative` (range −1..+1); **confidence = max(softmax)**.
- **Use OFF-THE-SHELF. Do NOT fine-tune** on large data — verified 2026 research (arXiv 2512.00946) shows up to −30% accuracy loss (catastrophic interference).

**Aspect/entity-level (per-company drill-down) — encoder-first, no LLM needed:** FinBERT can't target a specific entity, so *localize* instead: NER/regex-extract watchlist tickers, take the sentence window mentioning each ticker, and run FinBERT on that span → a per-company score from the same fast model. Only when a span is genuinely ambiguous (multiple tickers in one sentence, heavy sarcasm) do we **optionally** escalate that single item to a **≤4B** local LLM (Qwen3-4B via `ask_local_llm`/Ollama/MLX) with structured JSON output — on the flagged minority, off the hot path.

**Routing recipe (keeps it fast & free):**
- High FinBERT confidence → use FinBERT score (ms-latency).
- Low confidence (near-neutral) OR multi-entity OR social slang → first try entity-localized FinBERT; escalate only the still-ambiguous remainder to the optional ≤4B LLM (async, off the hot path).
- Batch ≥ 8 → MPS; singletons → CPU (optionally ONNX int8 via `optimum[onnxruntime]`, ~2–4× CPU speedup) — at batch=1 MPS dispatch overhead makes it *slower* than CPU.

**Performance budget (M5 Pro / 64 GB):** FinBERT ~5–15 ms/article on MPS, **hundreds–thousands/sec batched**; an ONNX-int8 copy adds parallel CPU throughput. The optional ≤4B LLM runs only on the flagged minority (low-confidence/ambiguous social), and the larger brief model runs once a day — so the firehose is always encoder-speed, never token-generation-bound.

**Track velocity & dispersion, not just level:** rolling mean + std of −1..+1 scores per ticker. A sharp shift or a spike in cross-article *disagreement* is often more predictive than the absolute level. **Handle the neutral class explicitly** (weight by confidence; FinBERT over-predicts neutral on short social messages) or it buries signal.

---

## 3. The Panels

### (a) News + Sentiment

- **Purpose:** unified, deduped, per-ticker + market-wide news timeline, each item FinBERT-scored, with entity-level drill-down via Qwen3.
- **Data sources (free, 2026-verified):**
  - **Yahoo Finance per-ticker RSS** — `https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL&region=US&lang=en-US` (comma-separate tickers). Articles arrive pre-mapped to the ticker, no entity resolution needed. ⚠️ **Unofficial and NOT independently confirmed by the fact-check — Yahoo is actively paywalling adjacent endpoints, so treat as fragile and verify the exact URL returns items at build time.** **Finnhub `/company-news` + SEC EDGAR are the guaranteed per-ticker fallbacks** so a Yahoo withdrawal degrades gracefully.
  - **Finnhub free tier** — `/company-news?symbol=AAPL&from=...&to=...` and `/news?category=general`. 60 calls/min, clean JSON pre-tagged to ticker + dedup IDs. Free key.
  - **SEC EDGAR** — `data.sec.gov/submissions/CIK##########.json` + structured-disclosure RSS (10-min refresh) + full-text search (`efts.sec.gov`). Authoritative 8-K/press-release source; never paywalls. **Requires descriptive UA; ≤10 req/s.** Map ticker→CIK once via `company_tickers.json`. Lib: `edgartools`.
  - **GDELT DOC 2.0** — `https://api.gdeltproject.org/api/v2/doc/doc?query=...&mode=ArtList&format=json&timespan=1d`. Only free no-key broad-market firehose (65+ languages). **HARD LIMIT: 1 req / 5 s — single global token bucket; query per sector/macro theme, NEVER per-ticker.** `ToneChart`/`TimelineVol` modes give free pre-computed tone/volume curves.
  - **CNBC / MarketWatch / Investing.com / Seeking Alpha per-symbol RSS** (`seekingalpha.com/api/sa/combined/SYMBOL.xml`) — breadth + analyst angles. **Set a real browser UA** (CNBC/MW 403 naive fetchers).
  - **AVOID:** Reuters RSS (DEAD, killed early 2026); NewsAPI.org free (localhost-only + 24h delay); Alpha Vantage NEWS_SENTIMENT (25 req/day total); Tiingo news (gated). Marketaux free is supplementary only (100/day, ~3 articles/req).
- **Refresh cadence:** Yahoo/Finnhub per-watchlist-ticker RSS every 5–15 min (conditional GET via ETag); EDGAR submissions RSS every 10 min; GDELT macro/sector queries on a slow 5-s-spaced rotation; CNBC/MW topic feeds every ~5 min.
- **Sentiment model use:** FinBERT scores headline+summary on ingest (cached by hash → never re-scored). Multi-entity macro articles → Qwen3 per-entity so one "chip export ban" article can move NVDA negative and domestic-fab names positive. Cross-check GDELT's free tone vs FinBERT — divergence flags nuance.
- **Key UI:** live news ticker (WS-pushed on ingest); per-ticker timeline with sentiment color + confidence; sentiment velocity sparkline; "multi-outlet convergence" badge (≥N independent outlets on one deduped story); EDGAR 8-K alert chips (Item 1.05 / 2.02 / 8.01).
- **Dedup (mandatory):** normalize URL (strip UTM/query) + title fuzzy-hash (SimHash/MinHash) within a time window — else sentiment counts inflate.

### (b) Retail Market Score (X / Reddit / StockTwits / Bluesky)

- **Purpose:** market-wide retail "risk-on/off" + per-company drill-down, **led by mention-volume SPIKE** (the strongest retail signal), modulated by FinBERT polarity.
- **Data sources (free, 2026-verified):**
  - **ApeWisdom** (keyless) — `https://apewisdom.io/api/v1.0/filter/{wallstreetbets|stocks|all-crypto|...}/page/{n}`. Returns `mentions`, `mentions_24h_ago`, `rank`, `rank_24h_ago`, `upvotes`. **NO sentiment** — mentions/upvotes only. Does the hard Reddit scraping + cashtag extraction for you. Updated ~2×/hr.
  - **StockTwits public JSON** (keyless, **fragile/Cloudflare**) — `https://api.stocktwits.com/api/2/streams/symbol/{SYM}.json`. Only major source with explicit human Bullish/Bearish tags (~30–50% of msgs). Poll only symbols ApeWisdom flags as spiking; realistic UA; 403/429 backoff; skip-on-fail.
  - **Bluesky AT Protocol firehose** (free, keyless reads — *verified 2026: no paid tier, no per-call fee, firehose needs no auth*). Cashtags shipped Jan 2026. `pip install atproto`; subscribe to firehose, filter `$CASHTAG` regex. **Best free X replacement.** Medium reliability only because finance community is still smaller than WSB.
  - **Reddit official API via PRAW** — ⚠️ **treat as likely-unavailable in 2026.** Fact-check (2026-06-09): Reddit closed self-service API access in Nov 2025, and the *Responsible Builder Policy* (updated 2026-06-05) now requires explicit approval before any API access; multiple June-2026 reports say OAuth itself is being closed to personal scripts absent an exception. **Do not architect on PRAW.** If you happen to hold working credentials, use ≤60 req/min for raw post/comment text — otherwise carry the entire Reddit signal through **ApeWisdom** (mention volume) + **Bluesky/StockTwits** (text you score yourself).
  - **Google Trends via `pytrends`** (FRAGILE — archived Apr 2025, random 429s) — orthogonal search-interest confirmation. Heavy caching, never on critical path.
  - **DEAD — do not design around:** free X/Twitter API (pay-per-use ~$0.005/read since Feb 2026), snscrape, Nitter, Pushshift (moderator-only).
- **Refresh cadence:** ApeWisdom every 15–30 min; StockTwits/Bluesky only for spiking tickers; PRAW every ~5 min (<60 req/min); pytrends slow + cached.
- **Sentiment model use:** FinBERT (bulk) over StockTwits/Bluesky/Reddit bodies; StockTwits human tags as ground-truth ratio; Qwen3 for sarcasm/multi-ticker WSB irony on top-N spiking names + a one-line "why it's trending" blurb for the drill-down. Add a slang preprocessing layer.
- **Key UI:** market-wide **Retail Risk-On/Off gauge** (aggregate bull/bear + total chatter volume); leaderboard ranked by **mention z-score & rank-velocity** (not absolute mentions — NVDA/TSLA are always-top noise); per-company drill: spike chart, bull/bear ratio, cross-source confirmation badge, sentiment-vs-volume **divergence** flag.
- **Anti-gaming (mandatory):** dedup near-identical text (MinHash); drop accounts < N days old / extreme post-rate; cap any single author per ticker; **require multi-source confirmation** before trusting a spike. Validate extracted tickers against an authoritative symbol list (kill `$A/$ON/$IT/$ALL/$GO/$DD` collisions; prefer `$`-cashtags).

### (c) Market Liquidity & Macro Indicators

- **Purpose:** single composite **Risk-On/Off score (−100..+100)** with transparent sub-buckets, anchored on bulletproof FRED data.
- **Data sources (free, 2026-verified):**
  - **FRED API** (free key, ~120 req/min) — the backbone. Series:
    - *Yield curve:* `T10Y2Y`, `T10Y3M`, `DGS10`, `DGS2`
    - *Real/breakeven:* `DFII10`, `T10YIE`
    - *Financial conditions:* `NFCI`, `ANFCI` (+ `NFCIRISK`/`NFCICREDIT`/`NFCILEVERAGE`)
    - *Credit:* `BAMLH0A0HYM2` (HY OAS), `BAMLC0A0CM` (IG OAS)
    - *Fed plumbing / net liquidity:* `WALCL` − `RRPONTSYD` − `WTREGEN`(TGA); `WRESBAL` (use this, **not** discontinued `EXCSRESNS`)
    - *Money/rates:* `M2SL`, `M2REAL`, `DFF`, `SOFR`; `VIXCLS` (VIX backup)
  - **CBOE CDN CSVs** (no auth) — `cdn.cboe.com/api/global/us_indices/daily_prices/{VIX,VIX3M}_History.csv` (DATE,OHLC) → compute **`term_structure = VIX3M/VIX`** (ratio<1 = backwardation = stress; high-signal regime gauge most dashboards omit). Put/call: `cdn.cboe.com/resources/options/volume_and_call_put_ratios/{totalpc,equitypc,indexpc}.csv` (+ `*pcarchive.csv` for history). **Ignore the 2019-frozen bulk file.**
  - **FINRA daily short-sale volume** (no auth) — `https://cdn.finra.org/equity/regsho/daily/CNMSshvol[YYYYMMDD].txt` (pipe-delimited). Market-wide `short_volume/total_volume` = risk-appetite proxy. *This is short SELL volume incl. MM hedging, NOT short interest.* Handle 404 on weekends/holidays.
  - **NAAIM Exposure Index** — free full-history Excel at `naaim.org/programs/naaim-exposure-index/` (latest 86.82, 2026-06-04). Weekly (Thursdays).
  - **AAII sentiment** — current weekly bull/neutral/bear public at `aaii.com/sentimentsurvey`; **full history `sentiment.xls` is member-gated** → append your own weekly prints.
  - **Breadth** — NO clean free API. **Self-compute** `%>200dma` and A/D from a free daily-close universe (Stooq bulk `stooq.com/db/`) for full control. Fallback (low reliability): scrape StockCharts `$SPXA200R`/`$NYAD`.
  - **CNN Fear & Greed** (FRAGILE overlay only) — `production.dataviz.cnn.io/index/fearandgreed/graphdata/[YYYY-MM-DD]`. **Requires browser UA + `Accept: application/json`** (returns HTTP 418 bare). Sanity check, not a dependency — you can reconstruct it from VIX/put-call/breadth/HY.
- **Refresh cadence:** FRED nightly cron (~16:30 ET) + on-demand; CBOE/FINRA daily after close; NAAIM/AAII weekly Thursdays. **Forward-fill weekly→daily; use most-recently-RELEASED value (no look-ahead).**
- **Sentiment model use:** none directly; Qwen3 optionally writes the regime-banner narrative.
- **Composite (the edge):** z-score each input over trailing 1–2yr, sign-align (+ = risk-on), weight-average: **Volatility 25%** (VIX inv, VIX3M/VIX term +, MOVE inv), **Credit 20%** (HY OAS inv), **Financial conditions 20%** (NFCI inv), **Breadth 20%**, **Sentiment/positioning 15%** (NAAIM + contrarian AAII + put/call inv). **Display sub-bucket contributions** so you see *what* drives the regime.
- **Key UI:** big composite gauge + stacked sub-bucket bars; VIX3M/VIX term-structure line with the 1.0 crossover alert; net-liquidity overlay vs SPY/BTC; contrarian-extreme alert (NAAIM bottom decile + AAII bears≫bulls + elevated put/call).

### (d) Custom Watchlist

- **Purpose:** per-symbol command row — quote/OHLCV, day change, latest headline + sentiment badge, retail-spike badge, regime context.
- **Data sources (free, 2026-verified):**
  - **Stooq CSV** (PRIMARY for EOD/daily history) — `https://stooq.com/q/d/l/?s=^spx&i=d` (URL-encode `^`; newest-first; detect the plain-text `"Exceeded the daily hits limit"` body and back off). Avoids Yahoo IP bans.
  - **yfinance** (delayed ~15–20 min, FRAGILE — rate-limited/IP-banned 2026) — intraday quotes only; cache hard, batch, sleep 1–2 s, `try/except` → Stooq fallback. **Note DXY ticker is `DX-Y.NYB`, not `DXY`.**
  - **CCXT** for crypto rows (see panel e). Reuse News (a) + Retail (b) tables for headline/sentiment/spike badges.
- **Refresh cadence:** price 60–120 s during market hours (cached), longer off-hours.
- **Sentiment model use:** reuses the FinBERT score already computed in panel (a); shows a colored badge + Qwen3 one-liner on click.
- **Key UI:** sortable table; per-row `lightweight-charts` mini-chart; badges for sentiment, retail-spike, insider-cluster (from extras), regime; "delayed" label on equity prices.

### (e) Multi-Asset Liquidity & Major Trades (stocks / crypto / gold / silver)

- **Purpose:** the only **genuinely real-time** panel — live crypto large-print tape + order-book depth — plus honest delayed/aggregated proxies for equity & metals flow.
- **Data sources (free, 2026-verified):**
  - **CCXT (incl. free CCXT Pro WebSockets, merged into base — *verified 2026*)** — `import ccxt.pro as ccxtpro; ex = ccxtpro.binance(); await ex.watch_trades(sym)` / `watch_order_book(sym)`. **Public endpoints, no key.** THE real "major trades" source: threshold on notional (>$250k) or rolling z-score of trade size → true large-print feed; order-book imbalance for liquidity walls. Stream BTC/ETH on 2–3 top exchanges (Binance/Coinbase/Kraken).
  - **gold-api.com** (keyless, unlimited — *verified*) — `https://api.gold-api.com/price/{XAU|XAG}` spot. **metals.dev** free (≤60s delay, monthly quota) as backup.
  - **yfinance / Stooq** — futures `GC=F`/`SI=F`, ETFs GLD/SLV (flow proxy), `DX-Y.NYB`, `^GSPC`/`^IXIC`. yfinance **options chain** (`Ticker.option_chain`) → unusual-options proxy (today's volume vs trailing-avg OI).
  - **FINRA daily short-sale volume** (per-ticker) + **FINRA OTC/ATS off-exchange "dark pool" weekly aggregates** (`otctransparency.finra.org`, **weekly, ~4-week delay** — positioning context, NOT intraday prints).
  - **CoinGecko Demo** (free key, 100/min, 10k/mo) + **CoinPaprika** (keyless backup) — cross-exchange aggregate volume/dominance to pick which CCXT markets to stream.
  - **Etherscan free tier** (FRAGILE — Ethereum-mainnet-only after 2026 multichain cuts; 5/s, 100k/day) — optional DIY ETH exchange-netflow from labeled hot-wallet addresses.
  - **DEAD for free:** Glassnode / CryptoQuant / Whale-Alert (no usable free tier).
- **Refresh cadence:** crypto = streaming (WS, sub-second); metals spot every 30–60 s; futures/ETF = yfinance cadence; FINRA short-vol T+1 daily; OTC/ATS weekly.
- **Sentiment model use:** FinBERT/Qwen3 as the *explanatory layer* — when a headline cluster turns strongly +/−, check flow proxies for confirming/diverging prints (divergence = high-value contrarian setup).
- **Key UI:** **green "LIVE" badge for crypto prints vs yellow "PROXY/DELAYED" badge for equity/metals** (honesty is a feature). Live crypto large-print tape + depth/imbalance chart; metals **spot-vs-futures basis** + GLD/SLV volume z-score + off-exchange % trend; equity **"accumulation score"** = blend(unusual-volume z, FINRA short-vol trend, off-exchange % trend, options OI surge).
- **Redundancy by design:** yfinance↔Stooq, gold-api↔metals.dev, CoinGecko↔CoinPaprika — no single break blinds an asset class.

### (f) Correlation Cookbook

- **Purpose:** 12 live intermarket rule-cards, each showing whether the textbook relationship **HOLDS or is BROKEN right now**, with auto regime detection.
- **Data sources:** **FRED** (macro/yields/spreads/DXY/balance sheets — bulletproof half) + **CoinGecko Demo** (BTC/crypto) + **Stooq via `pandas-datareader`** (equity/FX/commodity price legs — `pdr.DataReader('^SPX','stooq',...)`). **Do NOT build history on yfinance** (historical OHLC paywalled/fragile in 2026). Correlations computed **in DuckDB** from stored series — no external source on the critical path.
- **Refresh cadence:** recompute every 15 min or on-demand from cached series.
- **Sentiment model use:** Qwen3 auto-writes the plain-English "why is this broken right now" blurb per card — **commentary only, never the trigger** (the z-score/sign-flip is the source of truth; LLMs hallucinate causality).
- **Key UI:** rule-card grid (green=HOLDS, yellow=WATCH, red=BROKEN); per card show rolling 30d + 90d Pearson corr (on **log-returns**, not levels), long-run baseline mean±σ, **z-score of current corr**, and for ratio pairs a z-score of the ratio; correlation heatmap; regime banner header. See §4.

---

## 4. Correlation Cookbook Content

**Regime banner (June 2026 default, hard-coded):** *USD soft (DXY ~103–107), real yields elevated, HY spreads ultra-tight (~275–300 bp), 2s10s un-inverted to ~+50 bp (late-cycle steepening), gold structural bull (~$5,300–5,589 on CB buying), BTC weak (~$88k, −20% YTD).* → **"Risk-on but late-cycle, gold-bid, BTC-as-leveraged-tech."**

| # | Pair (series) | Normal sign / lead-lag | Rationale | How computed | Regime where it BREAKS | June-2026 status |
|---|---|---|---|---|---|---|
| 1 | **Gold vs BTC** (CoinGecko BTC, Stooq XAUUSD) | weak + ("hard money") | both inflation/debasement hedges | 90d corr of log-rets; z of corr | CB-driven gold bull + BTC trading as risk asset | **BROKEN** — 1yr corr ~−0.17, spiked to −0.88 |
| 2 | **Copper/Gold ratio vs 10y yield** (Stooq HG.F, XAUUSD; FRED `DGS10`) | + (~0.85 hist), copper/gold *leads* | growth proxy → yields | corr of ratio vs yield; lead-lag scan | gold inflated by CB buying + China copper-demand slump; yields up on deficits not growth | **BROKEN** — 60d corr ~0.08 |
| 3 | **BTC vs Nasdaq** (CoinGecko, Stooq ^NDX) | + (risk asset) | same institutional rebalancers | 30d+90d corr | sustained 30d corr <0.20 over 60d = decouple | **HOLDS/TIGHT** — hit ~0.94–0.96 |
| 4 | **USDJPY vs risk** (Stooq USDJPY; FRED `VIXCLS`; ^NDX) | +0.78 to Nasdaq, −0.92 to VIX; USDJPY *leads* | yen carry funds risk | corr + lead-lag | sharp yen RALLY → carry unwind → VIX spike (Aug-2024 replay; ~$500B carry outstanding) | **HOLDS — keep armed** |
| 5 | **HY spreads vs equities** (FRED `BAMLH0A0HYM2`, SPX) | − (tight spreads = risk-on); credit *leads* at turns | credit prices default risk | corr; overlay HY-inv on SPX | spreads WIDEN while SPX still rising = warning | **HOLDS but hidden divergence** — ~2.75% (tightest since '07) yet ~4.2–4.5% realized defaults → "credit complacency" sub-flag |
| 6 | **2s10s curve** (FRED `T10Y2Y`, `T10Y3M`) | inversion → recession; **un-inversion = imminent** | term-premium/growth | level + slope | bull-steepener after long inversion = late-cycle warning | **WATCH** — 2s10s ~+50 bp; 3m10y flipped negative again |
| 7 | **Gold/Silver ratio** (Stooq XAUUSD/XAGUSD) | risk-off thermometer | silver = industrial+precious | z of ratio | >90–100:1 = stress (1991/Mar-2020) | **NEUTRAL** — ~61–62:1 (mid-range) |
| 8 | **Net liquidity vs BTC/SPX** (FRED `WALCL`−`RRPONTSYD`−`WTREGEN`) | + with **~5–6 wk lag**; liquidity *leads* | CB liquidity → risk assets | resample weekly, lag BTC 4–6 wk, dual-axis | QT/draining liquidity, reserve cliff | **HOLDS** — WALCL ~$6.9T post-QT |
| 9 | **Real yields (DFII10) vs gold & growth equities** | − (rising real yields = headwind) | opportunity cost | corr; track DFII10 daily | gold decouples on CB buying (current!) | **PARTIAL** — gold ignoring high real yields |
| 10 | **DXY vs commodities/EM** (FRED `DTWEXBGS`*) | − | priced in USD | corr | *`DTWEXBGS`=BROAD USD ≠ ICE DXY; source true DXY from Stooq `^DXY`, label FRED one "broad USD"* | HOLDS |
| 11 | **VIX term structure** (CBOE VIX3M/VIX) | >1 calm / <1 stress; *leads composite* | vol risk premium | ratio crossover alert | crosses <1.0 = early risk-off | regime trigger |
| 12 | **Oil vs breakevens** (FRED `DCOILWTICO`, `T10YIE`) | + | energy → inflation expectations | corr | supply-shock spikes break it | context |

### Auto regime-detection / correlation-break design

- **Regime classifier (header):** 4 free FRED dials — real yields `DFII10` ↑/↓, broad USD `DTWEXBGS` ↑/↓, HY OAS `BAMLH0A0HYM2` tight/wide, `T10Y2Y` steepening/inverting → emit a single daily tag (`risk-on / neutral / risk-off / stress`). Every card reads this tag.
- **Per-card BROKEN detector:** store long-run mean & σ of the rolling-90d correlation. Each day compute `z = (corr_now − mean)/σ` **and** test `sign(corr_now) != sign(baseline)`. **Fire BROKEN (red)** on sign-flip OR `|z|>2`; **WATCH (yellow)** on `|z|>1`. Default cards #1 and #2 to BROKEN today.
- **Lead-lag scanner:** for lead-lag pairs (copper/gold→10y, net-liq→BTC, USDJPY→VIX) compute cross-correlation over lags −20..+20d; surface peak-`|corr|` lag. If the leading series stops leading (peak lag → 0 or flips) = early decay warning.
- **Divergence alerts:** when a *mean-reverting* pair's z-scores diverge >2σ, flag a potential reversion — **but gate it on the regime banner** so you don't fade a structural break (the copper/gold break is structural, not a fade).
- **Stress radar:** composite that lights when USDJPY rolls over + VIX term structure inverts + gold/silver ratio rises (the Aug-2024 confluence).
- **Hygiene:** use **log-returns** for correlation (levels create trend artifacts); align FRED/Stooq/24-7-crypto on a common business-day calendar + forward-fill before computing; minimum window to avoid 30d noise.

---

## 5. Free Data Source Master Table (2026 status)

> **Fact-check corrections (independent adversarial verification, 2026-06-09):**
> - **Reddit** is worse than a plain "Medium" — see the Low / likely-unavailable rating below; route the Reddit signal through ApeWisdom.
> - **yfinance history is not fully dead:** Yahoo paywalled the *browser CSV download*, but the **v8 chart JSON endpoint yfinance actually uses still returns free history** (rate-limited). Net guidance stands: Stooq primary, yfinance throttled-secondary.
> - **GDELT "1 req / 5 s"** is a *prudent self-imposed throttle, not an official spec* — but a **descriptive User-Agent is required** or you get rate-limited even at low volume.
> - **Etherscan** free-multichain cut was announced **Nov 2024** (in effect through 2026), not 2026; for free multichain consider **Routescan** (also 5/s, 100k/day).
> - **Marketaux "~3 articles/request"** is **unverified** — check the free-tier `limit`/`returned_limit` param yourself.
> - **FINRA `CNMSshvol` filename** and **Yahoo per-ticker RSS** URLs are unofficial — **verify both return data at build time.**

| Source | What | Cost | Reliability | Used by panel | 2026 status |
|---|---|---|---|---|---|
| **FRED API** | Macro/yields/spreads/DXY/balance-sheet series | Free (key) | **High** | c, f, extras | VERIFIED — `RRPONTSYD`/`WRESBAL`/`WALCL` updating to 2026-06-05; ~120 req/min |
| **SEC EDGAR** (`data.sec.gov`, `edgartools`) | Filings, 8-K, Form 4, 13F | Free (no key) | **High** | a, extras | VERIFIED — 10 req/s, descriptive UA mandatory (403 without) |
| **CBOE CDN CSV** | VIX/VIX3M history, put/call ratios, delayed options chain (GEX) | Free (no key) | High / *Medium for options JSON* | c, e, extras | VERIFIED — VIX CSV live; options JSON unofficial/schema-fragile |
| **FINRA CDN** | Daily short-sale volume; OTC/ATS off-exchange weekly | Free (no key) | **High** | c, e | VERIFIED — `CNMSshvol[YYYYMMDD].txt`; short SELL vol ≠ short interest; OTC ~4-wk delay |
| **CFTC** (`publicreporting.cftc.gov`, `cot_reports`) | Weekly COT positioning | Free | **High** | extras | VERIFIED — Socrata API + bulk CSV |
| **CCXT (+ free Pro WS)** | Crypto L2 books, live trade tape, OHLCV | Free (no key, public) | **High** | d, e | VERIFIED 2026 — Pro WebSockets merged into free base; the live "major trades" source |
| **Yahoo per-ticker RSS** | Per-company headlines | Free | **High** | a | VERIFIED live 2026-06-09 |
| **Finnhub free** | Per-company news JSON | Free (key) | High | a | VERIFIED — 60/min |
| **GDELT DOC 2.0** | Broad-market news firehose + tone | Free (no key) | Medium | a | VERIFIED — **1 req / 5 s** hard limit |
| **ApeWisdom** | Reddit ticker mention counts/upvotes | Free (no key) | High* | b | VERIFIED — *NO sentiment*; single small operator (SPOF) |
| **StockTwits public JSON** | Per-symbol Bull/Bear msgs | Free (no key) | **Fragile** | b | Works but Cloudflare-gated, can 403/break |
| **Bluesky AT firehose** (`atproto`) | Live social + cashtags | Free (no key for reads) | Medium | b | VERIFIED 2026 — no paid tier; cashtags Jan 2026; best free X replacement |
| **Reddit API / PRAW** | Raw post/comment text | Free (OAuth) | **Low / likely-unavailable** | b | Self-service closed Nov-2025; Responsible Builder Policy (2026-06-05) needs approval; OAuth reportedly closing to personal scripts → route Reddit via ApeWisdom |
| **gold-api.com** | Gold/silver spot | Free (keyless, unlimited) | **High** | e | VERIFIED — no auth, no rate limit |
| **metals.dev** | Metals spot (backup) | Free (key) | High | e | VERIFIED — ≤60s delay, monthly quota |
| **CoinGecko Demo** | Crypto aggregate market data | Free (key) | High | e, f | VERIFIED — 100/min, 10k/mo |
| **CoinPaprika** | Crypto aggregate (backup) | Free (keyless) | Medium | e | VERIFIED — ~1k req/day |
| **Stooq CSV** | EOD index/ETF/FX/commodity history | Free (no key) | Medium | c, d, e, f | VERIFIED — EOD only, undisclosed daily quota ("Exceeded daily hits limit") |
| **NAAIM** | Weekly manager exposure | Free | High | c | VERIFIED — 86.82 on 2026-06-04 |
| **AAII** | Weekly retail sentiment | Free (current) / gated (history) | Medium | c | Current public; `sentiment.xls` member-gated |
| **CNN Fear & Greed** | 0–100 composite | Free (unofficial) | **Fragile** | c | Live but 418 w/o browser UA; overlay only |
| **yfinance** | Equity/ETF/futures quotes + options chain | Free (scraper) | **Fragile** | d, e | Rate-limited/IP-banned 2026; `DX-Y.NYB`; Stooq fallback |
| **Etherscan free** | ETH on-chain | Free (key) | **Fragile** | e | ETH-mainnet only post-2026 cuts; 5/s, 100k/day |
| **ntfy.sh** | Push alerts | Free (open-source) | High | all | VERIFIED — use long random topic |
| **ProsusAI/finbert** | Local sentiment | Free (open weights) | **High** | a, b, e, f | VERIFIED — 6.77M dl/mo, MPS-ready |
| **Qwen3 4B (MCP/Ollama/MLX)** | Local LLM aspect/fallback | Free (local) | High | a, b, f, extras | VERIFIED — available via MCP |
| **DEAD — do not use** | Reuters RSS · free X API · snscrape · Nitter · Pushshift · NewsAPI.org free · Alpha Vantage NEWS_SENTIMENT · Glassnode/CryptoQuant/Whale-Alert free | — | dead/paywalled | — | Confirmed unavailable/crippled 2026 |

---

## 6. Recommended Extra Edge Features (ranked by edge-per-effort)

| Rank | Feature | Edge | Free source | Effort |
|---|---|---|---|---|
| 1 | **Daily LLM auto-brief** (Qwen3 via MCP) | Synthesizes regime + alerts + insider/COT/GEX into a 30-sec morning read — the feature you'll actually use daily; force-multiplies everything else | local Qwen3 ($0 tokens) | **Low** — APScheduler pre-market job feeding a grounded JSON digest |
| 2 | **Market + volatility-regime classifier** | Master risk-on/off switch that gates every other signal (only act on longs in risk-on; tighten in stress) | FRED `NFCI`+`BAMLH0A0HYM2`+`VIXCLS`+`T10Y2Y` + VIX3M/VIX | **Low** — reuses panel (c) |
| 3 | **Z-score anomaly + correlation-break alerting** | Anomalies precede moves; catches regime shifts (gold↔real-yields decoupling) early | self-computed on stored series | **Low** — pandas rolling on data you already have |
| 4 | **SEC Form 4 insider cluster-buy tracker** | Open-market BUYS (code `P`) + clusters (multiple insiders / CEO-CFO after drawdown) precede outperformance; few free tools surface the *pattern* | `data.sec.gov` + `edgartools` | **Low-Med** — filter code=P, detect clusters |
| 5 | **Seasonality + relative-strength / sector-rotation (RRG)** | RS rotation shows where money flows before headlines; RRG quadrants are a pro tool retail lacks | pure math on cached OHLC | **Low-Med** |
| 6 | **CFTC COT positioning + COT Index** | What big money does in futures (gold/silver/index/crypto); percentile extremes flag crowded mean-reversion | `cot_reports` lib (Socrata) | **Low-Med** — weekly Friday refresh |
| 7 | **ntfy.sh push alerting** | Converts passive dashboard → timely edge (act on spike when it happens) | ntfy.sh / apprise | **Low** |
| 8 | **Self-computed GEX / dealer positioning** | Gamma-flip + call/put walls = concrete S/R + vol-regime read invisible to price-only tools | CBOE delayed options JSON (`_SPX.json`) | **Med** — FRAGILE endpoint, isolate behind one adapter + schema validation |
| 9 | **13F whale-holdings tracker** | New initiations by respected managers = conviction context | EDGAR `edgartools` | **Med** — ~45-day lag (context not timing) |
| 10 | **Economic + earnings calendar** | Warns not to act into binary event risk (CPI/FOMC/earnings) | FRED `/release/dates` (clean); earnings via EDGAR (scraping fragile) | **Med** |
| 11 | **Backtesting harness** | Separates edge from curve-fitting; re-run quarterly to catch decay | `vectorbt` + `backtesting.py` | **Med-High** — add once signals exist; guard look-ahead (shift +1 bar) / survivorship |

---

## 7. Phased Build Roadmap (solo-dev realistic)

**Phase 0 — Scaffold (½–1 wk).** uv+pnpm monorepo; FastAPI `lifespan` with APScheduler started; DuckDB + SQLite files + schema (`source, symbol, ts` time-series tables; dedupe table; layout/watchlist/cursor tables). httpx+aiolimiter+tenacity+diskcache ingest layer with global UA. Next.js shell + react-grid-layout + one dummy panel + TanStack Query + `useWebSocket` + WS hub `ConnectionManager`. `openapi-typescript` type-gen. `launchd`/`honcho` supervised start. **Delivers:** running skeleton, one live REST + one WS round-trip.

**Phase 1 — Sentiment engine + News panel (1–1.5 wk).** Load FinBERT on MPS in a ThreadPool; `/sentiment` endpoint; text-hash cache. Yahoo per-ticker RSS + Finnhub + EDGAR ingestors; URL+title dedup; score-on-ingest; WS news push. **Delivers:** Panel (a) end-to-end + the reusable scoring service every other panel needs. *(Depends on P0.)*

**Phase 2 — Macro/Liquidity + Regime classifier (1 wk).** FRED nightly pull (all series); CBOE/FINRA/NAAIM ingestors; composite Risk-On/Off score + sub-buckets; regime tag. **Delivers:** Panel (c) + the regime tag that gates extras. *(Depends on P0; independent of P1.)*

**Phase 3 — Watchlist + price plumbing (½–1 wk).** Stooq-primary / yfinance-fallback fetcher with quota detection; CCXT REST for crypto quotes; reuse P1/P2 for badges. **Delivers:** Panel (d). *(Depends on P1, P2.)*

**Phase 4 — Multi-Asset Liquidity & Major Trades (1–1.5 wk).** CCXT Pro asyncio `watch_trades`/`watch_order_book` tasks → large-print threshold/z-score → WS push; gold-api/metals.dev spot; futures/ETF/options-OI proxies; FINRA short-vol + OTC/ATS; CoinGecko/CoinPaprika aggregates. Green/yellow freshness badges. **Delivers:** Panel (e) — the live panel. *(Depends on P0, P3.)*

**Phase 5 — Retail Market Score (1–1.5 wk).** ApeWisdom volume/spike → leaderboard by z-score/rank-velocity; StockTwits + Bluesky firehose text → FinBERT; PRAW if creds exist; anti-bot filters + multi-source confirmation; per-company drill with Qwen3 blurb. **Delivers:** Panel (b). *(Depends on P1.)*

**Phase 6 — Correlation Cookbook (1 wk).** DuckDB rolling-corr/z-score/lead-lag engine over stored series; 12 rule-cards + auto-BROKEN detector + regime banner + Qwen3 commentary. **Delivers:** Panel (f). *(Depends on P2, P3, P4 for the price/crypto legs.)*

**Phase 7 — Edge extras + alerting + brief (1–2 wk, incremental).** In edge-per-effort order: regime gating → z-score/correlation alerts → ntfy → Form 4 cluster buys → seasonality/RS-RRG → COT → GEX adapter → daily Qwen3 auto-brief. **Delivers:** the daily-driver layer. *(Depends on all prior.)*

**Phase 8 — Backtesting harness (when signals stabilize).** `vectorbt`/`backtesting.py`; validate each cookbook rule before trusting; re-run quarterly. *(Depends on P6, P7.)*

*Critical path: P0 → P1 → (P2 ∥ P5) → P3 → P4 → P6 → P7. P2 can proceed in parallel with P1.*

---

## 8. Risks & Mitigations

- **yfinance fragile (rate-limit/IP-ban 2026).** → Stooq primary for history; yfinance intraday-only with hard cache + batch + 1–2s sleep + 429 backoff; `DX-Y.NYB` not `DXY`. Never single point of failure.
- **GDELT 1 req/5s.** → single global token bucket; sector/macro queries only, never per-ticker.
- **SEC EDGAR 403/429.** → one shared throttled client, descriptive UA (`MarketTerminal saatvik1213@gmail.com`), ≤10 req/s — exceeding = 10-min IP block.
- **StockTwits / CNBC / MarketWatch / CNN Cloudflare/403/418.** → realistic browser UA + `Accept` headers; retry/backoff; **skip-on-fail, degrade gracefully**. Treat as fragile.
- **Reddit API likely-unavailable (verified 2026-06-09).** Self-service access closed Nov 2025; Responsible Builder Policy (updated 2026-06-05) requires approval; OAuth reportedly closing to personal scripts. → **Do not depend on PRAW.** Carry Reddit via ApeWisdom (volume) + Bluesky/StockTwits (text you score); PRAW only if you already hold working creds (≤60 req/min).
- **ApeWisdom single-point-of-failure** (one small operator, no sentiment). → keep PRAW + own cashtag extraction as fallback; remember sentiment must come from FinBERT, not ApeWisdom.
- **DEAD sources** (free X API, snscrape, Nitter, Pushshift, Reuters RSS, NewsAPI.org free, Alpha Vantage NEWS_SENTIMENT, Glassnode/CryptoQuant/Whale-Alert free). → architecturally forbidden; Bluesky is the X replacement.
- **CBOE options JSON & CNN endpoints unofficial.** → isolate each behind one adapter module with schema validation + graceful degrade; don't depend on unmaintained repos (`gex-tracker` = formula reference only).
- **Free quotas** (Finnhub 60/min, CoinGecko 10k/mo, CoinPaprika ~1k/day, metals.dev monthly, Marketaux 100/day, Etherscan 100k/day). → per-source token buckets + aggressive caching + watch 80/100% alerts; never call quota'd APIs from the browser.
- **Stooq silent quota** (returns plain-text "Exceeded the daily hits limit", not an HTTP error). → detect that string explicitly and back off.
- **DuckDB single-writer / single-process coupling.** → serialize writes through scheduler thread, read-only connections for API; FinBERT+scrapes on executor pools; `max_instances=1` per job; supervised restart + watchdog logging.
- **FinBERT limits** (neutral over-prediction, no entity-targeting, slang/sarcasm misses, label-order traps). → read each model's `id2label`; handle neutral explicitly; escalate ambiguous/multi-entity to Qwen3; slang preprocessing; **use off-the-shelf, never fine-tune** (−30% interference).
- **Latency honesty & ToS.** → badge every datum's freshness (LIVE crypto vs DELAYED equity); weekly/lagged sources (13F ~45d, COT weekly, OTC/ATS ~4wk, CBOE 15-min) labeled as-of. The sentiment layer is clean (open weights); ToS risk lives in scraping — keep all data **strictly local, personal, no redistribution**.
- **Backtest pitfalls.** → shift signals +1 bar (look-ahead), guard survivorship, out-of-sample/walk-forward before trusting; re-run quarterly to catch edge decay.

## 9. Post-P4 API Integration Backlog (added 2026-06-09, after Panel e shipped)

> Screened from a public-API list against the project's constraints (free tier real, adds something our verified sources don't, not another fragile scraper). **Verify each free tier at build time** — these were screened from a directory listing, not adversarially fact-checked like §5. Integrate in this order:

| Priority | API | Free tier | What it adds | Plugs into |
|---|---|---|---|---|
| **1** | **Alpaca** (key, no funded account needed) | IEX real-time WS quotes + free historical bars, US equities/ETFs | **Upgrades the equity leg from EOD-delayed to live-ish** — the single biggest honesty upgrade: Watchlist (d) intraday rows + equity prints/quotes in (e) alongside crypto | (d), (e); replaces yfinance as intraday primary, yfinance→fallback |
| **2** | **Tradier** (free dev sandbox token) | Delayed quotes + **full options chains w/ greeks & OI** | A *stable, documented* options source — replaces the fragile CBOE options JSON + yfinance `option_chain` for the GEX adapter and options-OI-surge proxy | (e) accumulation score, §6 #8 GEX |
| **3** | **Fed Treasury FiscalData** (no key) | Daily Treasury Statement: **TGA operating balance**, auctions, debt | Second source for the net-liquidity calc (`WALCL−RRP−TGA`) — FRED's `WTREGEN` is weekly/lagged; DTS is daily and authoritative | (c) net liquidity, cookbook #8 |
| **4** | **CongressInvests** (key, tier unverified) | Real-time Senate EFD / House Clerk stock-trade disclosures | Congressional cluster-buys = same pattern-edge as Form 4 insider clusters, different cohort | §6 extras, alongside #4 Form-4 tracker |
| **5** | **WallstreetBets API** (no key, single operator) | Pre-scored WSB comment sentiment | Cross-confirmation for the ApeWisdom spike signal (which has NO sentiment) — multi-source confirmation rule needs exactly this | (b) Phase 5 |
| **6** | **Twelve Data** (key, 800 credits/day, 8/min) | Real-time + historical stocks/FX/crypto JSON | Clean quota'd fallback when yfinance is banned and Alpaca is down — redundancy-by-design for the price plumbing | (d), (e) fallback chain |
| **7** | **Financial Modeling Prep** (key, ~250 req/day) | Earnings calendar, insider trades, fundamentals | Cleanest free **earnings calendar** (§6 #10 says EDGAR scraping for this is fragile) | §6 #10 calendar |
| **8** | **Polygon** (key, 5 req/min, EOD) | Historical OHLC, reference/ticker metadata | Another history backup + authoritative symbol list for cashtag validation | (d) history, (b) anti-gaming |
| **9** | **Econdb** (no key) | Global macro (ECB/BoJ/PBoC etc.) | Extends the cookbook beyond Fed-only liquidity (global net-liquidity card) | (f) new cards |
| **10** | **OpenFIGI** (key) | Bloomberg symbology mapping | Free authoritative symbol validation for the `$A/$ON/$IT` cashtag-collision filter | (b) anti-gaming |

**Screened out:** IEX Cloud (**retired Aug 2024 — dead**, despite directory listings); Alpha Vantage (25 req/day total — stays on the §5 avoid list); Marketstack/StockData/Finage/Intrinio/IG/SmartAPI/Real Time Finance (paywalled or trivial free tiers); Aletheia (EDGAR already covers Form 4/filings free); BriefTape/Helium/Sugra/Styvio/Hotstoks/EconPulse (new single-operator services, unverified — re-screen later); Yahoo Finance "API" listing (third-party paid wrapper); all banking/payments/IBAN/VAT/accounting entries (out of scope); Portfolio Optimizer (external compute violates local-only; vectorbt covers Phase 8 locally).

---

## 10. Gap Audit → Phase 11 Roadmap (added 2026-06-11, after Phase 10 shipped)

> Full implemented-vs-planned audit of the codebase. **Everything in §3 (all 6 panels), §6 (all 11 extras incl. the backtest harness — custom pandas/DuckDB regime replay in `edge/report_card.py`, not vectorbt), plus FOMC statement diffs, Lazy Prices filings diffs, Senate PTRs, 13F whales, market-wide insider scan, DBnomics intl cards, strategist + picks + report card is SHIPPED.** From §9 only FiscalData daily TGA (#3) is integrated; congressional trades were built directly on Senate eFD (no CongressInvests needed); Econdb (#9) is dead (Cloudflare) — DBnomics replaced it. Alpaca/Tradier/Twelve Data/FMP/Polygon/OpenFIGI remain unbuilt.

### ⚠️ Standing risk discovered during the audit

**Stooq now serves a JS challenge to headless clients** (see comment in `ingest/prices.py`), which silently broke the §8 "never a single point of failure" promise: **yfinance is currently the sole equity price source.** Items 1–2 below exist to fix exactly this.

### Phase 11 backlog (ranked by edge-per-effort, same method as §6)

| Rank | Feature | Why it's the gap | Free source | Effort |
|---|---|---|---|---|
| 1 | **Source-health watchdog panel** ✅ *shipped 2026-06-11* | The Stooq breakage was discovered by accident, in a code comment. Every fragile source (§5/§8) needs: last-success timestamp, consecutive-failure count, quota burn %, per-source status chip in the UI, ntfy on source-death. Protects every other feature; the terminal must never go quietly blind again. | self (instrument `ingest/http.py` — all fetches already flow through it) | **Low** |
| 2 | **Alpaca integration** (§9 #1, still top) ✅ *shipped 2026-06-11 (needs `MARKET_ALPACA_KEY_ID` + `MARKET_ALPACA_SECRET_KEY`)* | Now *urgent*, not just an upgrade: with Stooq JS-challenged, this restores price redundancy AND upgrades equities from delayed-EOD to IEX live-ish quotes. Shipped as: live watchlist quotes (`/api/watchlist/live`, LIVE badge, tiered 60s/15s polling) with Alpaca batched snapshots primary + yfinance `fast_info` fallback (keyless mode = yfinance only, delayed); Alpaca daily-bar backfill into the `'yahoo'` ts_price namespace whenever yfinance leaves a series stale. REST snapshots, not WS streaming; equity prints in panel (e) not included — revisit if WS becomes worth it. | Alpaca free (key, no funded acct) | **Med** |
| 3 | **Portfolio / holdings layer** | The biggest conceptual hole: the terminal knows everything about the market and *nothing about what I hold*. A `positions` table (symbol, qty, cost basis — manual/CSV entry, stays local+private) unlocks: P&L, exposure-vs-strategist-allocation drift, regime-aware *personal* alerts ("overweight equities entering stress regime"), and lets report_card score *my actual decisions*, not just hypothetical picks. | none needed (local data) | **Med** |
| 4 | **Unusual-options scanner + Tradier chains** (§9 #2) | Panel (e)'s accumulation score still lacks its "options OI surge" leg — never built. Tradier sandbox = stable documented chains w/ greeks & OI; also de-risks the fragile CBOE JSON that `edge/gex.py` depends on (keep CBOE as fallback). Scan: today's volume vs trailing-avg OI z-score per watchlist name. | Tradier dev sandbox | **Med** |
| 5 | **True short interest** ✅ *shipped 2026-06-11* | We ingest FINRA daily short-SALE volume, which §5 explicitly warns ≠ short interest. FINRA publishes actual bi-monthly equity short interest (shares short, days-to-cover) free via the keyless Query API (`api.finra.org`, dataset `consolidatedShortInterest`, partitioned by settlement date). High SI + retail mention-spike = squeeze-watch cross-signal panel (b)×(d) can't currently produce. | FINRA Query API (no key) | **Low** |
| 6 | **Market-wide earnings calendar** (§9 #7) ✅ *shipped 2026-06-11 (needs `MARKET_FMP_API_KEY`)* | Current calendar is watchlist-only via blocking yfinance calls. FMP free tier (~250/day) gives the full calendar → Event Horizon completeness + a hard "don't let strategist pick into earnings week" gate. Covers watchlist + news tickers + current strategist picks; yfinance stays the keyless fallback. | FMP (key) | **Low** |
| 7 | **ETF flows (shares-outstanding deltas)** | GLD/SLV/SPY/QQQ daily shares-outstanding changes = *actual* creation/redemption flow — strictly better than the volume proxies in (e) for metals accumulation + risk appetite. | issuer CSVs / yfinance `sharesOutstanding` (cached) | **Low-Med** |
| 8 | **Decision journal** | report_card scores the strategist; nothing scores *me*. Log each real decision with a frozen snapshot of regime+signals at entry; auto-score forward returns later. The only way to separate edge from luck over time. | none needed (local) | **Low-Med** |
| 9 | **OpenFIGI cashtag validation** (§9 #10) | Anti-gaming ticker validation in (b) still heuristic; `$A/$ON/$IT` collisions survive. | OpenFIGI (key) | **Low** |
| 10 | **FOMC presser tone (Whisper)** | `edge/fomc_diff.py` diffs the written statement; local Whisper on the press-conference audio adds spoken-tone delta (hawkish/dovish drift between statement and Q&A). Batch job, off the hot path — exactly the §3a carve-out. | federalreserve.gov audio + local Whisper | **Med** |
| 11 | **Per-card cookbook backtests** | Extend report_card's regime replay down to individual correlation-card rules (e.g. "BROKEN card #1 → what happened next, historically?") with walk-forward + 1-bar shift per §8. Re-run quarterly for decay. | self (stored series) | **Med** |
| 12 | **ETH exchange netflow** (optional) | Last unbuilt §5 source. Etherscan free is ETH-mainnet-only; Routescan as fallback. Low edge unless crypto allocation grows. | Etherscan/Routescan (key) | **Med** |

**Deliberately NOT planned:** Google Trends (pytrends archived, random 429s — fragility > signal); PRAW (confirmed dead per §8); CongressInvests (Senate eFD direct already shipped); vectorbt migration (custom replay in report_card is sufficient and dependency-free per §6 #11's actual goal).

*Suggested order: 1 → 5 → 6 (three Low-effort wins, one afternoon each) → 2 → 3 → 4 → 8, then re-rank.*

---

## 11. Phase 16 Gap Research (added 2026-06-28, from a 14-lane Sonnet research sweep)

Five signal families are entirely absent from the terminal: **rates microstructure** (bond vol, term premium, repo/CP stress), **real-economy nowcasting** (GDPNow, WEI, CFNAI), **energy fundamentals** (EIA petroleum/nat-gas weeklies), **policy-risk surface** (geopolitical risk, trade uncertainty, FedSpeak), and **Treasury supply dynamics** (auction demand, bidder class). The two fastest wins are the FRED credit-ladder completion (BBB-OAS + CPFF — two series calls on an already-wired key) and the Kim-Wright term premium (keyless fredgraph.csv — explains *why* yields move without building a model). Kalshi's keyless FOMC rate distribution feeds directly into the phase 15 divergence engine for near-zero marginal cost. Options-regime completeness (VVIX, VIX9D, ^MOVE) adds three orthogonal vol dimensions via sources already in the ingestor pattern. The EIA and policy lanes require modest new ingestors but unlock the only genuine energy and geopolitical-risk signals on the list.

### Phase 16 backlog (ranked by edge-per-effort)

| Rank | Feature | Signal it adds | Free source (concrete endpoint) | Access | Plugs into | Effort |
|---|---|---|---|---|---|---|
| 1 | **FRED macro nowcast trio: GDPNow + WEI + CFNAI** | Real-time intra-quarter GDP tracker (GDPNOW — moves equity markets on large revisions); weekly 10-series economic composite scaled to GDP growth (WEI); 85-indicator monthly recession signal (CFNAIMA3 < −0.70 historically = recession onset) | FRED series `GDPNOW`, `WEI`, `CFNAI`, `CFNAIMA3` — existing free API key; all confirmed live through June 2026 | Free key (already wired) | Macro/Regime panel — real-activity nowcast cards alongside NFCI | **Low** — 4 series IDs added to existing FRED batch pull |
| 2 | **FRED credit-stress additions: BBB-OAS + CPFF** | BBB OAS (`BAMLC0A4CBBB`) adds the missing tier to the existing IG→HY ladder; computed BBB-HY basis = fallen-angel spread (widens before IG downgrade waves, leads credit cycle turns); CPFF (3M AA commercial paper minus Fed funds) = primary-market CP stress gauge, spiked 450 bps in Oct 2008 | FRED `BAMLC0A4CBBB`, `CPFF`, `DCPN30`, `RIFSPPNA2P2D30NB` — existing key; all confirmed live | Free key (already wired) | Macro/Regime panel — extends existing HY-OAS + IG-OAS rows | **Low** — 3–4 new series in existing FRED batch |
| 3 | **Kim-Wright term premium (THREEFYTP10) + TIPS 5y5y forward (T5YIFR)** | Decomposes nominal 10Y yield into real rate + term premium daily; rising term premium with flat breakeven = supply/fiscal panic not inflation → different regime implication; T5YIFR is the Fed's own long-run credibility anchor (rising = markets doubt the 2% target, a regime-change leading indicator) | `https://fred.stlouisfed.org/graph/fredgraph.csv?id=THREEFYTP10,T5YIFR,T5YIE` — keyless fredgraph.csv path (no API key required, distinct from the FRED REST API); data through June 2026 confirmed | Keyless | Regime classifier + new Rates & Inflation panel | **Low** — one CSV fetch; new ingestor function |
| 4 | **^MOVE + VVIX + VIX9D** | ^MOVE = bond-market VIX (MOVE/VIX ratio identifies rate-led vs equity-led risk-off episodes); VVIX/VIX = vol-of-vol (VVIX spikes before spot VIX when sophisticated hedgers front-run a regime shift); VIX9D/VIX = front-end slope of the vol term structure, distinct signal from existing VIX3M/VIX | `^MOVE` via yfinance; `cdn.cboe.com/api/global/us_indices/daily_prices/VVIX_History.csv`; same CDN pattern for `VIX9D_History.csv` — all keyless; VVIX confirmed live | Keyless | Regime classifier (3 new orthogonal dimensions) | **Low** — two CDN CSV fetches + one yfinance ticker; identical pattern to existing VIX3M ingestor |
| 5 | **ICI weekly fund flows + MMF assets (XLS)** | Real dollar equity/bond MF+ETF net flows by style and cap-size; $8T+ MMF asset split (prime/govt/Treasury); prime-to-govt rotation = textbook early stress signal; institutional MMF outflows precede equity reallocation by days to weeks — the canonical "cash on sidelines" gauge | `https://www.ici.org/combined_flows_data_2026.xls`; `https://www.ici.org/mm_summary_data_2026.xls` — keyless direct XLS, both confirmed live through May/June 2026 | Keyless | Positioning panel + Macro/Regime panel | **Low** — openpyxl parse, weekly cron; URL contains year — update each January or parameterize |
| 6 | **CFTC TFF — Traders in Financial Futures** | Superior replacement for legacy COT commercial/non-commercial split: breaks ES, NQ, 10Y Treasury, VIX, and crypto futures into Asset Manager (pension/insurance/mutual funds) vs Leveraged Money (hedge funds) vs Dealer Intermediary; AM vs LM divergence is the actionable signal legacy COT hides | `https://www.cftc.gov/dea/newcot/FinFutWk.txt` (weekly fixed-width, confirmed live 2026-06-23); Socrata `https://publicreporting.cftc.gov/resource/gpe5-46if.json` for filtered JSON pulls | Keyless | Positioning panel — upgrade existing COT | **Med** — `cot_reports` lib supports TFF natively; Socrata path preferred over flat-file parsing |
| 7 | **Treasury auction demand (FiscalData auctions_query)** | Per-auction bid-to-cover ratio + primary dealer / indirect / direct bidder accepted and tendered amounts + high yield; heavy primary-dealer takedown means real money stayed away = yield spike risk; 11,022 records back to 1979, 52 fields confirmed | `https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query?sort=-record_date&format=json` — keyless JSON, confirmed live | Keyless | Macro/Liquidity + new Treasury Supply Dashboard | **Low** — same FiscalData client already in stack for TGA; add new endpoint |
| 8 | **Kalshi FOMC + CPI keyless REST (KXFED, KXCPI)** | Full per-strike probability distribution for FOMC rate outcomes and CPI MoM prints — not just binary next-cut; NBER-verified (2026010) to match or beat Bloomberg consensus; cross against FRED DFF/EFFR curve → bond-vs-prediction-market divergence signal feeding directly into the phase 15 engine | `https://external-api.kalshi.com/trade-api/v2/markets?series_ticker=KXFED&status=open`; same for `KXCPI`; enumerate series via `/trade-api/v2/series?category=Economics` — confirmed keyless, 200 read tokens/sec | Keyless | Divergence panel (phase 15 engine) + event-gate calendar | **Low** — simple REST poll; discover series on startup to catch new tickers |
| 9 | **Cleveland Fed inflation nowcast (daily JSON)** | Daily CPI and PCE MoM + YoY nowcast updated ~10am ET via oil/gasoline/prior-print inputs — the only confirmed-live keyless daily inflation estimate ahead of BLS release; nowcast-vs-prior-estimate gap = surprise direction signal; flags pre-CPI regime shifts the terminal currently misses entirely | `https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast_month.json` + `nowcast_year.json` — keyless, same-day update confirmed 2026-06-26 | Keyless | Macro/Regime panel — pre-CPI alert flag | **Med** — FusionCharts JSON format requires custom parser; 4 series (CPI, Core CPI, PCE, Core PCE) per file |
| 10 | **EIA WPSR crude + nat-gas storage (keyless weekly CSVs)** | Crude oil inventory build/draw vs 5-yr average + WTI/Brent/RBOB spot prices → in-process 3:2:1 crack spread (Wed 10:30 ET); nat-gas storage surplus/deficit vs 5-yr norm (Thu 10:30 ET) — both are the primary weekly price catalysts for energy names; currently zero energy fundamentals in the terminal | Crude: `http://ir.eia.gov/wpsr/table1.csv`, `table11.csv`; nat-gas: `http://ir.eia.gov/ngs/wngsr.csv` — all keyless, no JS, confirmed live through 2026-06-19 | Keyless | New Energy Fundamentals panel | **Low-Med** — 3 CSV fetches; crack spread = (2 × RBOB + heating oil − 3 × WTI) / 3, all inputs in table11 |
| 11 | **GPR Index + TPU daily CSV + Fed speeches hawk/dove RSS** | Monthly geopolitical risk score (1900-present, Threats sub-index leads Acts by 1-3 months); daily trade-policy uncertainty by sub-category (monetary/fiscal/trade/healthcare — routes sector rotation signals); FedSpeak hawk/dove stream scored by existing local FinBERT/Qwen3 — three distinct policy-risk dimensions wholly absent today | GPR: `https://www.matteoiacoviello.com/gpr_files/data_gpr_export.xls` (keyless Excel, confirmed live). TPU: `https://policyuncertainty.com/media/All_Daily_TPU_Data.csv` (keyless, confirmed live). Fed RSS: `https://www.federalreserve.gov/feeds/speeches_and_testimony.xml` (keyless, confirmed live June 2026) | Keyless | New Policy-Risk Surface panel | **Med** — three separate ingestors; Excel + CSV + RSS parse; NLP on speech text using existing FinBERT/Qwen3 |
| 12 | **8-K item-code EDGAR scanner** | Material cybersecurity incident (Item 1.05, mandatory since Dec 2023), surprise CEO/CFO departure (5.02), new buyback program (8.01 + 'repurchase'), asset impairment (2.06) — each a distinct tradeable catalyst detectable within ~60 s of EDGAR acceptance; supplements existing FinBERT news pipeline with structured item-code targeting | `https://efts.sec.gov/LATEST/search-index?q=%22item+1.05%22&forms=8-K` (swap item code per signal); Atom feed for real-time: `https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom` — keyless, 10 req/s, UA required | Keyless | Calendar + Strategist panel; extends existing EDGAR pipeline | **Med** — EDGAR EFTS already in stack; add item-text filter + CIK→ticker resolution via existing `company_tickers.json` |
| 13 | **SC 13D/13G activist stake (EDGAR EFTS)** | Machine-readable XML since Dec 2024 (holder name, share count, % owned, stated purpose); 13D = intent to influence, historically moves target 5-10% within hours of filing; orthogonal to existing 13F (quarterly, large institutions) and Form 4 (existing insiders) | `https://efts.sec.gov/LATEST/search-index?q=&forms=SC+13D,SC+13G,SC+13D%2FA,SC+13G%2FA&dateRange=custom&startdt=TODAY` — keyless, same EDGAR rate limit | Keyless | Insider/Whales panel | **Med** — EDGAR EFTS already in stack; XML parse new; CIK→ticker via existing mapping |
| 14 | **BIS CBTA global CB balance sheet** | Single keyless CSV for Fed + ECB + BoJ + PBoC + 30 other CBs in USD-converted and GDP-ratio variants — the only free source assembling a true global QE/QT score; pairs with FRED WALCL to construct global-vs-Fed divergence; DBnomics intl cards give individual CB slices but not the aggregate | `https://stats.bis.org/api/v1/data/WS_CBTA/A.JP+US+XM+CN..?startPeriod=2020&format=csv` — keyless, confirmed live with 2023-2025 data; quarterly cadence, ~1 quarter lag | Keyless | Macro/Liquidity panel (extends WALCL + existing DBnomics intl cards into global sum) | **Low** — one CSV fetch, quarterly cron |
| 15 | **CBOE SKEW + implied correlation COR3M** | SKEW = 30-day S&P 500 tail-risk premium (SKEW high + VIX low = quiet tail hedging = early warning before VIX reacts); COR3M = average pairwise SPX component correlation (high = macro panic tape; low = stock-picker's market) — both orthogonal to VIX level, zero overlap with existing GEX | `^SKEW`, `^COR3M` via yfinance; CDN backfill at `cdn.cboe.com/api/global/us_indices/daily_prices/SKEW_History.csv`, `COR3M_History.csv` — keyless | Keyless | Regime classifier (two new dimensions) | **Low** — two yfinance tickers; same pattern as existing VIX/yfinance ingestors |

### Net-new panels worth standing up

**Rates & Inflation Expectations** — term premium + TIPS 5y5y forward (rank 3), Cleveland daily CPI/PCE nowcast (rank 9), MOVE bond vol (rank 4); all FRED/keyless with existing ingestor patterns; deserves a standalone panel rather than crowding the Macro card further.

**Energy Fundamentals** — EIA WPSR crude inventories + crack spread + nat-gas storage (rank 10); fully keyless weekly CSVs; Wednesday crude and Thursday gas storage releases routinely move energy names; currently zero energy fundamentals exist in the terminal.

**Treasury Supply Dashboard** — auction demand bid-to-cover (rank 7) + upcoming auction calendar (`api.fiscaldata.treasury.gov/...upcoming_auctions` + TreasuryDirect `PendingAuctions.xml`, both confirmed keyless); extends existing FiscalData TGA integration into a complete bond-supply early-warning surface.

**Policy-Risk Surface** — GPR + TPU + Fed speeches hawk/dove + Federal Register executive orders (`https://www.federalregister.gov/api/v1/documents.json`, keyless, confirmed live, filters by agency and significance flag); geopolitical and policy uncertainty are wholly absent in every current panel.

### Verify-at-build-time / fragile

- **CBOE VIX9D_History.csv** — CDN file loads but tail row confirmed only to 2018 in testing; verify the last-date field is current before treating it as live; CBOE actively maintains the index but the CDN path is unofficial.
- **ICI XLS URLs** — year-embedded (`*_data_2026.xls`); parameterize or update each January; URL convention changed once before.
- **Kalshi series tickers (KXFED, KXCPI, etc.)** — not fully documented; enumerate live via `/trade-api/v2/series?category=Economics` before hardcoding; seasonal/quarterly contracts expire between runs.
- **CFTC TFF fixed-width format** — `FinFutWk.txt` column spec is unversioned; pin against a known-good row hash; the Socrata JSON endpoint (`gpe5-46if`) is more stable and preferred.
- **SC 13D/13G XML fields** — Dec 2024 mandate applies to new filings only; confirm `<beneficialOwnerName>` and `<sharesOwnedFollowingTransaction>` are present before relying on structured parsing; pre-2025 filings remain HTML-only.

**Suggested order:** 1 (GDPNow/WEI/CFNAI, ~1 hr) → 2 (BBB-OAS + CPFF, ~1 hr) → 3 (term premium + 5y5y, ~1 hr) → 4 (VVIX/VIX9D/^MOVE, ~1 hr) → 7 (auction demand, ~2 hr) → 8 (Kalshi, ~2 hr) → 5 (ICI flows, ~3 hr) → 14 (BIS CBTA, ~2 hr) → 15 (SKEW/COR3M, ~1 hr) → 10 (EIA energy, half-day) → 6 (TFF, half-day) → 9 (Cleveland nowcast, half-day) → 11 (policy-risk trio, ~1 day) → 12–13 (EDGAR corporate events, ~1 day).

---

## 12. ML Meta-Signal — predictive indicator from all stored signals (planned 2026-06-28, NOT built)

> The terminal already produces ~dozens of orthogonal daily/weekly signals (regime, positioning, sentiment, flows, breadth, divergence, prediction-market odds). This phase trains a **leakage-safe model zoo** on a point-in-time snapshot of all of them to emit ONE calibrated daily indicator — `P(SPY/QQQ up over h days)` (and a vol-scaled expected-return twin) — that feeds the Strategist and is scored forward by `report_card`. **Honest prior: this is the most overfit-prone thing in the whole plan.** With daily data you have ~2–4k rows and hundreds of candidate features — the curse-of-dimensionality zone where fancy nets reliably *lose* to a well-regularized gradient-boosted tree. The deliverable's value is 80% in the validation harness (§12.3) and 20% in the models. Build the harness first; treat every model as guilty of overfitting until walk-forward proves otherwise.

### 12.1 Target (the dependent variable)

- **Predict log returns, never price levels.** `r = ln(P_{t+h}/P_t)`. Price levels have a unit root → a model trained on levels memorizes "tomorrow ≈ today," scores a fake-great R², and has zero edge (the price-prediction trap). Log returns are ~stationary, time-additive, scale-free across assets/eras.
- **Volatility-scale it:** the primary target is `r / σ_t` (σ = EWMA / rolling realized vol) so high-vol regimes don't dominate the loss. This is the single highest-leverage modeling choice.
- **Also model direction:** parallel classification target = `sign(r)` with **triple-barrier labels** (López de Prado: profit-take / stop / time barriers in vol units) + optional **meta-labeling** (a second model sizes the bet given the primary's direction). Classification calibrates to a clean "indicator" probability better than regressing noisy returns.
- **Horizons:** `h ∈ {1, 5, 21}` trading days (matches signal cadence). Train per-horizon; the 5-day is the headline indicator.
- **Targets per asset:** start SPY + QQQ (deepest history, cleanest), then per-watchlist-name once the harness is trusted.

### 12.2 Feature matrix (point-in-time)

- One row per (date, asset); columns = a PIT snapshot of every stored signal, **lagged to the moment it was actually knowable** (see §12.3 leakage).
- Families: macro/regime (NFCI, OAS ladder, term premium, nowcasts), positioning (COT/TFF, true SI, GEX, put/call, VIX term structure, VVIX), sentiment (FinBERT news/social aggregates), flows (ICI, ETF shares-out), breadth, divergence score, Polymarket/Kalshi odds, seasonality, and price-derived momentum/vol/RSI (kept on a short leash — they tend to swamp the slower fundamental signals).
- Engineering: regime-relative **z-scores / percentile ranks** (not raw levels), deltas/velocities, and a few regime×signal interactions. Optional **fractional differentiation** to make series stationary while keeping memory (López de Prado) instead of naive first-differencing.
- All transforms (scaling, ranking, feature selection, imputation) fit **inside each CV fold**, never on the full panel.

### 12.3 Leakage control — the part that actually matters

- **As-of / point-in-time joins, on release timestamp not reference date.** COT prints Friday for Tuesday's positions; 13F lags ~45d; GDP/CPI get revised. Use vintages (FRED→ALFRED) and the source's *publish* time. A model that sees Tuesday's COT on Tuesday is cheating.
- **Purged K-fold + embargo:** overlapping multi-day labels leak train↔test; purge train samples whose label window overlaps the test fold and embargo a gap after it.
- **Walk-forward / expanding-window** as the headline evaluation; **Combinatorial Purged CV (CPCV)** to get a *distribution* of OOS performance, not one lucky split.
- No survivorship in the asset universe; no peeking via global standardization.

### 12.4 Model zoo (run all; let walk-forward pick)

| Model | Role | Library (local, $0) |
|---|---|---|
| ElasticNet / Logistic | Linear baseline — **the bar every fancy model must clear** | scikit-learn |
| **LightGBM / XGBoost** | Tabular workhorse — expected champion on daily data | lightgbm / xgboost (CPU) |
| Random Forest | Bagged baseline + honest feature importance | scikit-learn |
| 1D-CNN | Local cross-feature / short-window patterns | PyTorch (MPS) |
| LSTM / GRU | Sequence memory over the feature panel | PyTorch (MPS) |
| CNN-LSTM hybrid | Conv feature extraction → recurrent head (your suggestion) | PyTorch (MPS) |
| TCN (dilated causal conv) | Often beats LSTM, parallel, stable gradients | PyTorch (MPS) |
| PatchTST / small Transformer | Optional — data-hungry, likely overfits here; include for completeness | PyTorch (MPS) |
| **Stacking meta-learner** | Combines the OOS predictions of the above — this, not any single net, is the real "best model" | scikit-learn |

> **On GANs:** a GAN is **not a predictor** — TimeGAN / quant-GANs *generate synthetic price paths* for data augmentation and stress-testing, not directional forecasts. Slotting one in as the indicator would be a category error. Carry it as an **optional §12.7 augmentation** step (synthesize extra training paths to regularize the sequence nets), evaluated only by whether it improves OOS metrics — never as the output signal.

### 12.5 Selection — there is no "perfect" model

Rank candidates by **walk-forward OOS**, not in-sample fit:
- Regression target: **rank-IC** (Spearman of prediction vs realized vol-scaled return) with t-stat; OOS Sharpe of a simple long/short driven by the signal, **after costs**.
- Classification target: AUC + **calibrated Brier** (Platt/isotonic calibration so the probability means something).
- Discount for multiple testing: **Probability of Backtest Overfitting (PBO)** and **Deflated Sharpe Ratio** — running 9 model types across 3 horizons is a multiple-comparisons minefield; DSR/PBO are how you avoid crowning noise.
- Tie-break on **stability** across CPCV folds and across time, not peak fold score.

### 12.6 Output & wiring

- Emit a daily row to a new `ml_predictions` duck table: `{date, asset, horizon, p_up, exp_ret_volscaled, conf_band, champion_model, feature_version}`.
- New **"ML Signal" panel**: the calibrated probability + a **live OOS hit-rate vs naive baselines** badge (so it's honest about whether it's currently working) + top contributing features (SHAP) + an auto **decay flag** when rolling OOS IC goes negative.
- Feed the score into the **Strategist as one input among many (never the oracle)** and let **`report_card`** score it forward like any other pick. Badge it MODEL-OUTPUT, delayed-as-of.

### 12.7 Script architecture (`apps/api/app/ml/`)

- `dataset.py` — PIT feature matrix builder from DuckDB (release-time joins, ALFRED vintages).
- `labels.py` — log-return, vol-scaled, triple-barrier + meta-labels.
- `cv.py` — purged K-fold, embargo, walk-forward, CPCV splitters.
- `zoo/` — one file per model behind a common `fit(X,y)/predict(X)` interface.
- `train.py` — CLI: trains the whole zoo × horizons, logs every OOS metric to a run table (reproducible: fixed seeds, frozen feature spec).
- `select.py` — applies §12.5 gates, writes the champion + its metrics to a versioned `models/` registry.
- `predict.py` — daily inference job (APScheduler, **default-off**, like the bots); writes `ml_predictions`.
- Retrain cadence: quarterly walk-forward refit (per §8 decay discipline). All PyTorch on MPS, LightGBM CPU, fully local, $0 tokens.

### 12.8 Phased build (harness-first)

1. **Dataset + labels + leakage-safe CV** (the hard, load-bearing 80%). Ship nothing else until a known-leaky feature is provably caught by the harness.
2. **Baselines** (ElasticNet, LightGBM) + the full metric/PBO/DSR report — establishes the bar and a first honest read on whether *any* edge exists.
3. **Sequence nets** (TCN, LSTM, CNN-LSTM) head-to-head vs the tree baseline.
4. **Stacking ensemble + calibration** → champion selection.
5. **Daily inference job + ML panel + Strategist/report_card wiring.**
6. **Optional:** TimeGAN augmentation, Transformer, regime-conditional sub-models — only if §2–4 show real OOS edge.

### 12.9 Risks

- **Overfitting / curse of dimensionality** — few independent daily samples vs many features. Mitigate: aggressive regularization, feature budget, PBO/DSR gating, prefer trees over nets. *Expect LightGBM to win; run the nets to prove it, not to deploy them by default.*
- **Leakage from revisions / release timing** — the silent killer; §12.3 is non-negotiable.
- **Non-stationarity / regime change / edge decay** — quarterly refit, live OOS decay flag, never trust a frozen model.
- **Multiple-testing bias** — running the whole zoo inflates the best score; DSR/PBO mandatory before belief.
- **Garbage-in** — a flaky upstream source (see §5/§8) silently poisons features; gate training on source-health freshness.

**Net honest take:** worth building for the *harness and the calibrated ensemble probability* as one more Strategist input — **not** as a standalone "predict the market" oracle. If baselines (step 2) show no OOS edge after costs, that is itself the valuable, money-saving result — stop there.

---

**Sources for the two time-sensitive 2026 pivots I re-verified:** [Bluesky firehose free / no paid tier (Blotato 2026)](https://www.blotato.com/blog/bluesky-api-pricing) · [Bluesky cashtags Jan 2026 (TechCrunch)](https://techcrunch.com/2026/01/16/bluesky-rolls-out-cashtags-and-live-badges-amid-a-boost-in-app-installs/) · [CCXT Pro WebSockets merged into free CCXT (GitHub #15171)](https://github.com/ccxt/ccxt/issues/15171). All other source URLs are inline in the research dimensions above and the master table.
---

## 13. Phase 21 — Per-Name Volatility Risk Layer (design review 2026-08-14, NOT built)

> **Read §13.0 before anything else — the premise this phase was written on is wrong.**

### 13.0 Premise correction: most of "Phase 21" already exists, and the model already lost

The Phase-21 memo was written on 2026-08-13 off the `xtrain` cross-sectional run: forward realised
vol scored **IC 0.47 (h=5) / 0.62 (h=21)**, 100% daily hit rate, while returns failed every gate.
The memo concluded this was "the real find" and "the build". Three facts in this repo say otherwise:

1. **`apps/api/app/ml/vol_baselines.py` (committed `ac206a1`, 2026-06-28) already ran exactly the
   persistence check the memo listed as future work** — naive 21d close-to-close vol, naive 21d
   Garman-Klass vol, and a panel HAR (Corsi 2009) refit per fold, scored on the *identical* target,
   cross-sectional IC and `DateWalkForward` the GBM used. Its own docstring names the trap:
   *"vol is PERSISTENT ... a naive 'rank by trailing vol' predictor may already score most of that
   IC"*, citing Audrino & Chassot 2024 ("HARd to Beat": tuned HAR beats Lasso/RF/GBM/NN on 1,445 stocks).
2. **The naive estimator beats the 66-feature GBM.** `vol_overlay.py:4-6`: *"trailing Garman-Klass vol
   already nails it — IC 0.53/0.70"*. **Re-measured 2026-08-14 and confirmed** (§13.0a) — on identical
   OOS rows a parameter-free 21-day estimator scores **higher** than the gradient-boosted model on 66
   features. (Note: `GBM_REF = {5: 0.460, 21: 0.610}` at `vol_baselines.py:38` is a stale hardcoded
   constant from a different "56 feat" harness, not scored on the same rows — hence the re-run.)
3. **The deployable version is already built and wired.** `apps/api/app/ml/vol_overlay.py` produces a
   vol-targeted exposure weight, and it is live in three places: the strategist signal list and tilts
   (`edge/strategist.py:610-635`), the `/api/edge/vol-overlay` route (`routers/edge.py:88-100`), and the
   day sleeve's position-size throttle (`daytrader.py:1646` → `intraday.py:106-114`).

**Ruling:** the finding "cross-sectional vol is strongly forecastable" is true and unchanged — it is
also six weeks old, already actioned, and *not* evidence for an ML vol model. **The GBM is dropped
from this phase.** Everything below is built on the trailing-GK/HAR estimator that already wins.
This deletes three of the memo's four open questions (model versioning, retrain cadence, artifact
persistence): a parameter-free estimator has nothing to retrain and no artifact to version.

### 13.0a Gate 0 — measured 2026-08-14, all predictors on identical OOS rows

Re-ran `app.ml.vol_baselines` and added the two tests it never covered (orthogonalised residual,
level calibration). Same 147-name panel, same `DateWalkForward`, GBM reproduced at 0.4710/0.6169
confirming the harness. **Rank IC** (cross-sectional, vs `labels.forward_realized_vol`):

| h | GBM (66 feat) | naive_gk (21d) | HAR (Corsi, in-fold) | GBM lift vs naive_gk |
|---|---|---|---|---|
| 5 | 0.4710 (t=231) | **0.5190** (t=269) | 0.5174 (t=269) | **−0.048** |
| 21 | 0.6169 (t=349) | **0.6898** (t=446) | 0.6855 (t=434) | **−0.073** |

**Level accuracy** — the metric that governs sizing, never previously measured. Predicted vs realised
daily σ; QLIKE on variance; calibration = OLS of realised on predicted:

| h | model | RMSE | QLIKE | slope | R² |
|---|---|---|---|---|---|
| 5 | GBM | 0.01146 | 0.8549 | 1.396 | 0.342 |
| 5 | naive_gk | 0.01062 | 0.7806 | **1.069** | 0.400 |
| 5 | **HAR** | **0.01038** | **0.7403** | 1.187 | **0.442** |
| 21 | GBM | 0.00943 | 0.4304 | 1.133 | 0.342 |
| 21 | naive_gk | 0.00880 | 0.5979 | **1.018** | 0.496 |
| 21 | **HAR** | **0.00812** | **0.3756** | 1.098 | **0.523** |

**HAR is the best model by every level metric at both horizons**, and the GBM is the worst-calibrated
(slope furthest from 1, lowest R²). Its predictions are 0.81–0.83 rank-correlated with naive_gk — it is
largely reconstructing the trivial baseline, more noisily.

**But the §13.3 residual bar cleared, decisively.** Orthogonalising both the GBM prediction and the
realised target against rank(naive_gk) leaves residual IC **0.131 (t=74, h=5)** and **0.205 (t=116, h=21)**
— far above the pre-registered ≥0.05 / t>3. Recorded as passed.

### 13.0b Stronger controls — the GBM residual is real, and it is also nearly worthless

naive_gk is a single 21-day window, so a GBM that merely learned a better *mix* of lookbacks would show
that residual while knowing nothing beyond vol history. Four stronger controls were run. **The residual
survived all of them** — it does not decay toward zero under any reparameterisation of vol history:

| Control the GBM is orthogonalised against | h=5 resid IC (t) | h=21 resid IC (t) |
|---|---|---|
| naive_gk (the original, weak control) | 0.131 (74) | 0.205 (116) |
| in-fold HAR | 0.148 (84) | 0.223 (129) |
| joint: HAR + naive_gk | 0.124 (71) | 0.201 (114) |
| parameter-free composite rank(5d, 21d, 63d) | 0.126 (72) | 0.203 (115) |
| in-fold OLS on log-GK(5d, 21d, 63d) — strongest control built | 0.140 (80) | 0.208 (118) |

**Feature importance says why it is nonetheless small.** A single feature — `px_rvol_21`, literally
trailing 21-day realised vol — carries **57–59% of total gain**. Own-price/vol history is 73–74% of gain;
macro/positioning is 22–23% but is itself dominated by *market-wide* vol-regime indicators (VVIX/VIX,
VIX_z, VIX9D term structure); genuine cross-asset content is 3–5%. The GBM's edge is nonlinear
interaction of the *same* own-name vol and trend history, plus a market-wide vol-regime assist — not a
distinct information source.

**Practical payoff**: blending the residual into HAR at the best weight (w=0.1) buys **+0.004 IC (h=5)
and +0.005 IC (h=21)**, with a ~0.002 QLIKE improvement. Statistically overwhelming; economically trivial.

**The finding that actually matters came out of the control itself**: the in-fold OLS on log-GK at
**5d/21d/63d lookbacks** scored **IC 0.5235 (h=5) / 0.7027 (h=21)** — beating standard HAR *and* naive_gk,
and by +0.017 at h=21, roughly **three times the gain the entire 66-feature GBM pipeline buys**. Widening
HAR's lookbacks is free.

### 13.0c HAR-63 evaluated — it wins on ranks, loses on levels, and breaks in a shock

| h | estimator | rank IC | RMSE | QLIKE | slope | R² |
|---|---|---|---|---|---|---|
| 5 | HAR (1,5,22) | 0.5174 | **0.010379** | **0.7403** | 1.187 | **0.4423** |
| 5 | **HAR-63 (5,21,63)** | **0.5235** | 0.010424 | 0.7450 | 1.164 | 0.4331 |
| 21 | HAR (1,5,22) | 0.6855 | **0.008123** | **0.3756** | 1.098 | **0.5225** |
| 21 | **HAR-63 (5,21,63)** | **0.7027** | 0.008155 | 0.3835 | 1.040 | 0.5117 |

**Lookback robustness — a plateau, not a peak.** Five non-standard triples all land within IC
0.5219–0.5255 (h=5) and 0.6997–0.7080 (h=21); every one clears standard HAR and naive_gk by a similar
margin. The *simplest* spec tested — two-term (21,63), dropping the short lookback entirely — has the
**highest IC of the grid**, which is the opposite of what selection-overfit looks like. HAR-63 is not a
lucky triple. But the grid also shows an IC-vs-calibration tradeoff: standard HAR has the best QLIKE of
the whole grid, and (21,63) has the worst despite the best IC.

**Fold stability (6 walk-forward folds).** HAR-63 beats naive_gk in **every fold at both horizons** — no
single regime drives it. Both estimators score highest in the 2008 GFC fold and decay toward recent,
choppier folds (2023–2026: IC 0.46 h=5 / 0.65 h=21) — **live expectations must be anchored to the recent
fold, not the full-sample headline.** One flag: **h=5, HAR-63, fold 3 (2017–2020, contains COVID) —
calibration slope 1.355**, outside [0.8, 1.2]: a ~35% underprediction of near-term vol during a fast
regime shift, precisely when sizing matters most. At h=21 the same fold is fine (slope 1.091). Plain HAR
never left the band in any fold.

**Blend recheck**: on the stronger HAR-63 base the GBM residual adds only +0.0035/+0.0036 at w=0.1, down
~25% from its gain over plain HAR — a better base leaves less for it to contribute. Confirms §13.3.

### 13.1 What is actually missing

| Capability | Status |
|---|---|
| Market-level vol forecast (SPY) → exposure weight | **Exists** — `vol_overlay.current_signal()` |
| → strategist tilt, edge panel, day-sleeve `risk_scale` | **Exists** — 3-tier step function, `intraday.py:106-114` |
| **Per-name** vol forecast for the tradable set | **Missing** — overlay is called with one symbol (SPY) |
| Swing-sleeve vol input of any kind | **Missing** — sizing is pure rebalance-to-target-weight (`bot.py:216-256`) |
| Persisted per-name forecasts | **Missing** — no table; overlay recomputes on demand, cached 15min |
| Forecast graded vs realised outcome | **Missing** — `day_review` grades P&L only, never forecast accuracy |
| Strategist access to per-name vol ranks | **Missing** — it sees the market-level weight only |

The net-new capability is therefore **per-name relative vol → per-name sizing and stop geometry**,
plus the accountability layer (persist → grade) that the market-level overlay never got.

### 13.2 The unpriced blocker: the bot-tradable set has no live price coverage

This, not modelling, is the critical path.

- `data/market.duckdb` holds **43 symbols** — only **8 of the day sleeve's 60** (`settings.day_universe`).
- `data/ml/universe.duckdb` holds the 147 research names — **40 of 60**, and it is a **one-shot snapshot**
  written only by `scratchpad/fetch_universe.py`, with no refresh job anywhere in `scheduler/jobs.py`.
- The fetcher is deliberately slow (sequential, `time.sleep(1.5)`, ~4 min for 147 names) to dodge
  Yahoo's burst throttle — fine nightly, not fine on demand.
- Coupling to watch: the 390MB feature-matrix cache is keyed on `universe.duckdb`'s **mtime**
  (`xtrain.py:170`), so every refresh invalidates it and `xtrain` deletes *all* stale caches on rebuild.
  Re-key the cache on content (max(ts) + row count) or accept a ~minutes rebuild after each refresh.
- The swing watchlist is worse: **3 of 11** names are single stocks (AAPL, NVDA, TSLA). The rest are
  ETFs (SPY/QQQ/GLD/SLV), crypto (BTC/ETH) and metals (XAU/XAG) — none in the research universe.
  GK vol needs only OHLC, so they *can* be scored, but they were never in the panel the IC was measured on.

**Consequence:** per-name vol scores are only as good as daily OHLC coverage. Step 1 of the build is a
daily incremental price refresh for `day_universe ∪ watchlist`, not a model.

### 13.3 Estimator ruling and the pre-registered GBM kill switch

**Two estimators, each used only where it measurably wins** (§13.0c) — resist the urge to pick one:

| Use | Estimator | Why |
|---|---|---|
| **Ranks** (which names are jumpier: relative weights, trim lists, `vol_rank` tool) | **HAR-63** — in-fold OLS on log-GK at 5/21/63d | Best rank IC at both horizons, broad plateau across nearby triples, beats naive_gk in all 6 folds |
| **Levels** (σ in daily units → dollar risk, stop distances) | **plain HAR (1,5,22)**, at **h=21 only** | Best RMSE/QLIKE/R² at both horizons; calibration slope never left [0.8, 1.2] in any fold |
| Fallback if either fit degenerates | `naive_gk` | Zero parameters, cannot drift, best raw slope (1.069/1.018) |

**h=5 levels are not admissible for sizing.** In the COVID fold, HAR-63's 5-day calibration slope hit
1.355 — it underpredicted near-term vol by ~35% during exactly the kind of shock sizing exists to survive.
h=21 stayed calibrated through the same fold. So: **h=5 is a rank and alert signal only; h=21 carries the
levels.** This is preferred over the alternative of a regime-triggered sizing multiplier, which would add
a tunable knob — and untested knobs are how this project has previously fooled itself.

All of these already exist in `vol_baselines.py` (`_gk_daily_vol`, `_har_ic`); GK is ~5–8× more efficient
than close-to-close when no intraday data exists, which is our situation.

**GBM status: it passed every statistical bar I set, and it is still shelved. The statistics did not
kill it — the engineering economics did, and that distinction is recorded deliberately.**

Both admissibility conditions were met: the residual survived four stronger controls (§13.0b), and a
w=0.1 blend beat HAR alone on both IC and QLIKE at both horizons. By the pre-registered rule the GBM is
*admissible*. Admissible is not the same as worth shipping, and the cost side is lopsided:

- **Benefit**: +0.004–0.005 rank IC. Against a free lookback change (HAR-63) worth +0.017 at h=21.
- **Cost**: the full 66-feature pipeline in the daily live path — lightgbm, model artifacts, a retrain
  cadence and version registry, and a hard dependency on macro series with publication lags of 4–45 days
  (GPR 45d, M2 30d, WEI 6d, COT/TFF weekly) that must all be fresh for a score to be emitted.
- **Revision risk — the decisive one.** 22–23% of the GBM's gain comes from the macro/positioning block,
  and the h=21 top-15 includes `eng_anfci`, `eng_m2_yoy`, `eng_term_prem` — revision-prone series. This
  project has already been burned once by exactly this: §12's `EDGE_FOUND` verdict was ~60% NFCI revision
  leakage, and revised-vs-PIT NFCI ranked at only 0.45 correlation. The backtest that produced the
  +0.005 was run on revised macro; live scoring sees first prints. The measured gain is therefore an
  **upper bound**, and plausibly smaller than the PIT haircut.

**Ruling**: the GBM is out of the Phase-21 risk path. Not falsified — shelved, with its exact price
recorded (+0.005 IC) so the decision is revisitable if the cost side ever collapses. If it is ever
revived, it must first be re-scored on point-in-time macro via `ts_macro_vintage`, not revised series.

### 13.4 Rank is not level — the number nobody has measured

Every vol result on record before today (0.47/0.62, 0.53/0.70) was a **rank** IC: it says *which names*
will be jumpier, in order. Position sizing needs a **level** — a σ in daily return units — and until
§13.0a nothing in this repo had ever validated one. `labels.forward_realized_vol` returns
log-of-daily-vol (`log=True`, not annualised); `annualize_vol` is a separate helper, so the
log→level conversion must be explicit at every boundary.

**Measured answer**: plain HAR clears the calibration test at both horizons (slope 1.187 / 1.098, R²
0.442 / 0.523) and — critically — **never left [0.8, 1.2] in any of the six folds**, including COVID.
Its levels are admissible for sizing **with** the fitted shrinkage `σ̂ = a + b·σ_pred` applied, not raw.
naive_gk is better calibrated on average (slope 1.069 / 1.018) but explains less variance; it is the
fallback when HAR's in-fold fit is unavailable or degenerate. The GBM fails at h=5 (slope 1.396) and
HAR-63 fails in the COVID fold at h=5 (1.355) — neither drives levels (§13.0c).

Standing rules:
1. **Ranks drive relative decisions** (which names to trim or avoid); **levels drive sizing** and come
   from plain HAR at h=21 only.
2. Levels apply **only while the live calibration slope stays in band**, re-checked by the §13.6 grader.
   If it drifts out, sizing reverts to equal-weight and annotation keeps recording — degrade to the
   status quo, never to an uncalibrated number.
3. **Anchor live expectations to the most recent fold** (2023–2026: IC 0.46 h=5 / 0.65 h=21), never to
   the full-sample 0.52/0.70 — vol forecastability decays measurably from the GFC era toward the present.

### 13.5 Architecture

- **`apps/api/app/ml/vol_scores.py`** (new) — per-name daily scorer. Imports `_gk_daily_vol` and the HAR
  fit from `vol_baselines.py` and runs **both** estimators per §13.3: HAR-63 for the rank column, plain
  HAR for the level column. Emits per symbol, per horizon (5, 21): `pred_vol` (daily σ, HAR, shrinkage
  applied) plus a `level_admissible` flag that is **false at h=5** — the level is still stored at h=5 so
  the grader can monitor its calibration, but consumers may not size off it (§13.3); `rank`/`pctile`
  (HAR-63) against a **fixed
  147-name reference panel** (not the scored set — otherwise adding a watchlist name silently reshuffles
  every rank), `estimator`, `calib_slope` (so the §13.6 grader can trip rule 2 without a refit), `n_obs`.
- **Table `ml_vol_scores` in `data/market.duckdb`**, written through the shared `DuckStore`
  (`db/duck.py:20-66`, single writer, lock-serialised). ~150 rows × 2 horizons/day is trivial; it needs
  live serving, and the "keep it out of the main DB" convention exists for *big slow panels*, not small
  serving tables. Columns: `ts, symbol, horizon, estimator, pred_vol, pctile, rank, n_names, created_at`.
- **Daily job** in `scheduler/jobs.py::build_scheduler`, **default OFF** behind
  `MARKET_VOL_SCORES_ENABLED`, copying the `ml_snapshot` pattern verbatim (`jobs.py:742-762`): async
  wrapper, `run_in_executor` for the blocking work, `max_instances=1, coalesce=True`, and the
  `elif duck is not None: log.info(...)` disabled-hint line.
- **Price refresh**: promote `scratchpad/fetch_universe.py` to `apps/api/app/ml/universe.py` with an
  incremental mode (fetch only bars after `max(ts)` per symbol), scheduled before the scorer.
- **`GET /api/ml/vol-scores`** — latest cross-section, optional `?symbol=`.
- **Strategist `vol_rank` tool** — `Tool("vol_rank", …)` in `edge/strategist_tools.py:360-409`, read-only,
  same shape as `_tool_quote` (`strategist_tools.py:203-218`).

### 13.6 Shadow mode and the pre-registered promotion bar

Phase A **annotates only** — no order, size, or stop changes.

- **Day sleeve**: write the annotation into `day_signal_journal.context` (already a free-form JSON blob,
  `db/schema.py:922-950`) — no migration.
- **Swing sleeve**: `bot_proposals.rationale` is JSON but semantically owned by strategist evidence.
  Use a separate `ml_vol_shadow` table keyed on `(proposal_id, symbol, ts)` instead of overloading it.
- Each annotation records: `pred_vol`, `pctile`, and the **counterfactual** — the qty and stop distance
  the vol rule *would* have produced, alongside what actually happened.
- **Grading is of the forecast first, P&L second.** Forecast grading is clean (predicted vs realised vol,
  no confounds). P&L grading is confounded the moment the rule changes behaviour, which is exactly why
  Phase A must not change behaviour.

**Promotion criteria, fixed now, before any data exists** — all four must hold before any live knob moves:
1. Live per-name rank IC ≥ 0.30 at h=5 over ≥ 30 trading days (vs 0.53 in backtest; a large haircut,
   because live coverage is thinner and the universe differs).
2. Level calibration slope ∈ [0.8, 1.2] on live data, or an explicitly fitted shrinkage in place.
3. Counterfactual stop-outs strictly reduced, with realised P&L no worse.
4. Every counterfactual trade still clears the $5 min-risk fee gate (`config.py:498`) — see §13.7.

### 13.7 Live wiring — Phase B, a SEPARATE sign-off, explicitly not covered by this approval

- **Swing**: inverse-vol weights on top of strategist target weights, **capped by available cash, never
  margin** — the cash-only rule is absolute here (it previously caused a −$18k cash position).
- **Day**: replace the 3-tier step `risk_scale` (`intraday.py:106-114`) with a continuous per-name scale.
  **Two fee interactions that must be checked in this order:** vol-scaling shrinks size, which can push
  `risk_d = stop_dist × qty` *below* the $5 min-risk gate and silently kill trades; and vol-widened stops
  raise `risk_d`, which can inflate per-trade risk past intent. The gate must be re-evaluated **after**
  vol scaling, never before.
- **Fail-safe**: scores carry a TTL (ignore if older than 3 sessions); any failure falls back to current
  behaviour. The bots must never trade on a stale vol score, and must never *stop* trading because the
  scorer is down.

### 13.8 Risks

- **Inverse-vol sizing is itself a factor bet** — it systematically overweights low-vol names (the
  low-vol anomaly, and a crowded one). Cap per-name weight regardless of vol.
- **Survivorship**: the 147-name universe is current-membership only (documented in
  `fetch_universe.py:6-9`); delisted names are absent, so backtest vol dispersion is understated.
- **Mixed adjustment**: C and CMCSA are split-adjusted while the other 145 are total-return. Minor for
  vol, real for anything return-based.
- **Coverage asymmetry**: crypto and metals get no reference-panel percentile. Either score them against
  their own history or leave them unscored — do not silently rank them against equities.
- **Regime**: vol persistence is strongest in calm and clustered-stress regimes and breaks at turning
  points, which is precisely when sizing matters most. The overlay's existing stress gate already
  underperformed in backtest and was demoted to informational (`vol_overlay.py:165-167`) — do not
  reintroduce a hard regime gate here.

### 13.9 Sequence, with kill points

| Step | Deliverable | Kill point |
|---|---|---|
| 0 | Re-run `vol_baselines` + orthogonalised-residual and level-calibration tests | **DONE 2026-08-14** (§13.0a) — HAR is the estimator; GBM restricted to a candidate tilt pending the stronger controls |
| 1 | Incremental price refresh job for `day_universe ∪ watchlist` | — (pure infrastructure, no risk) |
| 2 | `vol_scores.py` + `ml_vol_scores` + default-OFF daily job + API route | — |
| 3 | `vol_rank` strategist tool | — (read-only, no bot risk) |
| 4 | Shadow annotations + forecast grader | Stop if live IC < 0.30 after 30 sessions (§13.6) |
| 5 | **Separate sign-off** — live sizing/stop wiring | Gated on all four §13.6 criteria |

### 13.10 Net honest take

The valuable output of this phase is **not** a better vol model — the best available vol model is a
three-parameter HAR regression on Garman-Klass vol, barely ahead of a zero-parameter 21-day average
that has been in production since June. The value is
(a) extending it from one market-level number to per-name, (b) giving the swing sleeve any volatility
awareness at all, and (c) building the persist-and-grade loop the market-level overlay never got, so
the next vol claim is settled by live data instead of a backtest IC. If step 4's live grading fails,
stop at step 3 — a strategist-readable vol rank with no bot wiring is still a real, safe deliverable.
