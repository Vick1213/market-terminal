"""Phase 16 §11 rank 6 — CFTC TFF (Traders in Financial Futures).

The legacy COT report (``app/edge/cot.py``) only splits a market into
"commercial" vs "non-commercial". For financial futures that lumps pension
funds, insurers and mutual funds in with hedge funds — exactly the split that
matters. The TFF report breaks the same contracts into three real-money
categories:

  * **Asset Manager / Institutional** — pensions, insurers, mutual funds: slow,
    benchmark-driven, "real money" positioning.
  * **Leveraged Funds** — hedge funds / CTAs: fast, tactical, momentum money.
  * **Dealer / Intermediary** — sell-side, mostly hedging client flow.

The actionable signal the legacy split hides is the **Asset Manager vs
Leveraged Money net-position divergence**: when real money and hedge funds sit
on opposite sides of ES, the 10Y or VIX, that gap leads regime turns. We store
the three net positions per contract; the divergence and its percentile are
derived at read time (``/api/cftc/tff``), exactly like the legacy COT index.

This is a *sibling* of the legacy COT pipeline, not a replacement — it pulls a
different Socrata dataset (``gpe5-46if``, the TFF futures-only report) and lands
scalar net-position series in ts_macro (source "cftc_tff"), so no new table is
needed. Same fail-soft contract as every other Phase 16 ingestor.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from app.db.duck import DuckStore
from app.ingest.http import HttpClient
from app.ingest.macro import MacroRow, _d

log = logging.getLogger("market.ingest.cftc_tff")

# TFF futures-only report (keyless Socrata). The legacy COT pipeline uses a
# different dataset (6dca-aqww); this one carries the AM / LM / dealer split.
SOCRATA_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"

# CFTC contract market code -> short ticker. Codes are stable identifiers (names
# drift); we keep the macro-relevant index, rates, vol and crypto contracts. The
# "+" codes are the *consolidated* index series (e-mini + micro rolled up), the
# truest read on total positioning.
CONTRACTS = {
    "13874+": "ES",       # S&P 500 consolidated
    "20974+": "NQ",       # Nasdaq-100 consolidated
    "239742": "RTY",      # Russell 2000 e-mini
    "020601": "UST30Y",   # UST bond (long end)
    "043602": "UST10Y",   # UST 10Y note
    "042601": "UST2Y",    # UST 2Y note
    "1170E1": "VIX",      # VIX futures
    "098662": "DXY",      # ICE US dollar index
    "133741": "BTC",      # CME bitcoin
    "146021": "ETH",      # CME ether
}

# ~3.3y of weekly prints — enough for a 3y percentile index on the divergence.
_LOOKBACK_DAYS = 365 * 3 + 60
_LIMIT = 5000  # 10 contracts x ~170 weeks fits comfortably under this.


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _net(r: dict, long_key: str, short_key: str) -> float | None:
    lo, sh = _f(r.get(long_key)), _f(r.get(short_key))
    if lo is None or sh is None:
        return None
    return lo - sh


def parse(rows: list[dict]) -> list[MacroRow]:
    """TFF Socrata rows -> AM / LM / dealer net-position series per contract."""
    out: list[MacroRow] = []
    for r in rows:
        code = (r.get("cftc_contract_market_code") or "").strip()
        ticker = CONTRACTS.get(code)
        if ticker is None:
            continue
        ts = (r.get("report_date_as_yyyy_mm_dd") or "")[:10]
        if not ts:
            continue
        day = _d(datetime.fromisoformat(ts).date())
        am = _net(r, "asset_mgr_positions_long", "asset_mgr_positions_short")
        lm = _net(r, "lev_money_positions_long", "lev_money_positions_short")
        de = _net(r, "dealer_positions_long_all", "dealer_positions_short_all")
        if am is not None:
            out.append(MacroRow(f"TFF_{ticker}_AM_NET", day, am, "cftc_tff"))
        if lm is not None:
            out.append(MacroRow(f"TFF_{ticker}_LM_NET", day, lm, "cftc_tff"))
        if de is not None:
            out.append(MacroRow(f"TFF_{ticker}_DEALER_NET", day, de, "cftc_tff"))
    return out


class CftcTffPipeline:
    """fetch the TFF report -> upsert AM/LM/dealer net positions into ts_macro."""

    def __init__(self, duck: DuckStore, http: HttpClient) -> None:
        self._duck = duck
        self._http = http

    async def _store(self, rows: list[MacroRow]) -> int:
        if not rows:
            return 0
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._duck.executemany,
            "INSERT OR REPLACE INTO ts_macro (series_id, ts, value, source) "
            "VALUES (?, ?, ?, ?)",
            [(r.series_id, r.ts, r.value, r.source) for r in rows],
        )
        return len(rows)

    async def run(self) -> None:
        codes = ",".join(f"'{c}'" for c in CONTRACTS)
        cutoff = (date.today() - timedelta(days=_LOOKBACK_DAYS)).isoformat()
        try:
            rows = await self._http.get_json(
                SOCRATA_URL,
                params={
                    "$where": (
                        f"cftc_contract_market_code in ({codes}) "
                        f"and report_date_as_yyyy_mm_dd>'{cutoff}'"
                    ),
                    "$order": "report_date_as_yyyy_mm_dd DESC",
                    "$limit": str(_LIMIT),
                },
            )
        except Exception as exc:
            log.warning("cftc tff fetch failed: %s", exc)
            return
        n = await self._store(parse(rows))
        log.info("cftc tff ingest: %s net-position points (%s contracts)", n, len(CONTRACTS))
