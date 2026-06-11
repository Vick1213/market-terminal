"""International macro via DBnomics (Phase 9 §4).

The plan named Econdb, but its API is hard Cloudflare-blocked for any
non-browser client (curl probe 2026-06-10: "Sorry, you have been blocked",
browser UA included). DBnomics (db.nomics.world) is the same kind of keyless
aggregator and serves the underlying OECD/Eurostat series directly, so it is
the source here.

Series choices were probed 2026-06-10 — picked for DEPTH + FRESHNESS, both of
which most mirrors fail (NBS China PMI keeps only 13 months; the ECB BSI M2
mirror ends 2024-12, so a "global M2 vs BTC" card would be permanently stale):

  * CN_CLI  — OECD composite leading indicator, China (1992+, monthly)
  * G20_CLI — OECD composite leading indicator, G20  (1961+, monthly)
  * EA_BCI  — OECD composite business confidence, euro area (1985+, monthly)

Each feeds exactly one new correlation card (corr/cards.py 13-15). Response
shape (verified): GET /v22/series/{provider}/{dataset}/{code}?observations=1
-> series.docs[0] with parallel ``period_start_day`` and ``value`` arrays.

Same contract as every ingestor: one series failing never blocks the others.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.db.duck import DuckStore
from app.ingest.http import HttpClient

log = logging.getLogger("market.ingest.intl")

DBNOMICS_SERIES = "https://api.db.nomics.world/v22/series/{provider}/{dataset}/{code}"

# series_id -> (provider, dataset, series code, label for logs)
INTL_SERIES: dict[str, tuple[str, str, str, str]] = {
    "CN_CLI": ("OECD", "DSD_STES@DF_CLI", "CHN.M.LI.IX._Z.AA.IX._Z.H",
               "China composite leading indicator"),
    "G20_CLI": ("OECD", "DSD_STES@DF_CLI", "G20.M.LI.IX._Z.AA.IX._Z.H",
                "G20 composite leading indicator"),
    "EA_BCI": ("OECD", "DSD_STES@DF_CLI", "EA20.M.BCICP.IX._Z.AA.IX._Z.H",
               "Euro-area business confidence"),
}


async def fetch_dbnomics(
    http: HttpClient, provider: str, dataset: str, code: str
) -> list[tuple[datetime, float]]:
    """One DBnomics series -> [(month-start timestamp, value)]."""
    data = await http.get_json(
        DBNOMICS_SERIES.format(provider=provider, dataset=dataset, code=code),
        params={"observations": "1"},
        conditional=False,
    )
    docs = data.get("series", {}).get("docs", [])
    if not docs:
        return []
    doc = docs[0]
    out: list[tuple[datetime, float]] = []
    for day, value in zip(doc.get("period_start_day", []), doc.get("value", [])):
        if value is None:
            continue
        try:
            out.append((datetime.fromisoformat(day), float(value)))
        except (TypeError, ValueError):
            continue
    return out


class IntlPipeline:
    def __init__(self, duck: DuckStore, http: HttpClient) -> None:
        self._duck = duck
        self._http = http

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        stored = 0
        for sid, (provider, dataset, code, label) in INTL_SERIES.items():
            try:
                rows = await fetch_dbnomics(self._http, provider, dataset, code)
            except Exception as exc:
                log.warning("dbnomics %s (%s) failed: %s", sid, label, exc)
                continue
            if not rows:
                log.warning("dbnomics %s (%s): no observations", sid, label)
                continue
            await loop.run_in_executor(
                None,
                self._duck.executemany,
                "INSERT OR REPLACE INTO ts_macro (series_id, ts, value, source) "
                "VALUES (?, ?, ?, 'dbnomics')",
                [(sid, ts, v) for ts, v in rows],
            )
            stored += len(rows)
            log.info("dbnomics %s: %s points (latest %s)", sid, len(rows), rows[-1][0].date())
        return stored
