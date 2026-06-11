"""FINRA consolidated equity short interest — Phase 11 #5 (PLAN §10).

Bi-monthly TRUE short interest (shares held short, days-to-cover) from
FINRA's keyless Query API — distinct from the daily short-SALE volume in
``ts_short_vol``, which PLAN §5 warns is mostly market-maker hedging flow.

The dataset is partitioned by settlement date (published ~9 days after each
mid/end-of-month settlement). The daily run diffs the partition list against
stored settlement dates and pulls watchlist symbols for anything new — a
no-op between publication days.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ingest.http import HttpClient

log = logging.getLogger("market.edge.short_interest")

PARTITIONS_URL = (
    "https://api.finra.org/partitions/group/otcMarket/name/consolidatedShortInterest"
)
DATA_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"

# Settlement dates ingested on first run (~1 year of bi-monthly prints) so
# trend/percentile reads have history from day one.
BACKFILL_PARTITIONS = 26


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class ShortInterestPipeline:
    def __init__(self, duck: DuckStore, sqlite: SqliteStore, http: HttpClient) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._http = http

    def _symbols(self) -> list[str]:
        """Watchlist symbols FINRA can know: plain US tickers (equities and
        metal ETFs), not crypto pairs or spot codes like XAU."""
        rows = self._sqlite.fetchall(
            "SELECT symbol FROM watchlist WHERE asset_class IN ('equity', 'metal')"
        )
        return [r[0].upper() for r in rows if r[0].isalpha() and len(r[0]) <= 5]

    async def run(self) -> int:
        symbols = self._symbols()
        if not symbols:
            return 0
        try:
            parts = await self._http.get_json(
                PARTITIONS_URL, headers={"Accept": "application/json"}
            )
        except Exception as exc:
            log.warning("short interest partitions failed: %s", exc)
            return 0
        available = [
            p["partitions"][0]
            for p in parts.get("availablePartitions", [])
            if p.get("partitions")
        ][:BACKFILL_PARTITIONS]
        have = {
            str(r[0])[:10]
            for r in self._duck.fetchall(
                "SELECT DISTINCT settlement_date FROM ts_short_interest"
            )
        }
        new_dates = [d for d in available if d not in have]
        total = 0
        for date in new_dates:
            try:
                resp = await self._http.post(
                    DATA_URL,
                    json_body={
                        "limit": len(symbols) + 10,
                        "compareFilters": [{
                            "fieldName": "settlementDate",
                            "compareType": "EQUAL",
                            "fieldValue": date,
                        }],
                        "domainFilters": [{
                            "fieldName": "symbolCode",
                            "values": symbols,
                        }],
                    },
                    headers={"Accept": "application/json",
                             "Content-Type": "application/json"},
                )
                rows = resp.json()
            except Exception as exc:
                log.warning("short interest pull %s failed: %s", date, exc)
                continue
            records = [
                (
                    datetime.fromisoformat(date),
                    r["symbolCode"],
                    _f(r.get("currentShortPositionQuantity")),
                    _f(r.get("previousShortPositionQuantity")),
                    _f(r.get("changePercent")),
                    _f(r.get("averageDailyVolumeQuantity")),
                    _f(r.get("daysToCoverQuantity")),
                )
                for r in rows
                if r.get("symbolCode")
            ]
            self._duck.executemany(
                "INSERT OR REPLACE INTO ts_short_interest "
                "(settlement_date, symbol, shares_short, shares_short_prev, "
                " change_pct, avg_daily_volume, days_to_cover) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                records,
            )
            total += len(records)
            log.info("short interest: stored %s symbols for %s", len(records), date)
        return total


def short_interest_summary(duck: DuckStore) -> list[dict]:
    """Latest print per symbol + days-to-cover percentile vs stored history.
    Blocking — run_in_executor."""
    df = duck.fetchdf(
        "SELECT settlement_date, symbol, shares_short, change_pct, "
        "avg_daily_volume, days_to_cover FROM ts_short_interest "
        "ORDER BY symbol, settlement_date"
    )
    out: list[dict] = []
    if df is None or df.empty:
        return out
    for sym, g in df.groupby("symbol"):
        last = g.iloc[-1]
        dtc = last["days_to_cover"]
        hist = g["days_to_cover"].dropna()
        pctile = (
            round(float((hist <= dtc).mean() * 100), 0)
            if dtc == dtc and len(hist) >= 4 else None
        )
        out.append({
            "symbol": sym,
            "settlement_date": str(last["settlement_date"])[:10],
            "shares_short": float(last["shares_short"])
            if last["shares_short"] == last["shares_short"] else None,
            "change_pct": float(last["change_pct"])
            if last["change_pct"] == last["change_pct"] else None,
            "days_to_cover": float(dtc) if dtc == dtc else None,
            "dtc_percentile": pctile,
            "prints": int(len(g)),
        })
    out.sort(key=lambda r: -(r["days_to_cover"] or 0))
    return out
