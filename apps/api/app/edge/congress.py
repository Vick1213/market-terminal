"""Senate PTR (congressional stock trade) tracker — Phase 8 §2.

Scrapes the official Senate eFD site (efdsearch.senate.gov, free, no key):
  1. handshake: GET the search home for a CSRF token, POST the prohibition
     agreement (cookies live on the shared HttpClient),
  2. POST the DataTables endpoint for PTRs filed inside the lookback,
  3. fetch each unseen *electronic* PTR and parse its transaction table.

Paper PTRs (scanned images, ``/view/paper/``) and House PTRs (PDF-only on
disclosures-clerk.house.gov) cannot be parsed and are skipped — this is a
Senate-only feed by construction. Third-party aggregators (CapitolTrades BFF,
*-stock-watcher S3 dumps) were probed 2026-06-10 and are dead/blocked, so the
official source is also the only free one left.

Seen-PTR bookkeeping uses the existing scraper_cursor table so a PTR with no
stock rows is still never re-fetched.
"""

from __future__ import annotations

import html as htmllib
import logging
import re
from datetime import datetime, timedelta, timezone

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ingest.http import HttpClient
from app.ingest.news import BROWSER_HEADERS
from app.profile import gated

log = logging.getLogger("market.edge.congress")

EFD_HOME = "https://efdsearch.senate.gov/search/home/"
EFD_SEARCH = "https://efdsearch.senate.gov/search/"
EFD_DATA = "https://efdsearch.senate.gov/search/report/data/"
EFD_VIEW = "https://efdsearch.senate.gov/search/view/ptr/{ptr_id}/"

CURSOR_SOURCE = "senate_efd"
PTR_REPORT_TYPE = "[11]"  # eFD code for Periodic Transaction Reports
PAGE_LEN = 100            # PTR volume is ~20/month — one page is plenty

_CSRF_RE = re.compile(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"')
_PTR_LINK_RE = re.compile(r'/search/view/ptr/([0-9a-f-]{36})/')
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_AMOUNT_RE = re.compile(r"\$([\d,]+)(?:\s*-\s*\$([\d,]+))?")

_SIDE = {"purchase": "buy", "sale": "sell", "exchange": "exchange"}


def _strip(cell: str) -> str:
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", cell)).split())


def _amount_band(text: str) -> tuple[float | None, float | None]:
    """'$1,001 - $15,000' -> (1001, 15000); 'Over $50,000,000' -> (5e7, None)."""
    m = _AMOUNT_RE.search(text)
    if not m:
        return None, None
    lo = float(m.group(1).replace(",", ""))
    hi = float(m.group(2).replace(",", "")) if m.group(2) else None
    return lo, hi


def parse_ptr_rows(page: str) -> list[dict]:
    """Electronic PTR HTML -> transaction dicts. Columns (verified 2026-06-10):
    # | tx date | owner | ticker | asset name | asset type | type | amount | comment."""
    out: list[dict] = []
    for tr in _TR_RE.findall(page):
        cells = [_strip(c) for c in _TD_RE.findall(tr)]
        if len(cells) < 8 or not cells[0].isdigit():
            continue  # header / layout rows
        tx_date = None
        try:
            tx_date = datetime.strptime(cells[1], "%m/%d/%Y")
        except ValueError:
            pass
        ticker = cells[3] if cells[3] not in ("--", "") else None
        tx_type = cells[6]
        side = _SIDE.get(tx_type.split()[0].lower(), tx_type.lower() or None)
        lo, hi = _amount_band(cells[7])
        out.append({
            "row_no": int(cells[0]),
            "tx_date": tx_date,
            "ticker": ticker,
            "asset": cells[4],
            "asset_type": cells[5],
            "tx_type": tx_type,
            "side": side,
            "amount_min": lo,
            "amount_max": hi,
        })
    return out


class CongressPipeline:
    def __init__(
        self,
        duck: DuckStore,
        sqlite: SqliteStore,
        http: HttpClient,
        *,
        lookback_days: int = 90,
    ) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._http = http
        self._lookback = lookback_days

    async def _handshake(self) -> str | None:
        """Accept the eFD prohibition agreement; returns the session csrftoken."""
        page = await self._http.get_text(EFD_HOME, headers=BROWSER_HEADERS, conditional=False)
        m = _CSRF_RE.search(page)
        if not m:
            log.warning("efd handshake: no csrf token on home page")
            return None
        await self._http.post(
            EFD_HOME,
            data={"csrfmiddlewaretoken": m.group(1), "prohibition_agreement": "1"},
            headers={**BROWSER_HEADERS, "Referer": EFD_HOME},
        )
        token = self._http.cookie("csrftoken")
        if not token:
            log.warning("efd handshake: agreement accepted but no csrftoken cookie")
        return token

    def _seen(self, ptr_id: str) -> bool:
        return (
            self._sqlite.fetchone(
                "SELECT 1 FROM scraper_cursor WHERE source = ? AND key = ?",
                [CURSOR_SOURCE, ptr_id],
            )
            is not None
        )

    def _mark_seen(self, ptr_id: str, filed: str) -> None:
        self._sqlite.execute(
            "INSERT OR REPLACE INTO scraper_cursor (source, key, cursor) VALUES (?, ?, ?)",
            [CURSOR_SOURCE, ptr_id, filed],
        )

    @gated("congress_efd", default=lambda: 0)
    async def run(self) -> int:
        """Ethics in Government Act bars commercial use of Financial
        Disclosure Reports (statute, not just ToS) — gated off entirely in
        MARKET_PROFILE=commercial. See docs/data-licensing-audit.md."""
        token = await self._handshake()
        if not token:
            return 0
        start = datetime.now(timezone.utc) - timedelta(days=self._lookback)
        try:
            resp = await self._http.post(
                EFD_DATA,
                data={
                    "start": "0",
                    "length": str(PAGE_LEN),
                    "report_types": PTR_REPORT_TYPE,
                    "filer_types": "[]",
                    "submitted_start_date": start.strftime("%m/%d/%Y 00:00:00"),
                    "submitted_end_date": "",
                    "candidate_state": "",
                    "senator_state": "",
                    "office_id": "",
                    "first_name": "",
                    "last_name": "",
                },
                headers={**BROWSER_HEADERS, "Referer": EFD_SEARCH, "X-CSRFToken": token},
            )
            listing = resp.json()
        except Exception as exc:
            log.warning("efd ptr listing failed: %s", exc)
            return 0

        stored = 0
        for row in listing.get("data", []):
            try:
                first, last, _office, link_html, filed = row[:5]
            except (ValueError, TypeError):
                continue
            m = _PTR_LINK_RE.search(link_html or "")
            if not m:
                continue  # paper filing — scanned image, unparseable
            ptr_id = m.group(1)
            if self._seen(ptr_id):
                continue
            senator = f"{first.strip().title()} {last.strip().title()}"
            try:
                filed_at = datetime.strptime(filed.strip(), "%m/%d/%Y")
            except ValueError:
                filed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            url = EFD_VIEW.format(ptr_id=ptr_id)
            try:
                page = await self._http.get_text(
                    url, headers=BROWSER_HEADERS, conditional=False
                )
                txns = parse_ptr_rows(page)
            except Exception as exc:
                log.warning("efd ptr %s fetch failed: %s", ptr_id, exc)
                continue  # NOT marked seen — retried next sweep
            for t in txns:
                self._duck.execute(
                    "INSERT OR REPLACE INTO congress_trades "
                    "(ptr_id, row_no, senator, filed_at, tx_date, ticker, asset, "
                    " asset_type, side, tx_type, amount_min, amount_max, url) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        ptr_id, t["row_no"], senator, filed_at, t["tx_date"],
                        t["ticker"], t["asset"], t["asset_type"], t["side"],
                        t["tx_type"], t["amount_min"], t["amount_max"], url,
                    ],
                )
                stored += 1
            self._mark_seen(ptr_id, filed_at.date().isoformat())
        if stored:
            log.info("congress ingest: %s new senate PTR transaction(s)", stored)
        return stored
