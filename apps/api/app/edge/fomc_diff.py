"""FOMC statement diff — a self-hosted "Fed statement tracker".

Desks read each FOMC statement AGAINST the previous one: the Fed deliberately
keeps the template stable, so any changed sentence is a deliberate signal.
This pipeline fetches the two most recent post-meeting statements from
federalreserve.gov (stable URL pattern, conditional-GET cached), scores
similarity with the same 5-word-shingle Jaccard used by the filings differ,
and stores the result in ``filings_diff`` under the reserved symbol
``_FOMC`` — one row per statement, with the changed sentences as evidence.

Decision dates come from the live fomccalendars.htm scrape with the static
fallback table (edge/calendar.py) — both already maintained there. Statements
publish at 14:00 ET on decision day; until then the newest past meeting is
the latest statement.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime

from app.db.duck import DuckStore
from app.edge.calendar import FOMC_DECISIONS, FOMC_URL, parse_fomc_meetings
from app.edge.filings_diff import _new_sentences, _similarity
from app.ingest.http import HttpClient

log = logging.getLogger("market.edge.fomc_diff")

STATEMENT_URL = (
    "https://www.federalreserve.gov/newsevents/pressreleases/monetary{ymd}a.htm"
)
_TAG_RE = re.compile(r"<[^>]+>")


def _statement_text(html: bytes) -> str:
    """The statement body: slice from the article container to the footer
    (the div nests, so a non-greedy </div> match would truncate it), then
    tag-strip. Falls back to the whole page — the identical chrome shingles
    cancel out in the diff."""
    text = html.decode("utf-8", errors="replace")
    start = text.find('id="article"')
    if start != -1:
        end = text.find('id="footer"', start)
        text = text[start: end if end != -1 else len(text)]
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


class FomcDiffPipeline:
    def __init__(self, duck: DuckStore, http: HttpClient) -> None:
        self._duck = duck
        self._http = http

    async def _decision_days(self) -> list[date]:
        days = {date.fromisoformat(d) for d in FOMC_DECISIONS}
        try:
            html = await self._http.get_text(FOMC_URL)
            days |= set(parse_fomc_meetings(html))
        except Exception as exc:
            log.warning("fomc calendar scrape failed (%s) — static fallback", exc)
        return sorted(d for d in days if d <= date.today())

    async def _statement(self, day: date) -> str | None:
        try:
            resp = await self._http.get(
                STATEMENT_URL.format(ymd=day.strftime("%Y%m%d")))
            return _statement_text(resp.body) if resp.status == 200 else None
        except Exception:
            return None  # statement not published (yet) for this date

    async def run(self) -> int:
        past = await self._decision_days()
        if len(past) < 2:
            return 0
        latest, prev = past[-1], past[-2]
        acc = f"FOMC-{latest.isoformat()}"
        if self._duck.fetchone(
            "SELECT 1 FROM filings_diff WHERE accession = ?", [acc]
        ):
            return 0
        new_text = await self._statement(latest)
        old_text = await self._statement(prev)
        if not new_text or not old_text:
            return 0  # latest decision day's statement not out yet
        sim = _similarity(new_text, old_text)
        self._duck.execute(
            "INSERT OR IGNORE INTO filings_diff "
            "(accession, symbol, form, filed_at, prev_accession, prev_filed, "
            " similarity, chars_new, chars_prev, url, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                acc, "_FOMC", "FOMC",
                datetime(latest.year, latest.month, latest.day),
                f"FOMC-{prev.isoformat()}",
                datetime(prev.year, prev.month, prev.day),
                sim, len(new_text), len(old_text),
                STATEMENT_URL.format(ymd=latest.strftime("%Y%m%d")),
                json.dumps({"new_sentences": _new_sentences(new_text, old_text)}),
            ],
        )
        log.info("fomc statement diff: %s vs %s similarity %s",
                 latest, prev, f"{sim:.0%}" if sim is not None else "n/a")
        return 1
