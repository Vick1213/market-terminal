"""CFTC Commitments-of-Traders positioning (PLAN §6 #6).

Weekly legacy futures-only report via the public Socrata API (keyless,
verified in the plan's source audit). We track a fixed set of macro-relevant
contract market codes; the COT Index (3-year percentile of net non-commercial
positioning) is computed at read time from stored history, and extremes are
picked up by the alert engine.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.db.duck import DuckStore
from app.ingest.http import HttpClient

log = logging.getLogger("market.edge.cot")

SOCRATA_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"

# CFTC contract market code -> short label. Codes are stable identifiers
# (names drift); an empty pull is logged so a dead code is visible.
MARKETS = {
    "088691": "GOLD",
    "084691": "SILVER",
    "067651": "WTI CRUDE",
    "13874A": "E-MINI S&P",
    "043602": "10Y T-NOTE",
    "098662": "USD INDEX",
    "133741": "BITCOIN",
    "1170E1": "VIX",
}

# ~3.3y of weekly prints — enough for the 3y COT-index window.
_LIMIT = 170


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class CotPipeline:
    def __init__(self, duck: DuckStore, http: HttpClient) -> None:
        self._duck = duck
        self._http = http

    async def run(self) -> int:
        total = 0
        for code, label in MARKETS.items():
            try:
                rows = await self._http.get_json(
                    SOCRATA_URL,
                    params={
                        "$where": f"cftc_contract_market_code='{code}'",
                        "$order": "report_date_as_yyyy_mm_dd DESC",
                        "$limit": str(_LIMIT),
                    },
                )
            except Exception as exc:
                log.warning("cot %s (%s) failed: %s", label, code, exc)
                continue
            if not rows:
                log.warning("cot %s (%s): no rows — market code may have changed", label, code)
                continue
            records = []
            for r in rows:
                ts = r.get("report_date_as_yyyy_mm_dd", "")[:10]
                if not ts:
                    continue
                records.append((
                    code, label, datetime.fromisoformat(ts),
                    _f(r.get("noncomm_positions_long_all")),
                    _f(r.get("noncomm_positions_short_all")),
                    _f(r.get("comm_positions_long_all")),
                    _f(r.get("comm_positions_short_all")),
                    _f(r.get("open_interest_all")),
                ))
            self._duck.executemany(
                "INSERT OR REPLACE INTO ts_cot "
                "(market_code, market, ts, noncomm_long, noncomm_short, "
                " comm_long, comm_short, open_interest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                records,
            )
            total += len(records)
        log.info("cot ingest: %s rows across %s markets", total, len(MARKETS))
        return total


def cot_summary(duck: DuckStore) -> list[dict]:
    """Latest print + 3y COT index + 13w trend per market (read-time compute)."""
    df = duck.fetchdf(
        "SELECT market_code, market, ts, noncomm_long - noncomm_short AS net, "
        "open_interest FROM ts_cot ORDER BY market_code, ts"
    )
    out: list[dict] = []
    if df is None or df.empty:
        return out
    for code, g in df.groupby("market_code"):
        net = g["net"]
        last = float(net.iloc[-1])
        lo, hi = float(net.min()), float(net.max())
        idx = 100 * (last - lo) / (hi - lo) if hi != lo else None
        prev_13w = float(net.iloc[-14]) if len(net) >= 14 else None
        out.append({
            "market_code": code,
            "market": g["market"].iloc[-1],
            "report_date": g["ts"].iloc[-1].strftime("%Y-%m-%d"),
            "net_noncommercial": last,
            "cot_index": round(idx, 1) if idx is not None else None,
            "net_13w_ago": prev_13w,
            "open_interest": float(g["open_interest"].iloc[-1])
            if g["open_interest"].iloc[-1] == g["open_interest"].iloc[-1] else None,
            "weeks": int(len(g)),
        })
    out.sort(key=lambda r: r["market"])
    return out
