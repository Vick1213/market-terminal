"""Phase 16 §11 rank 11 (leg 3) — Fed speeches hawk/dove stream.

The Federal Reserve publishes every speech and testimony in a keyless RSS feed.
We pull recent items, fetch each new speech's full text, and score it with the
EXISTING FinBERT sentiment service (app/sentiment) — no new model. The speech
detail lands in a small ``fed_speeches`` table (so the Policy-Risk panel can
list individual speeches with their tone) and a rolling hawk/dove index scalar
is upserted into ts_macro so it charts and the corr engine can pick it up.

Hawk/dove via FinBERT — the proxy, stated honestly:
  FinBERT scores financial-news sentiment (p_positive - p_negative, -1..+1). It
  is NOT a purpose-built hawk/dove classifier. We use the well-worn proxy that a
  restrictive / inflation-fighting (hawkish) tone reads as *negative* financial
  sentiment and an accommodative (dovish) tone reads as *positive*, so
  ``hawk_dove = -sentiment`` (>0 = hawkish, <0 = dovish). It is a directional
  tell, not a calibrated basis-point forecast — treat it as such.

Each speech is fetched + scored exactly once (the table's URL primary key is the
cache); subsequent runs only touch genuinely new speeches, so the polite
1-req/2s federalreserve.gov limit is a non-issue at the 12h cadence. The whole
speech body is chunked under FinBERT's 512-token window and the per-speech score
is the confidence-weighted mean across chunks, so the tone reflects the entire
speech, not just its opening pleasantries. Same fail-soft contract throughout.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape

from app.db.duck import DuckStore
from app.ingest.http import HttpClient
from app.ingest.macro import _d
from app.sentiment import SentimentService

log = logging.getLogger("market.ingest.fed_speeches")

RSS_URL = "https://www.federalreserve.gov/feeds/speeches_and_testimony.xml"

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_ITEM_RE = re.compile(r"<item\b.*?</item>", re.S | re.I)
# Article body lives in <div id="article">; the footer / "last update" block
# follows it — cut there so nav + footer boilerplate never pollute the score.
_ARTICLE_START = 'id="article"'
_ARTICLE_END = re.compile(r'id="lastUpdate"|<footer', re.I)

_CHUNK_CHARS = 1800   # ~ under FinBERT's 512-token window
_MAX_CHUNKS = 12      # cap the body scored per speech (the meaty part)


def _tag(item: str, name: str) -> str:
    """Inner text of <name>…</name> within one RSS <item> (CDATA-tolerant)."""
    m = re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", item, re.S | re.I)
    if not m:
        return ""
    val = m.group(1).strip()
    val = re.sub(r"^<!\[CDATA\[|\]\]>$", "", val).strip()
    return unescape(val)


def parse_feed(xml: str) -> list[dict]:
    """RSS -> [{url, title, speaker, ts, summary}] newest-first."""
    out: list[dict] = []
    for raw in _ITEM_RE.findall(xml):
        url = _tag(raw, "link")
        title = _tag(raw, "title")
        if not url or not title:
            continue
        # title is "Speaker, Topic" — speaker is the text before the first comma.
        speaker = title.split(",", 1)[0].strip()
        pub = _tag(raw, "pubDate")
        ts = None
        if pub:
            try:
                ts = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                ts = None
        if ts is None:
            ts = datetime.now(timezone.utc)
        out.append({
            "url": url,
            "title": title,
            "speaker": speaker,
            "ts": ts,
            "summary": _tag(raw, "description"),
        })
    return out


def extract_text(html: str) -> str:
    """Fed speech HTML -> plain article text (heading + body), '' if not found."""
    i = html.find(_ARTICLE_START)
    seg = html[i:] if i >= 0 else html
    end = _ARTICLE_END.search(seg)
    if end:
        seg = seg[: end.start()]
    seg = _SCRIPT_RE.sub(" ", seg)
    text = _TAG_RE.sub(" ", seg)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _chunks(text: str) -> list[str]:
    return [text[i : i + _CHUNK_CHARS] for i in range(0, len(text), _CHUNK_CHARS)][
        :_MAX_CHUNKS
    ]


class FedSpeechPipeline:
    """fetch the Fed speech RSS -> score new speeches -> fed_speeches + ts_macro."""

    def __init__(
        self,
        duck: DuckStore,
        http: HttpClient,
        sentiment: SentimentService,
        *,
        max_items: int = 20,
        index_window: int = 12,
    ) -> None:
        self._duck = duck
        self._http = http
        self._sentiment = sentiment
        self._max_items = max_items
        self._index_window = index_window

    async def _known_urls(self, urls: list[str]) -> set[str]:
        if not urls:
            return set()
        loop = asyncio.get_running_loop()
        ph = ",".join("?" for _ in urls)
        rows = await loop.run_in_executor(
            None,
            self._duck.fetchall,
            f"SELECT url FROM fed_speeches WHERE url IN ({ph})",
            urls,
        )
        return {r[0] for r in rows}

    async def _score_speech(self, url: str) -> tuple[float, float, int] | None:
        """Fetch + score one speech -> (sentiment, confidence, n_chunks)."""
        try:
            html = await self._http.get_text(url)
        except Exception as exc:
            log.warning("fed speech fetch %s failed: %s", url, exc)
            return None
        text = extract_text(html)
        chunks = _chunks(text)
        if not chunks:
            return None
        scores = await self._sentiment.score_texts(chunks, source="fed_speech")
        if not scores:
            return None
        wsum = sum(s.confidence for s in scores) or float(len(scores))
        sentiment = sum(s.score * s.confidence for s in scores) / wsum
        conf = sum(s.confidence for s in scores) / len(scores)
        return sentiment, conf, len(chunks)

    async def _persist(self, speech: dict, sentiment: float, conf: float, n: int) -> None:
        loop = asyncio.get_running_loop()
        now = datetime.now(timezone.utc)
        await loop.run_in_executor(
            None,
            self._duck.execute,
            "INSERT OR REPLACE INTO fed_speeches "
            "(url, speech_date, speaker, title, score, hawk_dove, confidence, "
            "chunks, scored_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                speech["url"], speech["ts"], speech["speaker"], speech["title"],
                round(sentiment, 4), round(-sentiment, 4), round(conf, 4), n, now,
            ],
        )

    async def _refresh_index(self) -> None:
        """Rolling hawk/dove index = mean hawk_dove over the last N speeches,
        stamped at the most recent speech date -> ts_macro FED_HAWK_DOVE."""
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(
            None,
            self._duck.fetchall,
            "SELECT speech_date, hawk_dove FROM fed_speeches "
            "WHERE hawk_dove IS NOT NULL ORDER BY speech_date DESC LIMIT ?",
            [self._index_window],
        )
        if not rows:
            return
        latest = rows[0][0]
        stamp = _d(latest.date() if hasattr(latest, "date") else latest)
        idx = sum(r[1] for r in rows) / len(rows)
        await loop.run_in_executor(
            None,
            self._duck.execute,
            "INSERT OR REPLACE INTO ts_macro (series_id, ts, value, source) "
            "VALUES (?, ?, ?, ?)",
            ["FED_HAWK_DOVE", stamp, round(idx, 4), "fed_speech"],
        )

    async def run(self) -> None:
        try:
            xml = await self._http.get_text(RSS_URL)
        except Exception as exc:
            log.warning("fed speeches RSS fetch failed: %s", exc)
            return
        feed = parse_feed(xml)[: self._max_items]
        if not feed:
            log.info("fed speeches: feed empty")
            return
        known = await self._known_urls([s["url"] for s in feed])
        fresh = [s for s in feed if s["url"] not in known]

        scored = 0
        for speech in fresh:
            res = await self._score_speech(speech["url"])
            if res is None:
                continue
            sentiment, conf, n = res
            await self._persist(speech, sentiment, conf, n)
            scored += 1

        await self._refresh_index()
        log.info("fed speeches: %s new scored (%s in feed)", scored, len(feed))
