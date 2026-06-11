"""Market-wide Form 4 cluster-buy scanner.

The watchlist tracker (edge/insider.py) only sees issuers you already follow —
but insider clusters are most valuable for DISCOVERING names. This scanner
sweeps the EDGAR daily form index (one request per day-file, conditional-GET
cached), walks the Form 4 entries with a persistent per-day cursor, fetches
each filing's submission text (the ownership XML is embedded in it, including
``issuerTradingSymbol`` — no extra request per filing), and stores open-market
buys above a value floor into the same ``insider_trades`` table the cluster
detection, alert engine and strategist picks layer already read.

Cost control, because ~2-4k Form 4s are filed per business day:
  * a hard per-sweep fetch cap (config); the cursor resumes where the last
    sweep stopped, so repeated sweeps work through the backlog,
  * skipped/remaining counts are logged — a capped sweep never silently
    pretends it covered the day,
  * a higher value floor than the watchlist tracker (default $50k vs $25k)
    keeps option-exercise dust and token buys out.

EDGAR etiquette is inherited from the shared HttpClient (descriptive UA,
per-host request cap).
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.edge.insider import _parse_form4
from app.ingest.http import HttpClient

log = logging.getLogger("market.edge.insider_scan")

DAILY_INDEX = (
    "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{q}/form.{ymd}.idx"
)
ARCHIVE = "https://www.sec.gov/Archives/{path}"

# form.idx line: form type, company name (spaces), CIK, YYYYMMDD, file path.
_IDX_LINE = re.compile(
    r"^(?P<form>\S+)\s+(?P<company>.+?)\s+(?P<cik>\d+)\s+"
    r"(?P<date>\d{8})\s+(?P<path>edgar/\S+\.txt)\s*$"
)
# Symbols that look like real exchange tickers (issuerTradingSymbol is free
# text: "NONE", "N/A", ADR suffixes etc. are all seen in the wild).
_TICKER_OK = re.compile(r"^[A-Z]{1,6}([.\-][A-Z]{1,2})?$")


def _extract_ownership_xml(submission: bytes) -> bytes | None:
    """The daily-index .txt is a full SGML submission; the ownership XML sits
    inside a <XML>...</XML> block."""
    text = submission.decode("utf-8", errors="replace")
    start = text.find("<ownershipDocument")
    if start == -1:
        return None
    end = text.find("</ownershipDocument>", start)
    if end == -1:
        return None
    return text[start : end + len("</ownershipDocument>")].encode("utf-8")


def _issuer_info(xml_bytes: bytes) -> tuple[str | None, str | None]:
    """(symbol, issuer name) from the ownership XML's <issuer> block."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None, None
    issuer = root.find("issuer")
    if issuer is None:
        return None, None
    sym = (issuer.findtext("issuerTradingSymbol") or "").strip().upper()
    name = (issuer.findtext("issuerName") or "").strip()
    return (sym if _TICKER_OK.match(sym) else None), (name or None)


class InsiderScanPipeline:
    def __init__(
        self,
        duck: DuckStore,
        sqlite: SqliteStore,
        http: HttpClient,
        *,
        max_filings_per_sweep: int = 300,
        min_trade_value: float = 50_000,
        lookback_days: int = 2,
    ) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._http = http
        self._cap = max_filings_per_sweep
        self._min_value = min_trade_value
        self._lookback = lookback_days

    # ---------------------------------------------------------------- cursor

    def _cursor(self, ymd: str) -> int:
        row = self._sqlite.fetchone(
            "SELECT cursor FROM scraper_cursor WHERE source = 'insider_scan' AND key = ?",
            [ymd],
        )
        return int(row["cursor"]) if row and row["cursor"] else 0

    def _save_cursor(self, ymd: str, line_no: int) -> None:
        self._sqlite.execute(
            "INSERT OR REPLACE INTO scraper_cursor (source, key, cursor, updated_at) "
            "VALUES ('insider_scan', ?, ?, datetime('now'))",
            [ymd, str(line_no)],
        )

    # ----------------------------------------------------------------- sweep

    def _seen(self, accessions: list[str]) -> set[str]:
        if not accessions:
            return set()
        ph = ", ".join("?" for _ in accessions)
        rows = self._duck.fetchall(
            f"SELECT DISTINCT accession FROM insider_trades WHERE accession IN ({ph})",
            accessions,
        )
        return {r[0] for r in rows}

    async def _scan_day(self, day: datetime, budget: int) -> tuple[int, int]:
        """Returns (stored, fetches_used). Cursor-resumable within the day."""
        ymd = day.strftime("%Y%m%d")
        url = DAILY_INDEX.format(year=day.year, q=(day.month - 1) // 3 + 1, ymd=ymd)
        try:
            text = await self._http.get_text(url)
        except Exception:
            return 0, 0  # weekend/holiday or not yet published — normal

        entries = []
        for line in text.splitlines():
            m = _IDX_LINE.match(line)
            if m and m.group("form") == "4":  # plain Form 4 only, no /A dups
                entries.append(m)
        done = self._cursor(ymd)
        todo = entries[done:]
        if not todo:
            return 0, 0

        batch = todo[:budget]
        # accession = the .txt basename (0001234567-26-000123).
        accs = [m.group("path").rsplit("/", 1)[-1][:-4] for m in batch]
        seen = self._seen(accs)

        stored = fetches = 0
        for m, accession in zip(batch, accs):
            if accession in seen:
                continue
            try:
                resp = await self._http.get(ARCHIVE.format(path=m.group("path")))
                fetches += 1
            except Exception as exc:
                log.debug("form4 submission fetch failed (%s): %s", accession, exc)
                continue
            xml_bytes = _extract_ownership_xml(resp.body)
            if xml_bytes is None:
                continue
            parsed = _parse_form4(xml_bytes)
            if parsed is None:
                continue  # no open-market buys in this filing
            symbol, issuer = _issuer_info(xml_bytes)
            if symbol is None:
                continue
            shares = sum(b[1] for b in parsed["buys"])
            value = sum(b[1] * b[2] for b in parsed["buys"])
            if value < self._min_value:
                continue
            filed = m.group("date")
            filed_at = datetime.strptime(filed, "%Y%m%d")
            trade_date = min(b[0] for b in parsed["buys"]) or filed_at.date().isoformat()
            self._duck.execute(
                "INSERT OR IGNORE INTO insider_trades "
                "(accession, symbol, issuer_name, insider_name, insider_title, "
                " is_officer, is_director, is_ceo_cfo, filed_at, trade_date, "
                " shares, price, value, url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    accession, symbol, issuer or m.group("company"),
                    parsed["insider"], parsed["title"],
                    parsed["is_officer"], parsed["is_director"], parsed["is_ceo_cfo"],
                    filed_at, datetime.fromisoformat(trade_date),
                    shares, value / shares if shares else None, value,
                    ARCHIVE.format(path=m.group("path")),
                ],
            )
            stored += 1

        self._save_cursor(ymd, done + len(batch))
        remaining = len(todo) - len(batch)
        log.info(
            "insider scan %s: %s buy filing(s) stored from %s Form 4s "
            "(%s remaining for later sweeps)",
            ymd, stored, len(batch), remaining,
        )
        return stored, fetches

    async def run(self) -> int:
        """Walk the last N day-indexes, newest first, within one fetch budget."""
        budget = self._cap
        stored_total = 0
        now = datetime.now(timezone.utc)
        for back in range(self._lookback + 1):
            if budget <= 0:
                break
            stored, fetches = await self._scan_day(now - timedelta(days=back), budget)
            stored_total += stored
            budget -= max(fetches, 0)
        return stored_total
