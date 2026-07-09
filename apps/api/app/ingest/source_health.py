"""Source-health watchdog — Phase 11 #1 (PLAN §10).

Every outbound fetch already flows through ``HttpClient``; this registry is
its black box recorder. One SQLite row per host: last success/failure, the
consecutive-failure streak, and lifetime counters. The streak — not a single
failure — is the signal: free sources 404 on weekends and 429 under load, but
only a dead one fails five times in a row.

Recording must never break a fetch: every write is wrapped, errors are logged
and swallowed. Reads (the /api/health/sources snapshot and the alert rule)
interpret the rows; nothing here makes a network request.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.db.sqlite import SqliteStore

log = logging.getLogger("market.ingest.source_health")

# Streak thresholds shared by the snapshot and the alert rule.
DEGRADED_AFTER = 2
DEAD_AFTER = 5

# host -> human label + what breaks when it dies. Hosts not listed still get
# tracked; they just render with the bare hostname.
SOURCE_LABELS: dict[str, tuple[str, str]] = {
    "api.stlouisfed.org": ("FRED", "macro/yields/liquidity series"),
    "data.sec.gov": ("SEC EDGAR (data)", "filings, Form 4, 13F, submissions"),
    "www.sec.gov": ("SEC EDGAR (archives)", "filing documents"),
    "efts.sec.gov": ("SEC EDGAR (search)", "full-text filing search"),
    "publicreporting.cftc.gov": ("CFTC", "weekly COT positioning"),
    "cdn.cboe.com": ("CBOE CDN", "VIX term structure, put/call, GEX options"),
    "cdn.finra.org": ("FINRA CDN", "daily short-sale volume"),
    "api.finra.org": ("FINRA Query API", "bi-monthly true short interest"),
    "financialmodelingprep.com": ("FMP", "market-wide earnings calendar"),
    "stooq.com": ("Stooq", "EOD price history (breadth, cookbook legs)"),
    "feeds.finance.yahoo.com": ("Yahoo RSS", "per-ticker news"),
    "finnhub.io": ("Finnhub", "per-ticker news fallback"),
    "api.gdeltproject.org": ("GDELT", "broad-market news firehose"),
    "apewisdom.io": ("ApeWisdom", "Reddit mention volume"),
    "api.stocktwits.com": ("StockTwits", "bull/bear tagged messages"),
    "api.bsky.app": ("Bluesky", "cashtag social firehose"),
    "api.gold-api.com": ("gold-api.com", "gold/silver spot"),
    "api.coingecko.com": ("CoinGecko", "crypto aggregates"),
    "api.coinpaprika.com": ("CoinPaprika", "crypto aggregates (backup)"),
    "api.fiscaldata.treasury.gov": ("Treasury FiscalData", "daily TGA (net liquidity)"),
    "markets.newyorkfed.org": ("NY Fed", "RRP (net liquidity)"),
    "efdsearch.senate.gov": ("Senate eFD", "congressional PTRs"),
    "api.db.nomics.world": ("DBnomics", "intl macro (CLI/BCI cards)"),
    "www.federalreserve.gov": ("Federal Reserve", "FOMC statements/calendar"),
    "naaim.org": ("NAAIM", "manager exposure index"),
    "data.alpaca.markets": ("Alpaca", "live watchlist quotes, daily-bar fallback"),
    "query1.finance.yahoo.com": ("Yahoo quotes", "intraday prices, earnings dates"),
    "query2.finance.yahoo.com": ("Yahoo quotes", "intraday prices, earnings dates"),
    "api.tiingo.com": ("Tiingo", "daily-bar price history (BYO-key, M2.5 yfinance replacement)"),
}


class SourceHealthRegistry:
    def __init__(self, sqlite: SqliteStore) -> None:
        self._sqlite = sqlite

    def record_success(self, host: str) -> None:
        self._record(host, ok=True, error=None)

    def record_failure(self, host: str, error: str) -> None:
        self._record(host, ok=False, error=error[:300])

    def record_disabled(self, host: str, status: str, detail: str | None = None) -> None:
        """Manually mark `host` as disabled (M2.5 profile gate / missing key)
        rather than via a real fetch outcome. ``snapshot()`` reports `status`
        verbatim instead of computing ok/degraded/dead from the failure
        streak. A later real ``record_success``/``record_failure`` for the
        same host clears this (the real signal always wins)."""
        if not host:
            return
        try:
            self._sqlite.execute(
                """
                INSERT INTO source_health (host, disabled_status, disabled_detail)
                VALUES (?, ?, ?)
                ON CONFLICT(host) DO UPDATE SET
                    disabled_status = excluded.disabled_status,
                    disabled_detail = excluded.disabled_detail
                """,
                [host, status, detail],
            )
        except Exception as exc:  # never let bookkeeping break startup
            log.warning("source_health record_disabled failed for %s: %s", host, exc)

    def _record(self, host: str, *, ok: bool, error: str | None) -> None:
        if not host:
            return
        now = datetime.now(timezone.utc).isoformat()
        try:
            if ok:
                self._sqlite.execute(
                    """
                    INSERT INTO source_health
                        (host, last_success, consecutive_failures, success_count, failure_count)
                    VALUES (?, ?, 0, 1, 0)
                    ON CONFLICT(host) DO UPDATE SET
                        last_success = excluded.last_success,
                        consecutive_failures = 0,
                        success_count = success_count + 1,
                        disabled_status = NULL,
                        disabled_detail = NULL
                    """,
                    [host, now],
                )
            else:
                self._sqlite.execute(
                    """
                    INSERT INTO source_health
                        (host, last_failure, last_error, consecutive_failures,
                         success_count, failure_count)
                    VALUES (?, ?, ?, 1, 0, 1)
                    ON CONFLICT(host) DO UPDATE SET
                        last_failure = excluded.last_failure,
                        last_error = excluded.last_error,
                        consecutive_failures = consecutive_failures + 1,
                        failure_count = failure_count + 1,
                        disabled_status = NULL,
                        disabled_detail = NULL
                    """,
                    [host, now, error],
                )
        except Exception as exc:  # never let bookkeeping break a fetch
            log.warning("source_health record failed for %s: %s", host, exc)

    def snapshot(self) -> list[dict]:
        rows = self._sqlite.fetchall(
            "SELECT host, last_success, last_failure, last_error, "
            "consecutive_failures, success_count, failure_count, "
            "disabled_status, disabled_detail "
            "FROM source_health ORDER BY consecutive_failures DESC, host"
        )
        out: list[dict] = []
        for r in rows:
            disabled_status = r["disabled_status"]
            if disabled_status:
                status = disabled_status
            else:
                streak = int(r["consecutive_failures"])
                if streak >= DEAD_AFTER:
                    status = "dead"
                elif streak >= DEGRADED_AFTER:
                    status = "degraded"
                else:
                    status = "ok"
            label, covers = SOURCE_LABELS.get(r["host"], (r["host"], ""))
            out.append({
                "host": r["host"],
                "label": label,
                "covers": covers,
                "status": status,
                "disabled_detail": r["disabled_detail"],
                "last_success": r["last_success"],
                "last_failure": r["last_failure"],
                "last_error": r["last_error"],
                "consecutive_failures": int(r["consecutive_failures"]),
                "success_count": int(r["success_count"]),
                "failure_count": int(r["failure_count"]),
            })
        return out
