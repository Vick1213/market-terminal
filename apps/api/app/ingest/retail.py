"""Phase 5 ingestors — Retail Market Score (Panel b).

Mention-volume SPIKE is the lead signal (PLAN §3b); the scored text only
modulates it. Sources, in reliability order:
  * ApeWisdom (keyless, ~2x/hr server-side): per-filter Reddit leaderboards
    with mentions/upvotes/rank now and 24h ago -> ts_retail snapshots. It does
    the Reddit scraping + cashtag extraction; it has NO sentiment.
  * StockTwits public JSON (fragile, Cloudflare): polled ONLY for spiking
    tickers with a browser UA. Human Bullish/Bearish tags are ground truth;
    FinBERT scores every body.
  * Bluesky AppView search (keyless; the public.* host 403s, api.bsky.app
    works): cashtag search per spiking ticker -> FinBERT.
  * Tradestie WSB API (single operator, Cloudflare-gated from scripts):
    pre-scored WSB comment sentiment, kept as a cross-confirmation source
    that may simply never deliver — skip-on-fail, log at debug.

PRAW is deliberately absent (PLAN: Reddit self-service API closed Nov 2025;
the Reddit signal arrives via ApeWisdom + the text sources above).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ingest.http import HttpClient
from app.retail.score import compute_gauge, compute_leaderboard
from app.sentiment.service import SentimentService
from app.ws.hub import ConnectionManager

log = logging.getLogger("market.ingest.retail")

APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/{flt}/page/{page}"
STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"
BLUESKY_SEARCH_URL = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
TRADESTIE_URL = "https://tradestie.com/api/v1/apps/reddit"

# StockTwits (and Tradestie) 403 the descriptive default UA — browser headers
# per PLAN §6 ("realistic browser UA; skip-on-fail, degrade gracefully").
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Min human Bullish/Bearish tags before the tag ratio outweighs FinBERT.
MIN_TAGS = 3
TAG_WEIGHT = 0.6  # tags are ground truth, FinBERT fills the untagged majority


def _now_minute() -> datetime:
    return datetime.now(timezone.utc).replace(second=0, microsecond=0, tzinfo=None)


class RetailPipeline:
    def __init__(
        self,
        duck: DuckStore,
        sqlite: SqliteStore,
        sentiment: SentimentService,
        hub: ConnectionManager,
        http: HttpClient,
        *,
        filters: list[str],
        pages: int = 2,
        spike_top_n: int = 8,
        message_retention_days: int = 14,
    ) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._sentiment = sentiment
        self._hub = hub
        self._http = http
        self._filters = filters
        self._pages = pages
        self._spike_top_n = spike_top_n
        self._retention_days = message_retention_days

    # ----------------------------------------------------------- cursors

    def _cursor_get(self, source: str, key: str) -> str | None:
        rows = self._sqlite.fetchall(
            "SELECT cursor FROM scraper_cursor WHERE source = ? AND key = ?", (source, key)
        )
        return rows[0]["cursor"] if rows else None

    def _cursor_set(self, source: str, key: str, cursor: str) -> None:
        self._sqlite.execute(
            "INSERT OR REPLACE INTO scraper_cursor (source, key, cursor, updated_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (source, key, cursor),
        )

    # --------------------------------------------------------- ApeWisdom

    async def run_apewisdom(self) -> None:
        ts = _now_minute()
        rows: list[tuple] = []
        for flt in self._filters:
            for page in range(1, self._pages + 1):
                try:
                    data = await self._http.get_json(
                        APEWISDOM_URL.format(flt=flt, page=page), conditional=False
                    )
                except Exception as exc:
                    log.warning("apewisdom %s p%s failed: %s", flt, page, exc)
                    break  # later pages of the same filter will fail too
                for r in data.get("results", []):
                    sym = (r.get("ticker") or "").strip().upper()
                    if not sym:
                        continue
                    rows.append(
                        (
                            f"apewisdom:{flt}", sym, ts,
                            int(r.get("mentions") or 0),
                            int(r.get("mentions_24h_ago") or 0),
                            int(r["rank"]) if r.get("rank") is not None else None,
                            int(r["rank_24h_ago"]) if r.get("rank_24h_ago") is not None else None,
                            int(r.get("upvotes") or 0),
                        )
                    )
        if not rows:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._duck.executemany,
            "INSERT OR REPLACE INTO ts_retail "
            "(source, symbol, ts, mentions, mentions_prev, rank, rank_prev, upvotes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        log.info("apewisdom: %s rows across %s filter(s)", len(rows), len(self._filters))
        await self._store_gauge_and_push()

    # ------------------------------------------- social text (spiking only)

    def _spiking_symbols(self) -> list[str]:
        board = compute_leaderboard(self._duck, limit=self._spike_top_n)
        return [r["symbol"] for r in board]

    async def run_social(self) -> None:
        """StockTwits + Bluesky text for the current spike leaders only —
        the PLAN cadence rule that keeps both fragile sources unbanned."""
        loop = asyncio.get_running_loop()
        symbols = await loop.run_in_executor(None, self._spiking_symbols)
        if not symbols:
            log.info("social: no apewisdom snapshot yet — nothing to poll")
            return
        for sym in symbols:
            await self._stocktwits_symbol(sym)
            await self._bluesky_symbol(sym)
        await loop.run_in_executor(None, self._prune_messages)
        await self._store_gauge_and_push()

    async def _score_and_store(
        self,
        source: str,
        sym: str,
        items: list[dict],  # {id, ts, text, url, tag}
        tag_counts: tuple[int, int] | None = None,  # (bull, bear)
    ) -> None:
        """FinBERT-score new messages, persist them, and write the per-symbol
        snapshot row (mentions = new messages this poll; sentiment = blended)."""
        if not items:
            return
        loop = asyncio.get_running_loop()
        scores = await self._sentiment.score_texts(
            [i["text"] for i in items], symbol=sym, source=source
        )
        await loop.run_in_executor(
            None,
            self._duck.executemany,
            "INSERT OR REPLACE INTO retail_messages "
            "(id, source, symbol, ts, text, url, score, label, tag) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (i["id"], source, sym, i["ts"], i["text"][:1000], i["url"],
                 s.score, s.label, i.get("tag"))
                for i, s in zip(items, scores)
            ],
        )

        finbert_mean = sum(s.score for s in scores) / len(scores)
        sent = finbert_mean
        if tag_counts:
            bull, bear = tag_counts
            if bull + bear >= MIN_TAGS:
                tag_ratio = (bull - bear) / (bull + bear)
                sent = TAG_WEIGHT * tag_ratio + (1 - TAG_WEIGHT) * finbert_mean
        await loop.run_in_executor(
            None,
            self._duck.execute,
            "INSERT OR REPLACE INTO ts_retail (source, symbol, ts, mentions, sentiment_score) "
            "VALUES (?, ?, ?, ?, ?)",
            [source, sym, _now_minute(), len(items), round(sent, 4)],
        )
        log.info("%s %s: %s new msgs, sentiment %.2f", source, sym, len(items), sent)

    async def _stocktwits_symbol(self, sym: str) -> None:
        loop = asyncio.get_running_loop()
        last_id = await loop.run_in_executor(None, self._cursor_get, "stocktwits", sym)
        url = STOCKTWITS_URL.format(sym=sym)
        params = {"since": last_id} if last_id else None
        try:
            data = await self._http.get_json(
                url, headers=BROWSER_HEADERS, params=params, conditional=False
            )
        except Exception as exc:
            log.debug("stocktwits %s skipped: %s", sym, exc)  # fragile by design
            return
        msgs = data.get("messages") or []
        items, bull, bear = [], 0, 0
        for msg in msgs:
            body = (msg.get("body") or "").strip()
            if not body:
                continue
            tag = ((msg.get("entities") or {}).get("sentiment") or {}).get("basic")
            bull += tag == "Bullish"
            bear += tag == "Bearish"
            items.append(
                {
                    "id": f"st:{msg['id']}",
                    "ts": datetime.fromisoformat(
                        msg["created_at"].replace("Z", "+00:00")
                    ).replace(tzinfo=None),
                    "text": body,
                    "url": f"https://stocktwits.com/symbol/{sym}",
                    "tag": tag,
                }
            )
        await self._score_and_store("stocktwits", sym, items, (bull, bear))
        if msgs:
            max_id = max(int(m["id"]) for m in msgs)
            await loop.run_in_executor(
                None, self._cursor_set, "stocktwits", sym, str(max_id)
            )

    async def _bluesky_symbol(self, sym: str) -> None:
        loop = asyncio.get_running_loop()
        since = await loop.run_in_executor(None, self._cursor_get, "bluesky", sym)
        try:
            data = await self._http.get_json(
                BLUESKY_SEARCH_URL,
                params={"q": f"${sym}", "limit": 50, "sort": "latest"},
                conditional=False,
            )
        except Exception as exc:
            log.debug("bluesky %s skipped: %s", sym, exc)
            return
        # The search matches loosely; require the literal cashtag so a post
        # about "$GLD" counts but unrelated text mentioning the letters doesn't.
        cashtag = re.compile(rf"\${re.escape(sym)}\b", re.I)
        items = []
        newest = since
        for post in data.get("posts") or []:
            record = post.get("record") or {}
            text = (record.get("text") or "").strip()
            created = record.get("createdAt") or ""
            if not text or not created or not cashtag.search(text):
                continue
            if since and created <= since:  # ISO-8601 sorts lexicographically
                continue
            if newest is None or created > newest:
                newest = created
            uri = post.get("uri") or ""
            items.append(
                {
                    "id": "bs:" + hashlib.sha256(uri.encode()).hexdigest()[:24],
                    "ts": datetime.fromisoformat(
                        created.replace("Z", "+00:00")
                    ).astimezone(timezone.utc).replace(tzinfo=None),
                    "text": text,
                    "url": uri,
                    "tag": None,
                }
            )
        await self._score_and_store("bluesky", sym, items)
        if newest and newest != since:
            await loop.run_in_executor(None, self._cursor_set, "bluesky", sym, newest)

    # ----------------------------------------------------------- Tradestie

    async def run_tradestie(self) -> None:
        try:
            data = await self._http.get_json(
                TRADESTIE_URL, headers=BROWSER_HEADERS, conditional=False
            )
        except Exception as exc:
            log.debug("tradestie skipped (Cloudflare-gated): %s", exc)
            return
        if not isinstance(data, list):
            log.debug("tradestie skipped: unexpected payload shape")
            return
        ts = _now_minute()
        rows = []
        for r in data:
            sym = (r.get("ticker") or "").strip().upper()
            if not sym:
                continue
            rows.append(
                ("tradestie", sym, ts, int(r.get("no_of_comments") or 0),
                 float(r.get("sentiment_score") or 0.0))
            )
        if not rows:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._duck.executemany,
            "INSERT OR REPLACE INTO ts_retail (source, symbol, ts, mentions, sentiment_score) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        log.info("tradestie: %s WSB sentiment rows", len(rows))

    # ------------------------------------------------------------- shared

    def _prune_messages(self) -> None:
        self._duck.execute(
            "DELETE FROM retail_messages WHERE ts < ?",
            [datetime.now(timezone.utc).replace(tzinfo=None)
             - timedelta(days=self._retention_days)],
        )

    async def _store_gauge_and_push(self) -> None:
        """Persist the market-wide gauge as chartable ts_macro series and let
        the panel know fresh numbers exist."""
        loop = asyncio.get_running_loop()
        gauge = await loop.run_in_executor(None, compute_gauge, self._duck)
        ts = _now_minute()
        rows = [
            (sid, ts, gauge[key], "retail")
            for sid, key in (
                ("RETAIL_SENT", "sentiment"),
                ("RETAIL_CHATTER_Z", "chatter_z"),
                ("RETAIL_MENTIONS", "total_mentions"),
            )
            if gauge.get(key) is not None
        ]
        if rows:
            await loop.run_in_executor(
                None,
                self._duck.executemany,
                "INSERT OR REPLACE INTO ts_macro (series_id, ts, value, source) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
        await self._hub.broadcast(
            "retail",
            {"type": "retail", "computed_at": gauge["computed_at"],
             "score": gauge["score"], "total_mentions": gauge["total_mentions"]},
        )
