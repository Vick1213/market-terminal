"""Phase 16 §11 rank 7 — Treasury auction demand (FiscalData auctions_query).

Per-auction bid-to-cover + bidder-class takedown is the bond-supply early
warning the terminal lacked. Heavy primary-dealer takedown means real money
(indirect = foreign/central banks; direct = domestic non-dealer) stayed away,
so dealers were stuck with the paper — a yield-spike / weak-demand tell. A low
bid-to-cover or a tail (stop-out yield above the when-issued level) says the
same. We keep the last row per CUSIP in the treasury_auctions table.

Keyless JSON, same FiscalData client already used for the TGA leg
(app/ingest/liquidity.py). Same fail-soft contract as every other ingestor.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from app.db.duck import DuckStore
from app.ingest.http import HttpClient
from app.ingest.macro import _parse_date

log = logging.getLogger("market.ingest.treasury")

AUCTIONS_QUERY = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
    "/v1/accounting/od/auctions_query"
)
# Only completed auctions carry the demand fields (announced-but-unsold rows
# have null bid_to_cover); filtering on it server-side skips the upcoming ones.
AUCTION_FIELDS = (
    "cusip,auction_date,security_type,security_term,bid_to_cover_ratio,"
    "high_yield,offering_amt,total_accepted,primary_dealer_accepted,"
    "indirect_bidder_accepted,direct_bidder_accepted"
)
DEFAULT_START = "2022-01-01"


def _f(v) -> float | None:
    """FiscalData serializes numbers as strings and missing as the literal
    'null' string; coerce both to a real float-or-None."""
    if v is None or v == "null" or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(part: float | None, total: float | None) -> float | None:
    if part is None or not total:
        return None
    return part / total


async def fetch_auctions(http: HttpClient, start_iso: str) -> list[tuple]:
    """Completed auctions since start_iso -> treasury_auctions rows."""
    try:
        data = await http.get_json(
            AUCTIONS_QUERY,
            params={
                "filter": f"auction_date:gte:{start_iso},bid_to_cover_ratio:gt:0",
                "fields": AUCTION_FIELDS,
                "sort": "-auction_date",
                "page[size]": "10000",
            },
            conditional=False,
        )
        records = data.get("data", [])
    except Exception as exc:
        log.warning("fiscaldata auctions failed: %s", exc)
        return []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows: list[tuple] = []
    for r in records:
        cusip = r.get("cusip")
        day = _parse_date(r.get("auction_date", ""))
        if not cusip or day is None:
            continue
        total = _f(r.get("total_accepted"))
        rows.append(
            (
                cusip,
                datetime(day.year, day.month, day.day),
                r.get("security_type"),
                r.get("security_term"),
                _f(r.get("bid_to_cover_ratio")),
                _f(r.get("high_yield")),
                _f(r.get("offering_amt")),
                total,
                _pct(_f(r.get("primary_dealer_accepted")), total),
                _pct(_f(r.get("indirect_bidder_accepted")), total),
                _pct(_f(r.get("direct_bidder_accepted")), total),
                now,
            )
        )
    return rows


class TreasuryAuctionPipeline:
    """fetch completed auctions -> upsert treasury_auctions (replace per CUSIP)."""

    def __init__(self, duck: DuckStore, http: HttpClient, *, start: str = DEFAULT_START) -> None:
        self._duck = duck
        self._http = http
        self._start = start

    def _start_iso(self) -> str:
        """Incremental: re-pull a couple weeks back from the latest auction so
        any reopened/corrected rows refresh; full history on an empty table."""
        row = self._duck.fetchone("SELECT max(auction_date) FROM treasury_auctions")
        if row and row[0]:
            return (row[0] - timedelta(days=14)).strftime("%Y-%m-%d")
        return self._start

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        start_iso = await loop.run_in_executor(None, self._start_iso)
        rows = await fetch_auctions(self._http, start_iso)
        if rows:
            await loop.run_in_executor(
                None,
                self._duck.executemany,
                "INSERT OR REPLACE INTO treasury_auctions "
                "(cusip, auction_date, security_type, security_term, bid_to_cover, "
                "high_yield, offering_amt, total_accepted, dealer_pct, indirect_pct, "
                "direct_pct, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        log.info("treasury auctions ingest: %s rows since %s", len(rows), start_iso)
