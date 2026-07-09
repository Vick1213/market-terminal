# Data-Source Licensing Audit

**Date:** 2026-07-09
**Scope:** every external data source touched by `apps/api` (FastAPI backend), assessed for use in a **paid, local-first, BYO-API-key desktop product**.
**Method:** full source sweep of `apps/api/app` (every ingestor/fetcher module read end-to-end, cross-checked against a repo-wide grep for `httpx|requests\.get|yfinance|feedparser|urlopen|aiohttp` and a regex sweep for every literal `https?://` host constant) + live web research of each provider's current Terms of Service / API terms, cited below. No code was changed as part of this audit.

## Key finding before the table

**BYO-key does not automatically make a source compliant.** Whether a "bring your own API key" architecture cures a vendor's licensing restrictions depends entirely on that vendor's specific ToS language:

- **Tiingo says so explicitly, in writing:** *"If you are a developer and are building software for your audience that requires users to submit their own Tiingo API token in order to use your software, and your software is not distributing our data, you do not need to contact us regarding licensing."* This is the clean template our BYO-key architecture wants everywhere.
- **Polygon.io explicitly forbids it anyway:** its Individual-plan Market Data Terms state *"you may not use the Market Data to build an application intended for use by end users other than you"* — i.e., a BYO-key desktop tool that other people install is out of scope for the Individual tier no matter whose key is in it.
- **Alpaca's own support FAQ** ("Can I redistribute Alpaca API data via my platform?") answers with a flat *"you cannot redistribute Alpaca API data"* and does not carve out the BYO-key case at all.
- **EODHD** requires a paid "Professional User" / commercial plan the moment you are "displaying data to end users" or "using data within a business application," regardless of who holds the key.

So each source below had to be checked on its own text, not assumed safe just because a customer supplies the key. Several premium market-data vendors (Polygon Individual, EODHD Non-Professional) are **not** safe drop-in replacements for exactly this reason.

---

## Summary table

| Source | Used for | Auth model | Rating | Action |
|---|---|---|---|---|
| **SEC EDGAR** (`data.sec.gov`, `www.sec.gov`, `efts.sec.gov`) | 8-K/10-K/10-Q feed, Form 4 insider buys (watchlist + market-wide scan), 13F whale tracker, Item-1A filings diff, 8-K/13D-13G scanners | Keyless, descriptive User-Agent required | 🟢 GREEN | None — public domain |
| **FRED / ALFRED** (`api.stlouisfed.org`) | Macro/yield/credit/vol series, ALFRED point-in-time vintages, FRED release-date calendar | Free API key (BYO) | 🟢 GREEN | None — attribute FRED per ToS |
| **US Treasury FiscalData** (`api.fiscaldata.treasury.gov`) | Daily TGA balance, Treasury auction demand | Keyless | 🟢 GREEN | None |
| **NY Fed Markets API** (`markets.newyorkfed.org`) | ON RRP daily total | Keyless | 🟢 GREEN | None |
| **CFTC** (`publicreporting.cftc.gov`, Socrata) | Legacy COT + TFF positioning | Keyless | 🟢 GREEN | None |
| **EIA** (`ir.eia.gov`) | Weekly petroleum/nat-gas fundamentals, crack spread | Keyless | 🟢 GREEN | None |
| **Federal Reserve Board** (`www.federalreserve.gov`) | FOMC calendar scrape, FOMC statement diff, Fed speech RSS | Keyless | 🟢 GREEN | None |
| **Cleveland Fed** (`www.clevelandfed.org`) | Daily inflation nowcast | Keyless | 🟢 GREEN | None |
| **GDELT** (`api.gdeltproject.org`) | Broad-market news firehose (macro/sector themes) | Keyless | 🟢 GREEN | Keep attribution+link on any redistribution |
| **Academic policy-risk indices** — GPR (`matteoiacoviello.com`), TPU (`policyuncertainty.com`) | Geopolitical Risk index, Trade Policy Uncertainty index | Keyless | 🟢 GREEN | Keep citation |
| **FINRA Reg SHO daily files** (`cdn.finra.org`) | Market-wide + per-ticker daily short-sale volume | Keyless | 🟢 GREEN | None found restricting reuse |
| **IBKR Client Portal** (`localhost:5000`, user's own gateway) | Broker reads/writes on the user's own funded account | User's own IBKR login via locally-run gateway | 🟢 GREEN | None — not a data-redistribution question |
| **Alpaca paper/live trading** (`paper-api.alpaca.markets`) | Order submission/cancel for the bot | User's own Alpaca account + BYO key | 🟢 GREEN | None — this is Alpaca's core supported use case |
| **LLM providers** (Ollama local / `api.openai.com` / `api.deepseek.com` / `api.anthropic.com`) | Brief + strategist narrative generation | Local model or BYO key | 🟢 GREEN | None |
| **ntfy.sh** | Optional push alerts | User-chosen random topic, opt-in | 🟢 GREEN | None |
| **CCXT public streams** (Coinbase, Kraken) | Live crypto trade/order-book firehose | Keyless public WS | 🟢 GREEN (light) | None — standard public exchange feeds |
| **Alpaca market data** (`data.alpaca.markets`) | Daily bars, live IEX snapshots, day-trader 1-min bars | User's own Alpaca account + BYO key | 🟡 YELLOW | Get written confirmation from Alpaca before shipping; FAQ gives only a blanket "no redistribution" |
| **gold-api.com** | XAU/XAG spot | Keyless | 🟡 YELLOW | Low risk, but no formal commercial license exists; easy to swap |
| **BIS Stats** (`stats.bis.org`) | Global central-bank balance sheets | Keyless | 🟡 YELLOW | Confirm the raw-statistics-vs-"BIS Material" distinction before relying on it commercially |
| **DBnomics** (`api.db.nomics.world`) | China/G20 CLI, Euro-area BCI | Keyless | 🟡 YELLOW | DBnomics' own ToS not directly confirmed (underlying OECD series are normally open) |
| **Bluesky AppView** (`api.bsky.app`) | Cashtag social sentiment | Keyless | 🟡 YELLOW | ToS page didn't fully resolve; no paid tier/app review found, likely fine |
| **Finnhub** (`finnhub.io`) | Company news fallback, could replace FMP earnings calendar | Free key (BYO) | 🟡 YELLOW | Commonly used this way; explicit redistribution clause not confirmed in this audit — verify before depending on it |
| **ApeWisdom** (`apewisdom.io`) | Reddit mention-volume leaderboard | Keyless | 🟡 YELLOW | No published ToS at all — email the operator or replace |
| **Tradestie** (`tradestie.com`) | WSB comment sentiment (already unreliable/Cloudflare-gated) | Keyless | 🟡 YELLOW | Low-value feature; drop or replace |
| **FINRA Query API** (`api.finra.org`) | True (bi-monthly) short interest | Keyless (registration implied) | 🟡 YELLOW (high) | Confirmed terms: "non-commercial personal or professional use only," attribution + no further redistribution by end users — needs a signed FINRA developer/redistribution agreement before a paid feature |
| **Polymarket** (`gamma-api.polymarket.com`, `data-api.polymarket.com`) | Geopolitical prediction-market odds, front-running divergence engine | Keyless | 🟡 YELLOW | Trading is geoblocked for US persons but "data is viewable globally"; explicit commercial-API clause not found — get legal confirmation given regulatory sensitivity |
| **ICI.org** | Weekly fund flows + money-market fund assets (Excel) | Keyless scrape | 🟡 YELLOW | "All rights reserved," no reuse policy found — confirm or drop |
| **NAAIM** (`naaim.org`) | Weekly manager exposure index | Keyless scrape of a linked Excel | 🟡 YELLOW (high) | No ToS found at all for this professional association's proprietary index — treat cautiously |
| **Yahoo Finance / yfinance** (`feeds.finance.yahoo.com`, `query1/2.finance.yahoo.com` via the `yfinance` lib) | **Primary daily-bar price history, live quotes, `^MOVE`, earnings dates, per-ticker RSS** | Keyless, unofficial | 🔴 RED | Replace — see swap plan below (highest priority, most load-bearing) |
| **Senate eFD** (`efdsearch.senate.gov`) | Congressional PTR (stock trade) tracker | Keyless, click-through "prohibition agreement" | 🔴 RED | Federal statute (not just ToS) bars commercial use — remove feature or license from a compliant reseller |
| **Seeking Alpha** (`seekingalpha.com`) | Per-ticker analyst-angle RSS | Keyless | 🔴 RED | Drop — Finnhub/EDGAR/GDELT already cover the fallback |
| **Dow Jones / MarketWatch** (`feeds.content.dowjones.io`) | Broad-market topic RSS | Keyless | 🔴 RED | Drop |
| **Investing.com** (`www.investing.com`) | Broad-market topic RSS | Keyless | 🔴 RED | Drop — ToS explicitly bars "making Market Information available... in an application" |
| **CNBC** (`search.cnbc.com`) | Broad-market topic RSS | Keyless (internal widget feed) | 🔴 RED (unconfirmed) | Specific RSS ToS text not found; treated conservatively by analogy to sibling media ToS — drop or get explicit confirmation |
| **StockTwits** (`api.stocktwits.com`) | Bull/bear tagged per-ticker messages | Keyless (browser-UA scrape, not the official API) | 🔴 RED | Personal/non-commercial license only; not currently accepting new developer registrations at all |
| **Kalshi** (`api.elections.kalshi.com`) | FOMC/CPI probability ladder | Keyless | 🔴 RED | Explicit: personal/non-business use only, no AI/ML use, no redistribution without written permission |
| **CBOE** (`cdn.cboe.com`, `www.cboe.com`) | VIX/VIX3M/VVIX/VIX9D/SKEW/COR3M history, put/call scrape, GEX delayed-options chain | Keyless | 🔴 RED | Market Data Policy explicitly prohibits auto-extraction and requires a signed Data Agreement + fees for redistribution; Cboe actively IP-blocks violators |
| **AAII** (`www.aaii.com`) | Bull/neutral/bear sentiment survey | Keyless scrape | 🔴 RED | ToS: "No part of the contents... may be copied or forwarded" |
| **FMP** (`financialmodelingprep.com`) | Market-wide earnings calendar (off by default without a key) | Free key (BYO) | 🔴 RED | Free/personal tier explicitly bars "Commercial Use" and requires a Data Display & Licensing Agreement to show FMP data in an app |

**Dead/vestigial hosts** (rate-limited / labeled in `app/ingest/http.py` and `app/ingest/source_health.py` but no longer actually fetched by any current ingestor, per the docstrings in `app/ingest/prices.py` and `app/ingest/multiasset.py`): `stooq.com` (replaced by yfinance — itself now RED, see swap plan), `api.coingecko.com` (replaced by CoinPaprika). No rating needed; flag for cleanup so they don't reappear as false leads in a future audit.

**Counts:** 16 GREEN, 12 YELLOW, 13 RED (41 sources total; RED items unconfirmed-but-treated-conservatively: CNBC; YELLOW items genuinely unconfirmed: BIS raw-data-vs-Material split, DBnomics own ToS, Bluesky ToS page, Finnhub redistribution clause, ApeWisdom/Tradestie/NAAIM/ICI.org — no ToS text found at all for these four).

---

## Per-source detail

### 🟢 GREEN — safe as-is

**SEC EDGAR** — `apps/api/app/ingest/news.py`, `app/edge/whales.py`, `app/edge/insider.py`, `app/edge/insider_scan.py`, `app/edge/filings_diff.py`, `app/ingest/edgar_events.py`. U.S. government content created by federal employees is not subject to domestic copyright (17 U.S.C. §105) and SEC states site/EDGAR content is "free to access and reuse." Only the **EDGAR/SEC trademarks** are restricted (don't put "EDGAR" in a product name). [SEC Webmaster FAQ](https://www.sec.gov/about/webmaster-frequently-asked-questions) · [Accessing EDGAR Data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)

**FRED / ALFRED** — `apps/api/app/ingest/macro.py`, `app/edge/calendar.py`. Requires a free API key. Series tagged Public-Domain or Copyright may be used commercially "internal commercial uses... displayed in textbooks, newsletters, or reports to clients" provided FRED and the original source are credited and no sponsorship/endorsement is implied. [FRED API Terms of Use](https://fred.stlouisfed.org/docs/api/terms_of_use.html) · [Legal Notices](https://fred.stlouisfed.org/legal/)

**US Treasury FiscalData / NY Fed / CFTC / EIA / Federal Reserve Board / Cleveland Fed** — all direct U.S. (or quasi-) government sources, keyless, published specifically for public dissemination. No commercial-use restriction found on any of them. Files: `app/ingest/liquidity.py`, `app/ingest/treasury.py`, `app/edge/cot.py`, `app/ingest/cftc_tff.py`, `app/ingest/eia.py`, `app/edge/calendar.py`, `app/ingest/fed_speeches.py`, `app/edge/fomc_diff.py`, `app/ingest/cleveland.py`.

**GDELT** — `apps/api/app/ingest/news.py`. "All datasets released by the GDELT Project are available for unlimited and unrestricted use for any academic, commercial, or governmental use of any kind without fee," redistribution allowed with attribution + link. [GDELT About](https://www.gdeltproject.org/about.html)

**GPR / TPU academic indices** — `apps/api/app/ingest/policyrisk.py`. Both explicitly labeled "Public Domain: Citation Requested." [policyuncertainty.com](https://www.policyuncertainty.com/all_country_data.html) · [matteoiacoviello.com/gpr.htm](https://www.matteoiacoviello.com/gpr.htm)

**FINRA Reg SHO daily short-volume files** — `apps/api/app/ingest/macro.py` (`FINRA_URL`), `app/ingest/multiasset.py`. Published by FINRA "free of charge" pursuant to an SEC request, specifically for public dissemination ("media-reported trades"). Distinct product from the FINRA Query API catalog below — no commercial restriction found. [FINRA Daily Short Sale Volume Files](https://www.finra.org/finra-data/browse-catalog/short-sale-volume-data/daily-short-sale-volume-files)

**IBKR Client Portal** — `apps/api/app/trading/ibkr.py`. Not a licensing question at all: the user runs their own gateway, logs in with their own IBKR credentials, and no data passes through us. Same category as any other broker's official app.

**Alpaca paper/live trading (order execution)** — `apps/api/app/trading/broker.py`. Distinct from Alpaca's *market data* feed below — order placement/cancel/read against the customer's own account is exactly Alpaca's core third-party-developer product ("Developer-first API for Stock, Options, Crypto Trading").

**LLM providers** — `apps/api/app/edge/llm.py`. Ollama runs fully local; OpenAI/DeepSeek/Anthropic are standard BYO-key chat-completions APIs explicitly designed for third-party application use.

**ntfy.sh** — `apps/api/app/edge/ntfy.py`. Opt-in, user-picked random topic; not a licensed-data question.

**CCXT public streams (Coinbase, Kraken)** — `apps/api/app/ingest/crypto.py`. Public, keyless market-data websockets on two large regulated exchanges; standard for retail trading tools. Flagged light-green only because generic exchange market-data agreements can in principle restrict "professional"/commercial redistribution of raw tape data — worth a footnote, not a blocker.

### 🟡 YELLOW — needs a look, not a hard blocker for day 1

**Alpaca market data** — `apps/api/app/ingest/alpaca.py`, used from `app/ingest/prices.py`. Alpaca's own support FAQ: *"you cannot redistribute Alpaca API data,"* with no BYO-key carve-out language (unlike Tiingo's explicit one). Alpaca's business model clearly targets third-party developer apps, so this is very likely fine in practice for a one-account-one-key-one-user architecture, but get it in writing before relying on it as the *primary* replacement for Yahoo. [Alpaca: Can I redistribute Alpaca API data via my platform?](https://alpaca.markets/support/redistribute-alpaca-api)

**FINRA Query API (true short interest)** — `apps/api/app/edge/short_interest.py`. Confirmed developer terms allow "redistribute[ing]... to third-party end users for non-commercial personal or professional use only," with mandatory FINRA attribution and a duty to flow the restriction down to end users — this is a real, usable path, but it requires actually enrolling in FINRA's developer program/agreement, not just calling the keyless-looking endpoint. [FINRA: Specific Terms for Equity Data](https://developer.finra.org/specific-terms-equity-data)

**Kalshi's cousin, Polymarket** — `apps/api/app/ingest/polymarket.py`. Trading is geoblocked for US persons but Polymarket states "data and information is viewable globally"; no explicit commercial-API-reuse clause was found in this audit. Given prediction markets' regulatory sensitivity, get counsel to read the current [Terms of Use](https://polymarket.com/tos) and [geoblock docs](https://docs.polymarket.com/api-reference/geoblock) before shipping this panel commercially.

**BIS, DBnomics, Bluesky, Finnhub, ApeWisdom, Tradestie, ICI.org, NAAIM, gold-api.com** — see summary table; each either has an ambiguous non-commercial/commercial split in its published terms (BIS: [terms_statistics.htm](https://www.bis.org/terms_statistics.htm)) or simply has no published terms at all (ApeWisdom, Tradestie, NAAIM, ICI.org — could not find *any* ToS document for these four despite searching). Treat the ones with no ToS as **undocumented risk**, not confirmed-safe.

### 🔴 RED — must be replaced or removed before charging money

**Yahoo Finance / yfinance** — `apps/api/app/ingest/prices.py` (primary daily bars + live quotes), `app/ingest/macro.py` (`^MOVE`), `app/edge/calendar.py` (earnings dates), `app/ingest/news.py` (per-ticker RSS). Yahoo's ToS prohibits "robots, spiders, crawlers, scrapers, or other automated means... to access the Services or extract data," and `yfinance` is an unofficial wrapper that works around Yahoo's anti-bot TLS fingerprinting (the code's own docstring confirms this: "yfinance ships curl_cffi browser impersonation"). This is the single most load-bearing source in the app. [Yahoo ToS discussion / yfinance commercial-risk summary](https://www.quarkip.com/blog/guides/3463)

**Senate eFD PTR tracker** — `apps/api/app/edge/congress.py`. Not merely a ToS violation — the Ethics in Government Act makes it **unlawful** to obtain or use a Financial Disclosure Report "for any commercial purpose, other than by news and communications media for dissemination to the general public," with civil penalties. The site's click-through "prohibition agreement" (which the scraper programmatically accepts) exists specifically to put users on notice of this statute. [Senate Ethics: Financial Disclosure](https://www.ethics.senate.gov/public/index.cfm/financialdisclosure) · [efdsearch.senate.gov](https://efdsearch.senate.gov/search/home/)

**Seeking Alpha RSS** — `apps/api/app/ingest/news.py`. "Use of Seeking Alpha's RSS feed is limited to personal, non-commercial use," and separately the ToS prohibits any scraping/data-mining. [Seeking Alpha Terms of Use](https://about.seekingalpha.com/terms)

**Dow Jones / MarketWatch RSS** — `apps/api/app/ingest/news.py` (`BROAD_FEEDS`). "For your personal, non-commercial use only... governed by the Subscriber Agreement." [Dow Jones RSS terms (via S&P DJI)](https://www.spglobal.com/spdji/en/rss/)

**Investing.com RSS** — `apps/api/app/ingest/news.py` (`BROAD_FEEDS`). Explicit and strong: "prohibited from copying, storing, selling, licensing, distributing... any Market Information," and separately bars "mak[ing] the Market Information available on any website or in an application." [Investing.com Terms and Conditions](https://www.investing.com/about-us/terms-and-conditions)

**CNBC RSS** — `apps/api/app/ingest/news.py` (`BROAD_FEEDS`, `search.cnbc.com`). This audit could not locate CNBC/NBCUniversal's specific RSS terms text; treated conservatively as RED by analogy to every sibling media outlet checked (Dow Jones, Investing.com, Seeking Alpha all restrict to personal/non-commercial use) since `search.cnbc.com/.../combinedcms` is an internal widget endpoint, not a published open feed. **Unconfirmed — flag for explicit legal review**, but do not ship on the current assumption it's fine.

**StockTwits** — `apps/api/app/ingest/retail.py`. General ToS: "solely for their personal, non-commercial use," and scraping outside "an approved API, widget, developer offering" is barred; StockTwits is also "currently reviewing all of its APIs... and is not accepting new registrations" — so even seeking a commercial license is not currently possible. [StockTwits Terms of Service](https://content.stocktwits.com/terms) · [Stocktwits for Developers](https://api.stocktwits.com/developers)

**Kalshi** — `apps/api/app/ingest/kalshi.py`. Explicit and unusually strict: data "restricted to personal, non-business use," bans "AI or machine learning in any form," and forbids "shar[ing]... with anyone else... without Kalshi's written permission" — displaying the derived FOMC-rate expectation inside a paid product is squarely what this prohibits. [Kalshi Data Terms of Service](https://kalshi-public-docs.s3.amazonaws.com/kalshi-data-terms-of-service.pdf) · [API Developer Agreement](https://kalshi.com/developer-agreement)

**CBOE** — `apps/api/app/ingest/macro.py` (`CBOE_HISTORY`, put/call scrape), `app/edge/gex.py` (delayed options chain). Cboe's Market Data Policy explicitly states auto-extraction is "strictly prohibited... Cboe will block IP addresses of parties who attempt to do so," and any redistribution (even of delayed data) requires a signed Data Agreement, a Data Order Form, and (often) fees. This is the biggest single licensing risk in the codebase because it underlies the VIX-complex panel *and* the entire self-computed GEX feature. [Cboe Market Data Policies (PDF)](https://cdn.cboe.com/resources/membership/Market_Data_Policies.pdf)

**AAII** — `apps/api/app/ingest/macro.py` (`fetch_aaii`). "No part of the contents of the website or newsletter may be copied or forwarded to anyone else" — a membership/subscription business whose free snapshot page is still covered by this clause. [AAII Terms of Use](https://www.aaii.com/privacy/tos)

**FMP** — `apps/api/app/edge/calendar.py` (earnings calendar, off by default without a key). Free/personal tier: "Customers may not use the Data or Services for any Commercial Use," and separately "displaying or redistributing data sourced from FMP requires a specific Data Display and Licensing Agreement" — this requirement attaches to the *application* displaying the data, not just the key holder's personal use, so BYO-key does not cure it. [FMP Terms of Service](https://site.financialmodelingprep.com/terms-of-service)

---

## Prioritized swap list

### Blocks charging money on day 1 (ship-blockers — remove or replace before the first paid release)

1. **Yahoo Finance / yfinance** — the primary price engine. Promote **Alpaca market data** (already wired as the fallback in `app/ingest/prices.py`/`app/ingest/alpaca.py`) to primary for US equities/ETFs/crypto, and add a small **Tiingo** adapter (explicitly BYO-key-safe per its own ToS, quoted above) for what Alpaca can't cover: COMEX metals proxies (currently `GC=F`/`SI=F`), the `^MOVE` bond-vol index, and earnings dates. Concretely: swap `yf.Ticker(...).history()`/`fast_info` calls in `prices.py`, `macro.py::_fetch_move_blocking`, and `calendar.py::_earnings_events` for Tiingo/Alpaca equivalents; drop the Yahoo-RSS leg of `news.py` entirely (Finnhub + EDGAR + GDELT already cover the fallback per the module's own docstring).
2. **CBOE** (VIX complex + GEX) — drop the self-computed GEX panel (`edge/gex.py`) outright, since it depends entirely on the protected delayed-options-chain endpoint. For VIX/VIX3M level, FRED's `VIXCLS` series is already ingested and is a fine free substitute; VVIX/VIX9D/SKEW/COR3M/put-call have no confirmed free replacement — drop them from the paid SKU or pursue a Cboe DataShop / licensed-reseller (e.g. Nasdaq Data Link) subscription.
3. **Senate eFD (Congress PTR tracker)** — remove this panel from the commercial product; the restriction is statutory, not contractual. If the feature matters, license it from a compliant paid reseller (e.g. Quiver Quantitative, Capitol Trades) that has already solved the redistribution problem.
4. **Kalshi (FOMC/CPI panel)** — remove or link out to kalshi.com instead of computing/embedding the probability ladder in-app.
5. **News RSS block** — drop Seeking Alpha, Dow Jones/MarketWatch, Investing.com, and (pending confirmation) CNBC from `ingest/news.py::BROAD_FEEDS` and `fetch_seekingalpha`. GDELT + Finnhub + EDGAR already form a legally clean fallback chain per the module's existing design.
6. **StockTwits** — drop from `ingest/retail.py`; keep ApeWisdom (YELLOW, cheap to swap later) and Bluesky (YELLOW-low) as the remaining retail-sentiment legs.
7. **AAII** — drop `fetch_aaii`.
8. **FMP** — drop the FMP leg of `edge/calendar.py`; it's already off by default without a key, so this is a documentation-only fix (don't recommend customers add an FMP key). Consider Finnhub's earnings-calendar endpoint (already an integrated provider) as a like-for-like replacement, pending its own redistribution-clause confirmation.

### Can wait / lower priority (fix before scaling, get written confirmation rather than assuming)

- **Alpaca market data** — email Alpaca for explicit written confirmation of the BYO-key/local-app pattern; very likely fine, not yet in writing.
- **FINRA Query API (short interest)** — enroll in FINRA's actual developer/redistribution program rather than relying on the bare endpoint.
- **Polymarket** — get counsel to review current ToS before scaling the divergence-engine feature commercially.
- **NAAIM, ICI.org, ApeWisdom, Tradestie** — reach out to each operator for explicit permission, or quietly drop these lower-value panels if no answer arrives; none currently has a discoverable ToS at all.
- **BIS, DBnomics, Bluesky, Finnhub** — lower risk, but tie up the loose end (confirm the specific reuse terms) before marketing these panels as a differentiator.

---

## Sources whose terms could not be fully confirmed

Flagged inline above; consolidated here for visibility:

- **CNBC** RSS-specific terms (treated conservatively as RED by analogy — no primary-source text found)
- **BIS** raw-statistics-vs-"BIS Material" redistribution split (the general terms are clear only for "BIS Material"/publications, not the SDMX statistics API specifically)
- **DBnomics'** own terms of use (only the underlying OECD/Eurostat open-data norms were confirmed, not DBnomics' own aggregator ToS)
- **Bluesky** ToS page (`bsky.app/support/tos`) did not resolve during this audit; inferred permissive from the absence of any paid tier/app-review gate
- **Finnhub** explicit redistribution/BYO-key clause (widely used this way in practice, but no ToS text found addressing it directly)
- **ApeWisdom, Tradestie, NAAIM, ICI.org** — no ToS document could be found for any of these four at all
- **CCXT/exchange market-data redistribution terms** — assessed generically (large public exchanges, no special restriction found), not confirmed exchange-by-exchange

---

## Implementation status (M2.5, 2026-07-09)

A `MARKET_PROFILE` env setting (`personal` default / `commercial`) and a
central gate registry now exist — `apps/api/app/profile.py` — so this is no
longer purely a paper audit. Personal profile is unchanged (every source
still runs). Setting `MARKET_PROFILE=commercial` gates the following off
cleanly (no-op, never raises, reported in `/api/health/sources` as
`"disabled (profile)"`):

- **Senate eFD congress tracker** — `app/edge/congress.py::CongressPipeline.run`
- **Seeking Alpha RSS** — `app/ingest/news.py::fetch_seekingalpha`
- **Dow Jones/MarketWatch + Investing.com + CNBC RSS** — `app/ingest/news.py::fetch_broad_rss` (GDELT/EDGAR/Finnhub untouched — the commercial-safe news path)
- **StockTwits** — `app/ingest/retail.py::RetailPipeline._stocktwits_symbol`
- **Kalshi** — `app/ingest/kalshi.py::KalshiPipeline.run`
- **CBOE** (VIX-complex history/put-call + GEX) — `app/ingest/macro.py::MacroPipeline.run_cboe` and `app/edge/gex.py::GexAdapter.run` (FRED's `VIXCLS`, ingested separately in `run_fred`, survives as the commercial-safe VIX level)
- **AAII** — `app/ingest/macro.py::MacroPipeline.run_aaii`
- **FMP earnings calendar** — `app/edge/calendar.py::CalendarPipeline._fmp_earnings_events`

**yfinance** (`app/ingest/prices.py`) is handled differently because it has a
clean drop-in replacement rather than just being dropped: a new **Tiingo**
daily-EOD provider (`app/ingest/tiingo.py`, `TIINGO_API_KEY`) is primary in
commercial profile (yfinance itself is gated off there) and an opt-in
fallback in personal profile when a key is set. Tiingo's free equity-EOD
endpoint can't serve crypto, COMEX metals futures (`XAU`/`XAG` → `GC=F`/
`SI=F`), or index tickers (`^MOVE`, `^VIX`) — those degrade gracefully
(stale/missing series, never a crash) unless Alpaca covers them instead.

**Gap closed (follow-up pass):** the three *other* Yahoo-vendor call sites
outside `prices.py` that were previously tracked here as an open gap are now
wired to the same `gated("yfinance", ...)` decorator as every other RED
source (reusing the existing `yfinance` registry key — same vendor, same
ToS risk; `feeds.finance.yahoo.com` was added to that key's `hosts` tuple
alongside the existing `query1`/`query2.finance.yahoo.com`):

- **`app/ingest/macro.py`'s `^MOVE` yfinance leg** —
  `MacroPipeline.run_move`. No free substitute exists, so commercial profile
  simply goes without the MOVE (bond-vol) series; nothing downstream assumes
  it is present, so this is a silent, non-crashing gap by design.
- **`app/edge/calendar.py`'s yfinance earnings-date fallback** — pulled out
  of the blocking `_earnings_events` helper into a new async
  `CalendarPipeline._yfinance_earnings_events` wrapper (needed since `@gated`
  wraps an async function, and the original helper is a sync method run via
  `loop.run_in_executor`) and gated there. FMP's earnings leg
  (`_fmp_earnings_events`) was already gated off separately, so in commercial
  profile the watchlist earnings-date feature has no provider left at all —
  an accepted, clean (empty, non-crashing) gap rather than a partial one.
- **`app/ingest/news.py`'s Yahoo RSS leg** — `fetch_yahoo` (used by both
  `run_yahoo` and the per-symbol `run_symbol` targeted ingest). GDELT + EDGAR
  + Finnhub remain the commercial-safe news path, per the module's own
  docstring.

All three: commercial profile skips cleanly (no network call, never raises,
returns the same-shaped empty default — `None` for `run_move`, `[]` for the
other two) and shows up as `"disabled (profile)"` in `/api/health/sources`
for `feeds.finance.yahoo.com` / `query1.finance.yahoo.com` /
`query2.finance.yahoo.com`. Personal profile: zero behavior change. Covered
by `app/tests/test_profile.py::test_real_call_sites_wired_to_gate` via the
same `__gate_source__` introspection used for every other gate.

**Still needs vendor confirmation / legal follow-up** (unchanged from the
original audit, tracked here so this doesn't get lost):

- **Alpaca market data** — no written confirmation yet that the BYO-key/
  local-app pattern is covered by Alpaca's "no redistribution" FAQ language;
  email Alpaca before marketing it as the primary live-quote leg.
- **Finnhub** — used as a news fallback and a candidate earnings-calendar
  replacement; no ToS text found addressing BYO-key redistribution directly.
- **Polymarket** — trading is geoblocked for US persons but data is "viewable
  globally"; no explicit commercial-API-reuse clause found — get counsel to
  read the current ToS before relying on the divergence-engine panel
  commercially.
- **FINRA Query API** (true short interest, `app/edge/short_interest.py`) —
  confirmed terms allow non-commercial personal/professional redistribution
  with attribution, but require actually enrolling in FINRA's developer/
  redistribution agreement — not gated by `MARKET_PROFILE` yet; either enroll
  or add it to the RED gate list before shipping the commercial build.
