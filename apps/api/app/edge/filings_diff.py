"""10-K/10-Q risk-factor diff detector ("Lazy Prices").

Cohen/Malloy/Nguyen (2020): markets underreact to CHANGES in boring periodic
filings — issuers that rewrite their risk-factor language underperform the
ones that quietly re-file the same text. This pipeline:

  * for every tracked ticker (watchlist equities + news-only tickers), finds
    the latest 10-K/10-Q in the EDGAR submissions JSON and the previous filing
    of the SAME form (quarter-over-quarter for 10-Qs; YoY would be closer to
    the paper but needs more history — stated trade-off),
  * extracts Item 1A "Risk Factors" from both primary documents (tag-stripped
    text, longest section wins — TOC mentions are shorter than the section),
  * scores similarity as Jaccard over 5-word shingles, plus a sample of new
    sentences for the evidence trail,
  * stores one row per newer filing in ``filings_diff``; rows with low
    similarity feed an alert rule and negative evidence in the strategist's
    single-name picks.

10-Qs that say "no material changes" produce too few shingles to score —
similarity is stored as NULL and never alerts, which is the honest reading.
EDGAR etiquette inherited from the shared HttpClient.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from html import unescape

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ingest.http import HttpClient
from app.ingest.news import _cik_map, watchlist_tickers

log = logging.getLogger("market.edge.filings_diff")

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_DOC = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

_FORMS = ("10-K", "10-Q")
_TAG_RE = re.compile(r"<[^>]+>")
# Below this the section is a "no material changes" stub, an incorporation-
# by-reference pointer, or a failed extraction — comparing a stub against a
# full section produces a fake near-zero similarity, so both sides must clear
# the floor (~1.5k chars of real risk-factor prose) before scoring.
_MIN_SHINGLES = 200


def _item_1a(html: bytes) -> str:
    """Item 1A Risk Factors as plain text; '' when not found."""
    text = _TAG_RE.sub(" ", html.decode("utf-8", errors="replace"))
    text = re.sub(r"\s+", " ", unescape(text))
    low = text.lower()
    best = ""
    for m in re.finditer(r"item\s*1a\.?\s*[—:\-]?\s*risk factors", low):
        tail = low[m.end():]
        stop = re.search(r"item\s*(1b|2)\s*\.", tail)
        seg = text[m.end(): m.end() + (stop.start() if stop else len(tail))]
        if len(seg) > len(best):
            best = seg
    return best.strip()


def _shingles(text: str, n: int = 5) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def _similarity(new: str, old: str) -> float | None:
    sa, sb = _shingles(new), _shingles(old)
    if len(sa) < _MIN_SHINGLES or len(sb) < _MIN_SHINGLES:
        return None
    return len(sa & sb) / len(sa | sb)


def _new_sentences(new: str, old: str, limit: int = 3) -> list[str]:
    """Sample sentences present in the new section but not the old —
    the 'what changed' evidence line."""
    old_sh = _shingles(old)
    out: list[str] = []
    for sent in re.split(r"(?<=[.!?])\s+", new):
        if len(sent) < 60 or len(sent) > 400:
            continue
        sh = _shingles(sent)
        if sh and len(sh & old_sh) / len(sh) < 0.2:
            out.append(sent.strip())
            if len(out) >= limit:
                break
    return out


class FilingsDiffPipeline:
    def __init__(self, duck: DuckStore, sqlite: SqliteStore, http: HttpClient) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._http = http

    def _have(self, accession: str) -> bool:
        return self._duck.fetchone(
            "SELECT 1 FROM filings_diff WHERE accession = ?", [accession]
        ) is not None

    async def _doc_text(self, cik: int, accession: str, doc: str) -> bytes | None:
        try:
            resp = await self._http.get(ARCHIVE_DOC.format(
                cik=cik, acc=accession.replace("-", ""), doc=doc.split("/")[-1]))
            return resp.body
        except Exception as exc:
            log.warning("filing doc fetch failed (%s %s): %s", accession, doc, exc)
            return None

    async def _diff_symbol(self, sym: str, cik: int) -> int:
        try:
            sub = await self._http.get_json(SUBMISSIONS.format(cik=cik))
        except Exception as exc:
            log.warning("submissions failed for %s: %s", sym, exc)
            return 0
        recent = sub.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        # (form, accession, primary doc, filed) newest-first, 10-K/10-Q only.
        filings = [
            (forms[i], recent["accessionNumber"][i],
             recent.get("primaryDocument", [""] * len(forms))[i],
             recent["filingDate"][i])
            for i in range(len(forms)) if forms[i] in _FORMS
        ]
        stored = 0
        for idx, (form, acc, doc, filed) in enumerate(filings):
            if self._have(acc) or not doc:
                continue
            prev = next(
                (f for f in filings[idx + 1:] if f[0] == form and f[2]), None)
            if prev is None:
                break  # no baseline stored on EDGAR — nothing to diff
            new_html = await self._doc_text(cik, acc, doc)
            old_html = await self._doc_text(cik, prev[1], prev[2])
            if new_html is None or old_html is None:
                break
            new_1a, old_1a = _item_1a(new_html), _item_1a(old_html)
            sim = _similarity(new_1a, old_1a)
            self._duck.execute(
                "INSERT OR IGNORE INTO filings_diff "
                "(accession, symbol, form, filed_at, prev_accession, prev_filed, "
                " similarity, chars_new, chars_prev, url, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    acc, sym, form, datetime.fromisoformat(filed),
                    prev[1], datetime.fromisoformat(prev[3]),
                    sim, len(new_1a), len(old_1a),
                    ARCHIVE_DOC.format(cik=cik, acc=acc.replace("-", ""),
                                       doc=doc.split("/")[-1]),
                    json.dumps({"new_sentences": _new_sentences(new_1a, old_1a)})
                    if sim is not None and sim < 0.9 else None,
                ],
            )
            stored += 1
            break  # only the newest unseen filing per symbol per sweep
        return stored

    async def run(self) -> int:
        cik_by_ticker = await _cik_map(self._http, self._sqlite)
        stored = 0
        for sym in watchlist_tickers(self._sqlite):
            cik = cik_by_ticker.get(sym.upper())
            if cik is None:
                continue  # ETFs / non-EDGAR tickers
            stored += await self._diff_symbol(sym, cik)
        if stored:
            log.info("filings diff: %s new 10-K/10-Q comparison(s)", stored)
        return stored


def latest_diffs(duck: DuckStore, limit: int = 50) -> list[dict]:
    """Read-time feed for the API. Blocking — run_in_executor."""
    rows = duck.fetchall(
        "SELECT accession, symbol, form, filed_at, prev_filed, similarity, "
        "chars_new, chars_prev, url, detail FROM filings_diff "
        "ORDER BY filed_at DESC LIMIT ?",
        [limit],
    )
    out = []
    for r in rows:
        out.append({
            "accession": r[0], "symbol": r[1], "form": r[2],
            "filed_at": str(r[3])[:10], "prev_filed": str(r[4])[:10] if r[4] else None,
            "similarity": round(float(r[5]), 3) if r[5] is not None else None,
            "chars_new": r[6], "chars_prev": r[7], "url": r[8],
            "new_sentences": (json.loads(r[9]) or {}).get("new_sentences", [])
            if r[9] else [],
        })
    return out
