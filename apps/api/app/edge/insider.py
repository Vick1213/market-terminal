"""SEC Form 4 insider cluster-buy tracker (PLAN §6 #4).

Scope is deliberately bounded to WATCHLIST issuers: the submissions JSON per
CIK lists recent filings, we pull only unseen Form 4s inside the lookback and
keep only open-market purchases (transaction code ``P``, acquired). Cluster
detection (>=N distinct insiders, or any CEO/CFO buy) happens at read time in
SQL and in the alert engine — the table stores plain per-filing buy rows.

EDGAR etiquette is inherited from the shared HttpClient (descriptive UA,
8 req/s cap on data.sec.gov / www.sec.gov).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ingest.http import HttpClient
from app.ingest.news import _cik_map  # ticker -> CIK, cached 7d in app_meta

log = logging.getLogger("market.edge.insider")

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_DOC = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
FILING_INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"

_CEO_CFO_HINTS = ("CEO", "CHIEF EXECUTIVE", "CFO", "CHIEF FINANCIAL")


def _text(root: ET.Element, path: str) -> str:
    el = root.find(path)
    return el.text.strip() if el is not None and el.text else ""


def _parse_form4(xml_bytes: bytes) -> dict | None:
    """Raw ownership XML -> {insider, title, flags, buys: [(date, shares, price)]}."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    owner = root.find("reportingOwner")
    if owner is None:
        return None
    name = _text(owner, "reportingOwnerId/rptOwnerName")
    rel = owner.find("reportingOwnerRelationship")
    is_officer = rel is not None and _text(rel, "isOfficer") in ("1", "true")
    is_director = rel is not None and _text(rel, "isDirector") in ("1", "true")
    title = _text(rel, "officerTitle") if rel is not None else ""
    is_ceo_cfo = any(h in title.upper() for h in _CEO_CFO_HINTS)

    buys: list[tuple[str, float, float]] = []
    for txn in root.iterfind("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(txn, "transactionCoding/transactionCode")
        acq = _text(txn, "transactionAmounts/transactionAcquiredDisposedCode/value")
        if code != "P" or acq != "A":
            continue
        try:
            shares = float(_text(txn, "transactionAmounts/transactionShares/value") or 0)
            price = float(_text(txn, "transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        date = _text(txn, "transactionDate/value")
        if shares > 0:
            buys.append((date, shares, price))
    if not name or not buys:
        return None
    return {
        "insider": name,
        "title": title,
        "is_officer": is_officer,
        "is_director": is_director,
        "is_ceo_cfo": is_ceo_cfo,
        "buys": buys,
    }


class InsiderPipeline:
    def __init__(
        self,
        duck: DuckStore,
        sqlite: SqliteStore,
        http: HttpClient,
        *,
        lookback_days: int = 30,
        min_trade_value: float = 25_000,
    ) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._http = http
        self._lookback = lookback_days
        self._min_value = min_trade_value

    def _watchlist_equities(self) -> list[str]:
        rows = self._sqlite.fetchall(
            "SELECT symbol FROM watchlist WHERE asset_class = 'equity' ORDER BY sort_order"
        )
        return [r[0] for r in rows]

    def _seen_accessions(self, symbol: str) -> set[str]:
        rows = self._duck.fetchall(
            "SELECT DISTINCT accession FROM insider_trades WHERE symbol = ?", [symbol]
        )
        return {r[0] for r in rows}

    async def _form4_xml(self, cik: int, acc_nodash: str, primary_doc: str) -> bytes | None:
        """Fetch the raw ownership XML (strip the xslF345X.. rendering prefix)."""
        doc = primary_doc.split("/")[-1]
        if not doc.endswith(".xml"):
            return None
        try:
            resp = await self._http.get(
                ARCHIVE_DOC.format(cik=cik, acc=acc_nodash, doc=doc)
            )
            return resp.body
        except Exception as exc:
            log.debug("form4 doc fetch failed (%s/%s): %s", acc_nodash, doc, exc)
            return None

    async def run(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self._lookback)).date()
        cik_by_ticker = await _cik_map(self._http, self._sqlite)
        stored = 0
        for sym in self._watchlist_equities():
            cik = cik_by_ticker.get(sym.upper())
            if cik is None:
                continue
            try:
                sub = await self._http.get_json(SUBMISSIONS.format(cik=cik))
            except Exception as exc:
                log.warning("submissions failed for %s: %s", sym, exc)
                continue
            recent = sub.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            seen = self._seen_accessions(sym)
            issuer = sub.get("name", sym)
            for i, form in enumerate(forms):
                if form != "4":
                    continue
                filed = recent["filingDate"][i]
                if filed < cutoff.isoformat():
                    continue
                accession = recent["accessionNumber"][i]
                if accession in seen:
                    continue
                acc_nodash = accession.replace("-", "")
                xml_bytes = await self._form4_xml(
                    cik, acc_nodash, recent["primaryDocument"][i]
                )
                if xml_bytes is None:
                    continue
                parsed = _parse_form4(xml_bytes)
                if parsed is None:
                    continue  # no open-market buys in this filing
                shares = sum(b[1] for b in parsed["buys"])
                value = sum(b[1] * b[2] for b in parsed["buys"])
                if value < self._min_value:
                    continue
                avg_price = value / shares if shares else None
                trade_date = min(b[0] for b in parsed["buys"]) or filed
                self._duck.execute(
                    "INSERT OR IGNORE INTO insider_trades "
                    "(accession, symbol, issuer_name, insider_name, insider_title, "
                    " is_officer, is_director, is_ceo_cfo, filed_at, trade_date, "
                    " shares, price, value, url) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        accession, sym, issuer, parsed["insider"], parsed["title"],
                        parsed["is_officer"], parsed["is_director"], parsed["is_ceo_cfo"],
                        datetime.fromisoformat(filed), datetime.fromisoformat(trade_date),
                        shares, avg_price, value,
                        FILING_INDEX.format(cik=cik, acc=acc_nodash),
                    ],
                )
                stored += 1
        if stored:
            log.info("insider ingest: %s new Form 4 buy filing(s)", stored)
        return stored
