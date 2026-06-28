"""Phase 16 §11 ranks 12-13 — EDGAR corporate-event scanners.

Two siblings of ``app/edge/insider_scan.py`` (the market-wide Form 4 sweeper),
but driven off EDGAR full-text search (EFTS, ``efts.sec.gov``) instead of the
daily form index, because the targets here are sparse needles in the 8-K /
Schedule-13 haystack:

Rank 12 — **8-K item-code catalysts.** Each tradeable catalyst is a distinct
8-K item code: 1.05 (material cybersecurity incident, mandatory since Dec-2023),
2.06 (material impairment / write-off), 5.02 (executive departure), 8.01 +
"repurchase" (buyback). EFTS is queried per catalyst with a discriminating
full-text phrase, then every hit is confirmed against the filing's *structured*
``items`` field (the full-text phrase is only a pre-filter — a body can mention
"Item 1.05" without actually reporting it). Results land in ``edgar_8k_events``.

Rank 13 — **SC 13D / 13G stakes.** EFTS with an empty ``q`` and a ``forms``
filter returns every 13D/13G (and their /A amendments) in the window. Since the
Dec-2024 mandate, new filings ship a machine-readable ``primary_doc.xml`` (holder
name, share count, % of class, stated purpose); we parse it when present and fall
back to the filing-level metadata (filer, subject, type, url) otherwise — a
filing is never dropped just because its structured body is missing. 13D = intent
to influence (moves the target fast); 13G = passive. Results land in
``edgar_13d_filings``.

Ticker resolution is free: EFTS ``display_names`` embed the ticker inline
("Karat Packaging Inc.  (KRT)  (CIK 0001758021)"), so no company_tickers.json
round-trip is needed. EDGAR etiquette (descriptive UA, 8 req/s on the SEC hosts)
is inherited from the shared HttpClient; EFTS intermittently 500s, so every query
is fail-soft (a missing ``hits`` key skips that page, the cursor-free re-run picks
it up next sweep). Accession primary keys + ``INSERT OR IGNORE`` make every sweep
idempotent.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from app.db.duck import DuckStore
from app.ingest.http import HttpClient

log = logging.getLogger("market.ingest.edgar_events")

EFTS = "https://efts.sec.gov/LATEST/search-index"
FILING_INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{adsh}-index.htm"
PRIMARY_DOC = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/primary_doc.xml"

_PAGE = 10  # EFTS returns 10 hits per page; paginate with `from`.

# ticker(s) embedded in an EFTS display name, e.g. "ACME INC  (AAA, AAA-PB)  (CIK 0001..)".
_TICKER_IN_NAME = re.compile(
    r"\(([A-Z][A-Z0-9.\-]*(?:,\s*[A-Z0-9.\-]+)*)\)\s*\(CIK\s*\d+\)\s*$"
)
_TRAILING_PARENS = re.compile(r"\s*\([^)]*\)\s*$")


def _ticker_from_display(name: str) -> str | None:
    """First (common-class) ticker from an EFTS display name, or None."""
    m = _TICKER_IN_NAME.search(name.strip())
    if not m:
        return None
    first = m.group(1).split(",")[0].strip()
    return first or None


def _clean_name(name: str) -> str:
    """Strip the trailing "(TICKER)" / "(CIK ...)" parens from a display name."""
    out = name.strip()
    for _ in range(2):  # at most a ticker group then a CIK group
        out = _TRAILING_PARENS.sub("", out).strip()
    return out


def _filed_at(src: dict) -> datetime:
    try:
        return datetime.strptime(src["file_date"], "%Y-%m-%d")
    except (KeyError, ValueError):
        return datetime.now(timezone.utc)


async def _efts_hits(
    http: HttpClient, q: str, forms: str, start: str, end: str, from_: int
) -> list[dict] | None:
    """One EFTS page -> list of hit ``_source`` dicts, or None on a soft failure
    (transient 500 body / shape we don't recognise)."""
    params = {"q": q, "forms": forms, "dateRange": "custom",
              "startdt": start, "enddt": end}
    if from_:
        params["from"] = from_
    try:
        data = await http.get_json(EFTS, params=params)
    except Exception as exc:
        log.debug("efts fetch failed (%s %s from=%s): %s", forms, q, from_, exc)
        return None
    if not isinstance(data, dict) or "hits" not in data:
        return None
    return [h.get("_source", {}) for h in data["hits"]["hits"]]


# --------------------------------------------------------------------------- 8-K

# Highest-priority catalyst first: a filing matching several is stored once,
# under the first match (INSERT OR IGNORE on the accession PK).
ITEM_CATALYSTS = [
    {"code": "1.05", "q": '"Item 1.05"',
     "label": "Material cybersecurity incident"},
    {"code": "2.06", "q": '"Item 2.06"',
     "label": "Material impairment / write-off"},
    {"code": "5.02", "q": '"Item 5.02" "resigned"',
     "label": "Executive departure"},
    {"code": "8.01", "q": '"Item 8.01" "repurchase"',
     "label": "Share repurchase program"},
]


class Edgar8KPipeline:
    """EFTS per-catalyst -> structured items-field confirm -> edgar_8k_events."""

    def __init__(
        self,
        duck: DuckStore,
        http: HttpClient,
        *,
        lookback_days: int = 3,
        max_pages: int = 30,
    ) -> None:
        self._duck = duck
        self._http = http
        self._lookback = lookback_days
        self._max_pages = max_pages

    def _window(self) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=self._lookback)).strftime("%Y-%m-%d")
        return start, now.strftime("%Y-%m-%d")

    def _known(self, accs: list[str]) -> set[str]:
        if not accs:
            return set()
        ph = ", ".join("?" for _ in accs)
        rows = self._duck.fetchall(
            f"SELECT accession FROM edgar_8k_events WHERE accession IN ({ph})", accs
        )
        return {r[0] for r in rows}

    async def _scan_catalyst(self, cat: dict, start: str, end: str, seen: set[str]) -> int:
        # Gather candidates whose STRUCTURED items field actually carries this
        # catalyst (the full-text phrase is only a pre-filter), deduped by adsh.
        cands: dict[str, dict] = {}
        for page in range(self._max_pages):
            hits = await self._efts(cat["q"], start, end, page * _PAGE)
            if not hits:
                break
            for src in hits:
                adsh = src.get("adsh")
                if not adsh or adsh in cands or adsh in seen:
                    continue
                if cat["code"] in [str(i) for i in (src.get("items") or [])]:
                    cands[adsh] = src
            if len(hits) < _PAGE:
                break
        # Insert only genuinely-new filings so the count/log are honest; an
        # accession already stored (this run, or a prior sweep) is skipped.
        known = self._known(list(cands))
        stored = 0
        for adsh, src in cands.items():
            seen.add(adsh)
            if adsh in known:
                continue
            items = [str(i) for i in (src.get("items") or [])]
            names = src.get("display_names") or []
            ciks = src.get("ciks") or []
            cik = ciks[0] if ciks else None
            company = _clean_name(names[0]) if names else None
            ticker = _ticker_from_display(names[0]) if names else None
            url = (
                FILING_INDEX.format(cik=int(cik), acc=adsh.replace("-", ""), adsh=adsh)
                if cik else None
            )
            self._duck.execute(
                "INSERT OR IGNORE INTO edgar_8k_events "
                "(accession, cik, ticker, company, item_code, catalyst, "
                " items, filed_at, url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [adsh, cik, ticker, company, cat["code"], cat["label"],
                 ",".join(items), _filed_at(src), url],
            )
            stored += 1
        return stored

    async def _efts(self, q: str, start: str, end: str, from_: int) -> list[dict] | None:
        return await _efts_hits(self._http, q, "8-K", start, end, from_)

    def _refresh_counts(self) -> None:
        """Chartable daily catalyst frequency -> ts_macro EDGAR_8K_<code>_COUNT."""
        self._duck.execute(
            "INSERT OR REPLACE INTO ts_macro (series_id, ts, value, source) "
            "SELECT 'EDGAR_8K_' || replace(item_code, '.', '_') || '_COUNT', "
            "       date_trunc('day', filed_at), count(*), 'edgar_8k' "
            "FROM edgar_8k_events GROUP BY 1, 2"
        )

    async def run(self) -> int:
        start, end = self._window()
        total = 0
        seen: set[str] = set()  # an accession is filed under one catalyst only
        for cat in ITEM_CATALYSTS:
            total += await self._scan_catalyst(cat, start, end, seen)
        if total:
            self._refresh_counts()
            log.info("edgar 8-K scan: %s new item-code event(s)", total)
        return total


# -------------------------------------------------------------------- SC 13D/13G

_NS_DECL = re.compile(r'\sxmlns(:\w+)?="[^"]*"')
_NS_PREFIX = re.compile(r"(</?)\w+:")


def _strip_ns(xml_text: str) -> str:
    return _NS_PREFIX.sub(r"\1", _NS_DECL.sub("", xml_text))


# SC 13D and SC 13G ship different schemas; the reporting-person block and the
# share/percent tags differ. We read whichever set is present (scoped to each
# person block so the European-formatted "19,6 %" cover field is never picked up).
_PERSON_TAGS = ("reportingPersonInfo", "coverPageHeaderReportingPersonDetails")
_PCT_TAGS = ("percentOfClass", "classPercent")
_SHARE_TAGS = ("aggregateAmountOwned",
               "reportingPersonBeneficiallyOwnedAggregateNumberOfShares")


def _num(text: str | None) -> float | None:
    if not text:
        return None
    raw = text.strip().rstrip("%").strip().replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_filing_xml(xml_text: str) -> dict:
    """primary_doc.xml (13D or 13G) -> {filer, pct, shares, purpose, subject_name}.
    Any field absent on parse failure / older schema returns None for that field."""
    out: dict = {"filer": None, "pct": None, "shares": None,
                 "purpose": None, "subject_name": None}
    try:
        root = ET.fromstring(_strip_ns(xml_text))
    except ET.ParseError:
        return out
    out["subject_name"] = (root.findtext(".//issuerInfo/issuerName") or "").strip() or None

    persons: list[ET.Element] = []
    for tag in _PERSON_TAGS:
        persons.extend(root.findall(f".//{tag}"))

    names: list[str] = []
    pcts: list[float] = []
    shares: list[float] = []
    for p in persons:
        n = (p.findtext("reportingPersonName") or "").strip()
        if n:
            names.append(n)
        for tag in _PCT_TAGS:
            v = _num(p.findtext(tag))
            if v is not None:
                pcts.append(v)
        for tag in _SHARE_TAGS:
            v = _num(p.findtext(tag))
            if v is not None:
                shares.append(v)
    if names:
        out["filer"] = "; ".join(dict.fromkeys(names))[:300]
    if pcts:
        out["pct"] = round(max(pcts), 4)
    if shares:
        out["shares"] = max(shares)
    purpose = (root.findtext(".//transactionPurpose") or "").strip()  # 13D only
    if purpose:
        out["purpose"] = re.sub(r"\s+", " ", purpose)[:1500]
    return out


class Edgar13DPipeline:
    """EFTS forms filter -> structured primary_doc.xml (when present) -> edgar_13d_filings."""

    def __init__(
        self,
        duck: DuckStore,
        http: HttpClient,
        *,
        lookback_days: int = 3,
        max_pages: int = 40,
        max_xml_fetches: int = 250,
    ) -> None:
        self._duck = duck
        self._http = http
        self._lookback = lookback_days
        self._max_pages = max_pages
        self._xml_budget = max_xml_fetches

    def _window(self) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=self._lookback)).strftime("%Y-%m-%d")
        return start, now.strftime("%Y-%m-%d")

    def _known(self, accs: list[str]) -> set[str]:
        if not accs:
            return set()
        ph = ", ".join("?" for _ in accs)
        rows = self._duck.fetchall(
            f"SELECT accession FROM edgar_13d_filings WHERE accession IN ({ph})", accs
        )
        return {r[0] for r in rows}

    async def _structured(self, ciks: list[str], acc_nodash: str) -> dict | None:
        """Fetch + parse primary_doc.xml, trying each associated CIK path (the
        filing dir lives under the subject CIK, but try the others if not)."""
        for cik in ciks:
            try:
                cik_int = int(cik)
            except (TypeError, ValueError):
                continue
            try:
                text = await self._http.get_text(
                    PRIMARY_DOC.format(cik=cik_int, acc=acc_nodash)
                )
            except Exception:
                continue
            if "<edgarSubmission" in text or "ReportingPerson" in text:
                return _parse_filing_xml(text)
        return None

    async def run(self) -> int:
        start, end = self._window()
        stored = 0
        budget = self._xml_budget
        for forms in ("SCHEDULE 13D", "SCHEDULE 13G"):
            seen: set[str] = set()
            page_records: list[dict] = []
            for page in range(self._max_pages):
                hits = await _efts_hits(self._http, "", forms, start, end, page * _PAGE)
                if not hits:
                    break
                for src in hits:
                    adsh = src.get("adsh")
                    if not adsh or adsh in seen:
                        continue
                    seen.add(adsh)
                    page_records.append(src)
                if len(hits) < _PAGE:
                    break
            fresh = [s for s in page_records if s["adsh"] not in self._known(
                [s["adsh"] for s in page_records]
            )]
            for src in fresh:
                adsh = src["adsh"]
                names = src.get("display_names") or []
                ciks = src.get("ciks") or []
                form = src.get("form") or forms
                subject_name = _clean_name(names[0]) if names else None
                subject_ticker = _ticker_from_display(names[0]) if names else None
                filer_name = _clean_name(names[1]) if len(names) > 1 else None
                subject_cik = ciks[0] if ciks else None
                acc_nodash = adsh.replace("-", "")

                pct = shares = purpose = None
                if budget > 0:
                    parsed = await self._structured(ciks, acc_nodash)
                    budget -= 1
                    if parsed:
                        pct, shares, purpose = parsed["pct"], parsed["shares"], parsed["purpose"]
                        filer_name = parsed["filer"] or filer_name
                        subject_name = parsed["subject_name"] or subject_name

                url = (
                    FILING_INDEX.format(cik=int(subject_cik), acc=acc_nodash, adsh=adsh)
                    if subject_cik else None
                )
                self._duck.execute(
                    "INSERT OR IGNORE INTO edgar_13d_filings "
                    "(accession, subject_cik, subject_ticker, subject_name, "
                    " filer_name, filing_type, is_activist, pct_owned, shares, "
                    " purpose, filed_at, url) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [adsh, subject_cik, subject_ticker, subject_name, filer_name,
                     form, "13D" in form, pct, shares, purpose, _filed_at(src), url],
                )
                stored += 1
        if stored:
            self._refresh_counts()
            log.info("edgar 13D/13G scan: %s new filing(s)", stored)
        return stored

    def _refresh_counts(self) -> None:
        """Chartable daily filing frequency -> ts_macro EDGAR_13D_COUNT / _13G_COUNT."""
        self._duck.execute(
            "INSERT OR REPLACE INTO ts_macro (series_id, ts, value, source) "
            "SELECT CASE WHEN is_activist THEN 'EDGAR_13D_COUNT' ELSE 'EDGAR_13G_COUNT' END, "
            "       date_trunc('day', filed_at), count(*), 'edgar_13d' "
            "FROM edgar_13d_filings GROUP BY 1, 2"
        )
