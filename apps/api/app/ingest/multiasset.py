"""Phase 4 polled ingestors — metals spot, crypto aggregates, per-ticker FINRA.

The streaming leg of Panel (e) lives in ``crypto.py``; everything here is the
honest delayed/aggregated remainder, on APScheduler cadences:
  * gold-api.com XAU/XAG spot (keyless, unlimited) -> ts_price every ~60s,
  * CoinPaprika /global (keyless backup of CoinGecko) -> BTC dominance +
    24h volume into ts_macro,
  * FINRA CNMSshvol per-ticker short-sale volume for watchlist equities ->
    ts_short_vol (the accumulation-score input; short SELL volume, NOT short
    interest).

Same contract as the other pipelines: every fetcher is allowed to fail alone.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ingest.http import HttpClient
from app.ingest.macro import FINRA_URL, _d

log = logging.getLogger("market.ingest.multiasset")

GOLD_API_URL = "https://api.gold-api.com/price/{sym}"
METAL_SYMBOLS = ("XAU", "XAG")

COINPAPRIKA_GLOBAL = "https://api.coinpaprika.com/v1/global"

FINRA_TICKER_LOOKBACK_DAYS = 45  # enough history for a 20d ratio baseline


class MultiAssetPipeline:
    def __init__(self, duck: DuckStore, sqlite: SqliteStore, http: HttpClient) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._http = http

    # --- metals spot (every ~60s) ------------------------------------------

    async def run_metals(self) -> None:
        rows = []
        for sym in METAL_SYMBOLS:
            try:
                data = await self._http.get_json(GOLD_API_URL.format(sym=sym), conditional=False)
                price = float(data["price"])
            except Exception as exc:
                log.warning("gold-api %s failed: %s", sym, exc)
                continue
            ts = datetime.now(timezone.utc).replace(second=0, microsecond=0, tzinfo=None)
            rows.append(("gold-api", sym, "metal", ts, None, None, None, price, None))
        if not rows:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._duck.executemany,
            "INSERT OR REPLACE INTO ts_price "
            "(source, symbol, asset_class, ts, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    # --- CoinPaprika global aggregates (daily-ish) ---------------------------

    async def run_paprika(self) -> None:
        try:
            data = await self._http.get_json(COINPAPRIKA_GLOBAL, conditional=False)
        except Exception as exc:
            log.warning("coinpaprika global failed: %s", exc)
            return
        today = _d(date.today())
        rows = []
        for sid, key in (
            ("BTC_DOMINANCE", "bitcoin_dominance_percentage"),
            ("CRYPTO_VOL24H", "volume_24h_usd"),
            ("CRYPTO_MCAP", "market_cap_usd"),
        ):
            v = data.get(key)
            if v is not None:
                rows.append((sid, today, float(v), "coinpaprika"))
        if not rows:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._duck.executemany,
            "INSERT OR REPLACE INTO ts_macro (series_id, ts, value, source) VALUES (?, ?, ?, ?)",
            rows,
        )
        log.info("coinpaprika: %s aggregates stored", len(rows))

    # --- FINRA per-ticker short volume (daily, T+1) ---------------------------

    def _watchlist_equities(self) -> set[str]:
        return {
            r["symbol"]
            for r in self._sqlite.fetchall(
                "SELECT symbol FROM watchlist WHERE asset_class = 'equity'"
            )
        }

    def _missing_short_vol_days(self, lookback_days: int) -> list[date]:
        """Recent business days with no per-ticker rows stored yet."""
        have = {
            r[0].date()
            for r in self._duck.fetchall(
                "SELECT DISTINCT ts FROM ts_short_vol WHERE ts >= ?",
                [_d(date.today() - timedelta(days=lookback_days))],
            )
        }
        days = []
        for i in range(1, lookback_days + 1):  # today's file appears T+1
            day = date.today() - timedelta(days=i)
            if day.weekday() < 5 and day not in have:
                days.append(day)
        return days

    async def run_short_vol(self) -> None:
        loop = asyncio.get_running_loop()
        symbols = await loop.run_in_executor(None, self._watchlist_equities)
        if not symbols:
            return
        days = await loop.run_in_executor(
            None, self._missing_short_vol_days, FINRA_TICKER_LOOKBACK_DAYS
        )
        total = 0
        for day in days:
            url = FINRA_URL.format(ymd=day.strftime("%Y%m%d"))
            try:
                text = await self._http.get_text(url, conditional=False)
            except Exception as exc:
                log.debug("finra %s unavailable: %s", day, exc)  # weekend/holiday
                continue
            rows = []
            for line in text.splitlines()[1:]:  # Date|Symbol|ShortVolume|ShortExempt|Total|Mkt
                parts = line.split("|")
                if len(parts) < 5 or parts[1] not in symbols:
                    continue
                try:
                    rows.append((parts[1], _d(day), float(parts[2]), float(parts[4])))
                except ValueError:
                    continue
            if rows:
                await loop.run_in_executor(
                    None,
                    self._duck.executemany,
                    "INSERT OR REPLACE INTO ts_short_vol (symbol, ts, short_volume, total_volume) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
                total += len(rows)
        log.info("finra per-ticker: %s rows (%s candidate days)", total, len(days))
