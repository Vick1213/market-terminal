"""News ingestors + pipeline (Panel a).

Free sources, all through the shared rate-limited/conditional-GET
``HttpClient``:
  * Yahoo per-ticker RSS  — pre-mapped to ticker; fragile/unofficial, so its
    failure is logged and swallowed (Finnhub + EDGAR are the fallbacks).
  * SEC EDGAR submissions JSON — authoritative 8-K/10-Q/10-K source, no key,
    descriptive UA required (set globally), <=10 req/s (limited per-host).
  * Finnhub /company-news — optional free key; ingestor disabled without one.
  * GDELT DOC 2.0 — only free no-key broad-market firehose. Queried per
    macro/sector THEME, never per-ticker; the 1 req/5s budget is enforced by
    the per-host limiter in HttpClient.
  * CNBC / MarketWatch / Investing.com topic RSS — breadth; need a browser UA
    (403/Cloudflare on naive fetchers), fragile by design, skip-on-fail.
  * Seeking Alpha per-symbol RSS — analyst angle; same browser-UA treatment.

Market-wide items (no pre-mapped ticker) get entity-localized FinBERT tagging:
watchlist tickers mentioned in the text are scored on their sentence window
(PLAN §3a — per-company sentiment with NO LLM), and a single-ticker article is
tagged with that symbol so it shows up in the per-ticker drill-down.

Pipeline (PLAN §3 dedup is mandatory): normalize URL (strip query/fragment) +
normalize title, hash both, drop the item if EITHER hash was seen
(``news_dedupe`` in SQLite) — else sentiment counts inflate when outlets
syndicate the same story. Fresh items are FinBERT-scored in one batch
(cached by text-hash, never rescored), stored in DuckDB ``news_items``, and
pushed on WS topic "news".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import feedparser

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ingest.http import HttpClient
from app.sentiment import SentimentService
from app.ws.hub import ConnectionManager

log = logging.getLogger("market.news")

NEWS_TOPIC = "news"

YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
EDGAR_TICKERS = "https://www.sec.gov/files/company_tickers.json"
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
EDGAR_DOC = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"
FINNHUB_NEWS = "https://finnhub.io/api/v1/company-news"
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"
SEEKING_ALPHA_RSS = "https://seekingalpha.com/api/sa/combined/{symbol}.xml"

# CNBC/MarketWatch/Investing 403 naive fetchers — they get a real browser UA
# instead of the descriptive one (PLAN §3 a / §8). All are fragile by design.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

BROAD_FEEDS: list[tuple[str, str]] = [
    ("cnbc", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"),  # top news
    ("cnbc", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),   # markets
    ("marketwatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("marketwatch", "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"),
    ("investing", "https://www.investing.com/rss/news_25.rss"),  # market news
]

# GDELT is queried per sector/macro THEME, never per-ticker (PLAN hard rule).
GDELT_QUERIES: dict[str, str] = {
    "fed": '"federal reserve" OR FOMC OR "interest rate decision"',
    "inflation": '"inflation report" OR "CPI report" OR "consumer prices"',
    "equities": '"stock market" OR "wall street" OR "S&P 500"',
    "crypto": "bitcoin OR ethereum OR cryptocurrency",
    "metals": '"gold price" OR "silver price" OR "precious metals"',
    "energy": '"oil price" OR OPEC OR "crude oil"',
}

# Filing forms worth a timeline entry in P1 (8-K alert chips per plan; insider
# Form 4 clustering is a later phase).
EDGAR_FORMS = {"8-K", "10-Q", "10-K", "6-K", "20-F", "S-1"}

# Symbols that look like real exchange tickers (skips BTC/USD, XAU spot, ...).
_TICKER_RE = re.compile(r"^[A-Z]{1,6}(\.[A-Z]{1,2})?$")


@dataclass
class RawNews:
    source: str
    symbol: str | None
    title: str
    summary: str
    url: str
    published: datetime


def _norm_url(url: str) -> str:
    """Strip query (UTM et al) + fragment, lowercase scheme/host."""
    p = urlsplit(url.strip())
    return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path.rstrip('/')}"


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", title.lower())).strip()


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]


def watchlist_tickers(sqlite: SqliteStore) -> list[str]:
    """Watchlist equities/metals plus the news-only custom tickers — every
    per-symbol ingestor (yahoo, EDGAR, finnhub, seekingalpha) covers both."""
    rows = sqlite.fetchall(
        "SELECT symbol FROM watchlist WHERE asset_class IN ('equity', 'metal') ORDER BY sort_order"
    )
    syms = [r["symbol"] for r in rows if _TICKER_RE.match(r["symbol"])]
    extra = sqlite.fetchall("SELECT symbol FROM news_tickers ORDER BY added_at")
    syms += [r["symbol"] for r in extra
             if _TICKER_RE.match(r["symbol"]) and r["symbol"] not in syms]
    return syms


# --------------------------------------------------------------- ingestors


def _entries_to_raw(feed, source: str, symbol: str | None) -> list[RawNews]:
    """Map feedparser entries to RawNews; entries without link/title dropped."""
    items: list[RawNews] = []
    for e in feed.entries:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        published = (
            datetime.fromtimestamp(time.mktime(ts), tz=timezone.utc)
            if ts
            else datetime.now(timezone.utc)
        )
        link = e.get("link", "")
        title = (e.get("title") or "").strip()
        if not link or not title:
            continue
        summary = re.sub(r"<[^>]+>", " ", e.get("summary") or "").strip()
        items.append(
            RawNews(
                source=source,
                symbol=symbol,
                title=title,
                summary=summary,
                url=link,
                published=published,
            )
        )
    return items


async def fetch_rss(
    http: HttpClient,
    url: str,
    source: str,
    symbol: str | None = None,
    headers: dict[str, str] | None = None,
) -> list[RawNews]:
    """One RSS/Atom feed -> RawNews. Every feed here is allowed to fail."""
    try:
        text = await http.get_text(url, headers=headers)
        feed = feedparser.parse(text)
    except Exception as exc:  # fragile-by-design sources — degrade, don't die
        log.warning("%s rss failed (%s): %s", source, url, exc)
        return []
    return _entries_to_raw(feed, source, symbol)


async def fetch_yahoo(http: HttpClient, symbols: list[str]) -> list[RawNews]:
    items: list[RawNews] = []
    for sym in symbols:
        items.extend(await fetch_rss(http, YAHOO_RSS.format(symbol=sym), "yahoo", sym))
    return items


async def fetch_broad_rss(http: HttpClient) -> list[RawNews]:
    """CNBC / MarketWatch / Investing.com topic feeds — market-wide breadth."""
    items: list[RawNews] = []
    for source, url in BROAD_FEEDS:
        items.extend(await fetch_rss(http, url, source, headers=BROWSER_HEADERS))
    return items


async def fetch_seekingalpha(http: HttpClient, symbols: list[str]) -> list[RawNews]:
    items: list[RawNews] = []
    for sym in symbols:
        items.extend(
            await fetch_rss(
                http,
                SEEKING_ALPHA_RSS.format(symbol=sym),
                "seekingalpha",
                sym,
                headers=BROWSER_HEADERS,
            )
        )
    return items


async def fetch_gdelt(http: HttpClient) -> list[RawNews]:
    """GDELT DOC 2.0 ArtList per macro theme. The api.gdeltproject.org host
    limiter (1 req / 5 s) spaces the theme rotation automatically."""
    items: list[RawNews] = []
    for theme, query in GDELT_QUERIES.items():
        try:
            data = await http.get_json(
                GDELT_DOC,
                params={
                    "query": f"({query}) sourcelang:eng",
                    "mode": "ArtList",
                    "format": "json",
                    "maxrecords": "30",
                    "timespan": "6h",
                },
                # Cache key is the bare URL — identical across themes — so
                # conditional GET would serve one theme's body for another.
                conditional=False,
            )
        except Exception as exc:
            log.warning("gdelt query failed (%s): %s", theme, exc)
            continue
        for a in data.get("articles", []) if isinstance(data, dict) else []:
            title = (a.get("title") or "").strip()
            url = a.get("url") or ""
            if not title or not url:
                continue
            try:
                published = datetime.strptime(
                    a.get("seendate", ""), "%Y%m%dT%H%M%SZ"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                published = datetime.now(timezone.utc)
            items.append(
                RawNews(
                    source="gdelt",
                    symbol=None,  # market-wide; entity tagging happens in process()
                    title=title,
                    summary="",
                    url=url,
                    published=published,
                )
            )
    return items


async def _cik_map(http: HttpClient, sqlite: SqliteStore) -> dict[str, int]:
    """ticker -> CIK, fetched from SEC once and cached in app_meta for 7 days."""
    row = sqlite.fetchone("SELECT value FROM app_meta WHERE key = 'edgar_cik_map'")
    if row:
        cached = json.loads(row["value"])
        fetched = datetime.fromisoformat(cached["fetched"])
        if datetime.now(timezone.utc) - fetched < timedelta(days=7):
            return {k: int(v) for k, v in cached["map"].items()}

    data = await http.get_json(EDGAR_TICKERS)
    mapping = {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}
    sqlite.execute(
        "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('edgar_cik_map', ?)",
        [json.dumps({"fetched": datetime.now(timezone.utc).isoformat(), "map": mapping})],
    )
    log.info("edgar cik map refreshed (%s tickers)", len(mapping))
    return mapping


async def fetch_edgar(
    http: HttpClient,
    sqlite: SqliteStore,
    symbols: list[str],
    lookback_days: int = 7,
) -> list[RawNews]:
    cik_by_ticker = await _cik_map(http, sqlite)
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    items: list[RawNews] = []

    for sym in symbols:
        cik = cik_by_ticker.get(sym.upper())
        if cik is None:
            continue
        try:
            sub = await http.get_json(EDGAR_SUBMISSIONS.format(cik=cik))
        except Exception as exc:
            log.warning("edgar submissions failed for %s (CIK %s): %s", sym, cik, exc)
            continue

        recent = sub.get("filings", {}).get("recent", {})
        company = sub.get("name", sym)
        forms = recent.get("form", [])
        for i, form in enumerate(forms):
            if form not in EDGAR_FORMS:
                continue
            try:
                accepted = recent["acceptanceDateTime"][i]
                published = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
            except (KeyError, IndexError, ValueError):
                continue
            if published < cutoff:
                continue
            accession = recent["accessionNumber"][i].replace("-", "")
            doc = recent.get("primaryDocument", [""] * len(forms))[i]
            desc = recent.get("primaryDocDescription", [""] * len(forms))[i] or form
            filing_items = recent.get("items", [""] * len(forms))[i]
            title = f"{company} files {form}: {desc}"
            if filing_items:
                title += f" (items {filing_items})"
            items.append(
                RawNews(
                    source="edgar",
                    symbol=sym,
                    title=title,
                    summary=f"SEC {form} filing, accession {recent['accessionNumber'][i]}",
                    url=EDGAR_DOC.format(cik=cik, accession=accession, doc=doc),
                    published=published,
                )
            )
    return items


async def fetch_finnhub(http: HttpClient, api_key: str, symbols: list[str]) -> list[RawNews]:
    if not api_key:
        return []
    today = datetime.now(timezone.utc).date()
    frm = (today - timedelta(days=2)).isoformat()
    items: list[RawNews] = []
    for sym in symbols:
        try:
            data = await http.get_json(
                FINNHUB_NEWS,
                params={"symbol": sym, "from": frm, "to": today.isoformat(), "token": api_key},
                conditional=False,  # key in params — keep it out of the disk cache
            )
        except Exception as exc:
            log.warning("finnhub company-news failed for %s: %s", sym, exc)
            continue
        for a in data if isinstance(data, list) else []:
            title = (a.get("headline") or "").strip()
            url = a.get("url") or ""
            if not title or not url:
                continue
            items.append(
                RawNews(
                    source="finnhub",
                    symbol=sym,
                    title=title,
                    summary=(a.get("summary") or "").strip(),
                    url=url,
                    published=datetime.fromtimestamp(a.get("datetime", 0), tz=timezone.utc),
                )
            )
    return items


# ----------------------------------------------------------------- pipeline


class NewsPipeline:
    """dedupe -> batch-score -> store -> WS-push, shared by every ingestor."""

    def __init__(
        self,
        duck: DuckStore,
        sqlite: SqliteStore,
        sentiment: SentimentService,
        hub: ConnectionManager,
        http: HttpClient,
        *,
        finnhub_key: str = "",
        edgar_lookback_days: int = 7,
    ) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._sentiment = sentiment
        self._hub = hub
        self._http = http
        self._finnhub_key = finnhub_key
        self._edgar_lookback_days = edgar_lookback_days

    def _dedupe(
        self, items: list[RawNews]
    ) -> tuple[list[tuple[str, RawNews]], list[tuple[str, str]]]:
        """Return (fresh, convergent): fresh = (id, item) for unseen items;
        convergent = (original_item_id, new_source) when a dup arrives from a
        DIFFERENT outlet — that's the multi-outlet convergence signal, not
        noise, so it bumps the kept row's counter instead of vanishing.
        An item is a dup if its URL hash OR title hash was already recorded."""
        fresh: list[tuple[str, RawNews]] = []
        convergent: list[tuple[str, str]] = []
        batch_seen: dict[str, str] = {}  # hash -> source of the item that claimed it
        for it in items:
            url_h = _h(_norm_url(it.url))
            title_h = _h("title:" + _norm_title(it.title))
            prior = batch_seen.get(url_h) or batch_seen.get(title_h)
            if prior is not None:
                continue  # same batch, same source pull — plain dup
            row = self._sqlite.fetchone(
                "SELECT item_id, source FROM news_dedupe WHERE content_hash IN (?, ?)",
                [url_h, title_h],
            )
            if row:
                if row["item_id"] and row["source"] and row["source"] != it.source:
                    convergent.append((row["item_id"], it.source))
                continue
            batch_seen[url_h] = it.source
            batch_seen[title_h] = it.source
            self._sqlite.executemany(
                "INSERT OR IGNORE INTO news_dedupe (content_hash, item_id, source) "
                "VALUES (?, ?, ?)",
                [(url_h, url_h, it.source), (title_h, url_h, it.source)],
            )
            fresh.append((url_h, it))
        return fresh, convergent

    def _apply_convergence(self, convergent: list[tuple[str, str]]) -> int:
        """Bump outlets/outlet_names on the kept rows for cross-outlet dups."""
        bumped = 0
        for item_id, new_source in convergent:
            row = self._duck.fetchone(
                "SELECT source, outlets, outlet_names FROM news_items WHERE id = ?",
                [item_id],
            )
            if row is None:
                continue
            names = {n for n in (row[2] or row[0] or "").split(",") if n}
            if new_source in names:
                continue
            names.add(new_source)
            self._duck.execute(
                "UPDATE news_items SET outlets = ?, outlet_names = ? WHERE id = ?",
                [len(names), ",".join(sorted(names)), item_id],
            )
            bumped += 1
        return bumped

    async def _tag_entities(self, fresh: list[tuple[str, RawNews]]) -> None:
        """Entity-localized scoring for market-wide items (PLAN §3a, no LLM):
        find watchlist tickers mentioned in the text (case-sensitive, optional
        $-prefix), score each ticker's sentence window with the same encoder
        (cached per-symbol in ts_sentiment for later panels), and tag the item
        with the symbol when exactly one ticker matched."""
        loop = asyncio.get_running_loop()
        tickers = await loop.run_in_executor(None, watchlist_tickers, self._sqlite)
        if not tickers:
            return
        pat = re.compile(
            r"(?<![A-Z$])\$?(" + "|".join(map(re.escape, tickers)) + r")(?![A-Z])"
        )
        for _, it in fresh:
            if it.symbol is not None:
                continue
            text = f"{it.title}. {it.summary[:500]}" if it.summary else it.title
            matched = sorted(set(pat.findall(text)))
            if not matched or len(matched) > 3:  # >3 = ticker-list noise, skip
                continue
            try:
                await self._sentiment.score_entities(text, matched, source=it.source)
            except Exception:
                log.exception("entity scoring failed for %s", it.url)
                continue
            if len(matched) == 1:
                it.symbol = matched[0]

    async def process(self, items: list[RawNews]) -> int:
        loop = asyncio.get_running_loop()
        fresh, convergent = await loop.run_in_executor(None, self._dedupe, items)
        if convergent:
            bumped = await loop.run_in_executor(None, self._apply_convergence, convergent)
            if bumped:
                log.info("convergence: %s stories confirmed by another outlet", bumped)
        if not fresh:
            return 0

        await self._tag_entities(fresh)

        # Score headline+summary in ONE batch — encoder-speed, cache-backed.
        texts = [
            f"{it.title}. {it.summary[:500]}" if it.summary else it.title for _, it in fresh
        ]
        scores = await self._sentiment.score_texts(texts, source="news")

        now = datetime.now(timezone.utc)
        rows = []
        ws_payloads = []
        for (item_id, it), sc in zip(fresh, scores):
            rows.append(
                (
                    item_id, it.source, it.symbol, it.title, it.summary[:1000], it.url,
                    it.published, now, sc.score, sc.confidence, sc.label, sc.model,
                    it.source,
                )
            )
            ws_payloads.append(
                {
                    "type": "news",
                    "item": {
                        "id": item_id,
                        "source": it.source,
                        "symbol": it.symbol,
                        "title": it.title,
                        "summary": it.summary[:1000] or None,
                        "url": it.url,
                        "published": it.published.isoformat(),
                        "score": round(sc.score, 4),
                        "confidence": round(sc.confidence, 4),
                        "label": sc.label,
                        "outlets": 1,
                    },
                }
            )

        await loop.run_in_executor(
            None,
            self._duck.executemany,
            "INSERT OR REPLACE INTO news_items "
            "(id, source, symbol, title, summary, url, published, ingested, "
            " score, confidence, label, model, outlet_names) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        for payload in ws_payloads:
            await self._hub.broadcast(NEWS_TOPIC, payload)
        return len(fresh)

    # Job entrypoints (each ingestor isolated so one source failing never
    # blocks the others — PLAN: Yahoo is fragile, EDGAR/Finnhub are fallbacks).

    async def run_yahoo(self) -> None:
        symbols = watchlist_tickers(self._sqlite)
        n = await self.process(await fetch_yahoo(self._http, symbols))
        log.info("yahoo ingest: %s new items (%s tickers)", n, len(symbols))

    async def run_edgar(self) -> None:
        symbols = watchlist_tickers(self._sqlite)
        items = await fetch_edgar(
            self._http, self._sqlite, symbols, self._edgar_lookback_days
        )
        n = await self.process(items)
        log.info("edgar ingest: %s new items (%s tickers)", n, len(symbols))

    async def run_finnhub(self) -> None:
        symbols = watchlist_tickers(self._sqlite)
        n = await self.process(await fetch_finnhub(self._http, self._finnhub_key, symbols))
        log.info("finnhub ingest: %s new items (%s tickers)", n, len(symbols))

    async def run_gdelt(self) -> None:
        n = await self.process(await fetch_gdelt(self._http))
        log.info("gdelt ingest: %s new items (%s themes)", n, len(GDELT_QUERIES))

    async def run_broad_rss(self) -> None:
        n = await self.process(await fetch_broad_rss(self._http))
        log.info("broad rss ingest: %s new items (%s feeds)", n, len(BROAD_FEEDS))

    async def run_seekingalpha(self) -> None:
        symbols = watchlist_tickers(self._sqlite)
        n = await self.process(await fetch_seekingalpha(self._http, symbols))
        log.info("seekingalpha ingest: %s new items (%s tickers)", n, len(symbols))

    async def run_symbol(self, symbol: str) -> int:
        """Targeted ingest for one (just-added) ticker: pull every per-symbol
        source once so the panel fills without waiting for the next sweep."""
        items = await fetch_yahoo(self._http, [symbol])
        items += await fetch_seekingalpha(self._http, [symbol])
        items += await fetch_finnhub(self._http, self._finnhub_key, [symbol])
        try:
            items += await fetch_edgar(
                self._http, self._sqlite, [symbol], self._edgar_lookback_days
            )
        except Exception as exc:  # the CIK-map fetch itself can fail
            log.warning("edgar targeted ingest failed for %s: %s", symbol, exc)
        n = await self.process(items)
        log.info("targeted ingest for %s: %s new items", symbol, n)
        return n
