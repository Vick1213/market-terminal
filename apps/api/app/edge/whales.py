"""EDGAR 13F whale tracker — Phase 8 §3.

Tracks a configured list of famous funds (CIK + label, see Settings) and
stores, per fund, the latest 13F-HR filings: one whale_filings row per filing
(total portfolio value, position count) and the top-N holdings aggregated by
(cusip, class, put/call). Quarter-over-quarter changes (adds, trims, new
positions, exits among the stored top-N) are computed at read time.

Values are USD: 13F infoTables report whole dollars since Jan 2023, and we
only ever pull each fund's two most recent filings. No new API — the same
data.sec.gov / www.sec.gov etiquette as the Form 4 tracker.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from app.db.duck import DuckStore
from app.ingest.http import HttpClient

log = logging.getLogger("market.edge.whales")

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FILING_INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json"
FILING_DOC = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

# Latest two 13F-HR per fund: enough for QoQ diffs without crawling history.
FILINGS_PER_FUND = 2


def _local(tag: str) -> str:
    """'{ns}value' -> 'value' (13F infoTables are namespaced)."""
    return tag.rsplit("}", 1)[-1]


def parse_infotable(xml_bytes: bytes) -> list[dict]:
    """infoTable XML -> holdings aggregated by (cusip, class, put/call)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    agg: dict[tuple[str, str, str], dict] = {}
    for el in root.iter():
        if _local(el.tag) != "infoTable":
            continue
        fields = {_local(c.tag): c for c in el}
        issuer = (fields.get("nameOfIssuer") is not None and fields["nameOfIssuer"].text or "").strip()
        cusip = (fields.get("cusip") is not None and fields["cusip"].text or "").strip()
        cls = (fields.get("titleOfClass") is not None and fields["titleOfClass"].text or "").strip()
        put_call = (fields.get("putCall") is not None and fields["putCall"].text or "").strip()
        try:
            value = float(fields["value"].text)
        except (KeyError, TypeError, ValueError):
            continue
        shares = 0.0
        spa = fields.get("shrsOrPrnAmt")
        if spa is not None:
            for c in spa:
                if _local(c.tag) == "sshPrnamt" and c.text:
                    try:
                        shares = float(c.text)
                    except ValueError:
                        pass
        if not cusip:
            continue
        key = (cusip, cls, put_call)
        cur = agg.setdefault(
            key,
            {"cusip": cusip, "cls": cls, "put_call": put_call, "issuer": issuer,
             "value": 0.0, "shares": 0.0},
        )
        cur["value"] += value
        cur["shares"] += shares
    return list(agg.values())


class WhalesPipeline:
    def __init__(
        self,
        duck: DuckStore,
        http: HttpClient,
        funds: list[str],
        *,
        top_holdings: int = 20,
    ) -> None:
        self._duck = duck
        self._http = http
        self._top = top_holdings
        # "CIK:Label" -> (int cik, label); malformed entries are dropped loudly.
        self._funds: list[tuple[int, str]] = []
        for entry in funds:
            cik_s, _, label = entry.partition(":")
            try:
                self._funds.append((int(cik_s), label.strip() or cik_s))
            except ValueError:
                log.warning("whale_funds entry %r is not 'CIK:Label' — skipped", entry)

    def _have(self, accession: str) -> bool:
        return (
            self._duck.fetchone(
                "SELECT 1 FROM whale_filings WHERE accession = ?", [accession]
            )
            is not None
        )

    async def _infotable_doc(self, cik: int, acc_nodash: str) -> bytes | None:
        """The holdings XML is the non-primary .xml in the filing directory
        (largest one wins if several)."""
        index = await self._http.get_json(FILING_INDEX.format(cik=cik, acc=acc_nodash))
        candidates = [
            i for i in index.get("directory", {}).get("item", [])
            if i.get("name", "").endswith(".xml") and i.get("name") != "primary_doc.xml"
        ]
        if not candidates:
            return None
        def _size(i: dict) -> int:
            try:
                return int(i.get("size") or 0)
            except ValueError:
                return 0
        doc = max(candidates, key=_size)["name"]
        resp = await self._http.get(FILING_DOC.format(cik=cik, acc=acc_nodash, doc=doc))
        return resp.body

    async def run(self) -> int:
        stored = 0
        for cik, label in self._funds:
            try:
                sub = await self._http.get_json(SUBMISSIONS.format(cik=cik))
            except Exception as exc:
                log.warning("whales submissions failed for %s: %s", label, exc)
                continue
            recent = sub.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            picked = 0
            for i, form in enumerate(forms):
                if form != "13F-HR" or picked >= FILINGS_PER_FUND:
                    continue
                picked += 1
                accession = recent["accessionNumber"][i]
                if self._have(accession):
                    continue
                acc_nodash = accession.replace("-", "")
                try:
                    xml_bytes = await self._infotable_doc(cik, acc_nodash)
                except Exception as exc:
                    log.warning("whales %s %s index/doc failed: %s", label, accession, exc)
                    continue
                if xml_bytes is None:
                    log.warning("whales %s %s: no infotable xml", label, accession)
                    continue
                holdings = parse_infotable(xml_bytes)
                if not holdings:
                    continue
                total = sum(h["value"] for h in holdings)
                holdings.sort(key=lambda h: h["value"], reverse=True)
                self._duck.execute(
                    "INSERT OR IGNORE INTO whale_filings "
                    "(accession, cik, fund, period, filed_at, total_value, positions) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        accession, cik, label,
                        datetime.fromisoformat(recent["reportDate"][i]),
                        datetime.fromisoformat(recent["filingDate"][i]),
                        total, len(holdings),
                    ],
                )
                self._duck.executemany(
                    "INSERT OR REPLACE INTO whale_holdings "
                    "(accession, cusip, cls, put_call, issuer, value, shares, pct, rank) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            accession, h["cusip"], h["cls"], h["put_call"], h["issuer"],
                            h["value"], h["shares"],
                            h["value"] / total if total else None, rank,
                        )
                        for rank, h in enumerate(holdings[: self._top], start=1)
                    ],
                )
                stored += 1
                log.info(
                    "whales: stored %s %s (%s, $%.1fbn, %s positions)",
                    label, recent["reportDate"][i], accession, total / 1e9, len(holdings),
                )
        return stored


def whale_new_positions(
    duck: DuckStore, *, days: int | None = 7, top_rank: int = 10
) -> list[dict]:
    """Top-``top_rank`` positions in each fund's latest filing whose
    (cusip, cls, put_call) was absent from that fund's previous filing.
    ``days`` filters on the latest filing's filed_at (None = no filter).
    Funds with a single stored filing are skipped — "new" needs a baseline.
    Blocking — run_in_executor."""
    filings = duck.fetchall(
        "SELECT accession, cik, fund, period, filed_at "
        "FROM whale_filings ORDER BY cik, period DESC"
    )
    by_fund: dict[int, list] = {}
    for f in filings:
        by_fund.setdefault(f[1], []).append(f)
    cutoff = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        if days is not None else None
    )
    out: list[dict] = []
    for cik, fs in by_fund.items():
        if len(fs) < 2:
            continue
        latest, prev = fs[0], fs[1]
        if cutoff is not None and latest[4] < cutoff:
            continue
        prev_keys = {
            (r[0], r[1], r[2])
            for r in duck.fetchall(
                "SELECT cusip, cls, put_call FROM whale_holdings WHERE accession = ?",
                [prev[0]],
            )
        }
        rows = duck.fetchall(
            "SELECT cusip, cls, put_call, issuer, value, rank FROM whale_holdings "
            "WHERE accession = ? AND rank <= ? ORDER BY rank",
            [latest[0], top_rank],
        )
        for r in rows:
            if (r[0], r[1], r[2]) in prev_keys:
                continue
            out.append({
                "cik": cik, "fund": latest[2], "accession": latest[0],
                "period": str(latest[3])[:10], "filed_at": str(latest[4])[:10],
                "cusip": r[0], "cls": r[1], "put_call": r[2], "issuer": r[3],
                "value": float(r[4]) if r[4] is not None else None,
                "rank": int(r[5]),
            })
    out.sort(key=lambda h: (h["fund"], h["rank"]))
    return out


def whales_summary(duck: DuckStore) -> list[dict]:
    """Latest filing per fund + QoQ changes vs the prior stored filing.
    'New'/'exit' are relative to the stored top-N — a position drifting in or
    out of the top-N reads as new/exit, which is the honest bound of 13F data
    without storing thousand-row portfolios. Blocking — run_in_executor."""
    filings = duck.fetchall(
        "SELECT accession, cik, fund, period, filed_at, total_value, positions "
        "FROM whale_filings ORDER BY cik, period DESC"
    )
    by_fund: dict[int, list] = {}
    for f in filings:
        by_fund.setdefault(f[1], []).append(f)

    out: list[dict] = []
    for cik, fs in by_fund.items():
        latest = fs[0]
        prev = fs[1] if len(fs) > 1 else None
        rows = duck.fetchall(
            "SELECT cusip, cls, put_call, issuer, value, shares, pct, rank "
            "FROM whale_holdings WHERE accession = ? ORDER BY rank",
            [latest[0]],
        )
        prev_by_key: dict[tuple, tuple] = {}
        if prev:
            for r in duck.fetchall(
                "SELECT cusip, cls, put_call, issuer, value, shares, pct "
                "FROM whale_holdings WHERE accession = ?",
                [prev[0]],
            ):
                prev_by_key[(r[0], r[1], r[2])] = r
        latest_keys = set()
        holdings = []
        for r in rows:
            key = (r[0], r[1], r[2])
            latest_keys.add(key)
            p = prev_by_key.get(key)
            chg = None
            status = "held"
            if prev is not None:
                if p is None:
                    status = "new"
                elif p[5] and r[5] is not None:
                    chg = (float(r[5]) - float(p[5])) / float(p[5]) * 100  # share-count Δ%
                    if abs(chg) < 0.5:
                        chg = 0.0
            holdings.append({
                "issuer": r[3], "cls": r[1], "put_call": r[2],
                "value": float(r[4]) if r[4] is not None else None,
                "pct": round(float(r[6]) * 100, 1) if r[6] is not None else None,
                "rank": r[7],
                "shares_chg_pct": round(chg, 1) if chg is not None else None,
                "status": status,
            })
        exits = sorted(
            {v[3] for k, v in prev_by_key.items() if k not in latest_keys and v[3]}
        )
        out.append({
            "cik": cik,
            "fund": latest[2],
            "period": str(latest[3])[:10],
            "filed_at": str(latest[4])[:10],
            "total_value": float(latest[5]) if latest[5] is not None else None,
            "positions": int(latest[6]) if latest[6] is not None else None,
            "prev_period": str(prev[3])[:10] if prev else None,
            "holdings": holdings,
            "new_count": sum(1 for h in holdings if h["status"] == "new"),
            "exits": exits,
        })
    out.sort(key=lambda f: f["total_value"] or 0, reverse=True)
    return out
