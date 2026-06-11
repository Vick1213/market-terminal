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

**Sources for the two time-sensitive 2026 pivots I re-verified:** [Bluesky firehose free / no paid tier (Blotato 2026)](https://www.blotato.com/blog/bluesky-api-pricing) · [Bluesky cashtags Jan 2026 (TechCrunch)](https://techcrunch.com/2026/01/16/bluesky-rolls-out-cashtags-and-live-badges-amid-a-boost-in-app-installs/) · [CCXT Pro WebSockets merged into free CCXT (GitHub #15171)](https://github.com/ccxt/ccxt/issues/15171). All other source URLs are inline in the research dimensions above and the master table.