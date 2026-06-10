"""Cookbook pipeline: ensure price legs -> recompute -> persist -> WS push.

The price-leg ensure reuses the cache-first yfinance path (prices.py), so each
run is a no-op once today's bars are stored — recomputing every 15 minutes
costs only DuckDB reads + pandas. One fragile leg never blocks the others or
the recompute itself (skip-on-fail, the standard ingestor contract).
"""

from __future__ import annotations

import asyncio
import logging

from app.corr.cards import PRICE_LEGS
from app.corr.engine import CookbookResult, compute_cookbook, persist_cookbook
from app.db.duck import DuckStore
from app.ingest.prices import ensure_daily_history
from app.ws.hub import ConnectionManager

log = logging.getLogger("market.corr")

CORR_TOPIC = "corr"


class CorrPipeline:
    def __init__(self, duck: DuckStore, hub: ConnectionManager) -> None:
        self._duck = duck
        self._hub = hub

    async def run(self) -> CookbookResult | None:
        for symbol, asset_class in PRICE_LEGS.items():
            try:
                await ensure_daily_history(None, self._duck, symbol, asset_class)
            except Exception as exc:
                log.warning("price leg %s failed: %s", symbol, exc)

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, compute_cookbook, self._duck)
        if result is None:
            log.info("cookbook compute skipped — no series stored yet")
            return None
        await loop.run_in_executor(None, persist_cookbook, self._duck, result)

        statuses = [c.status for c in result.cards]
        log.info(
            "cookbook recomputed: %s broken, %s watch, %s holds, %s no-data (regime %s)",
            statuses.count("broken"), statuses.count("watch"),
            statuses.count("holds"), statuses.count("no-data"), result.regime,
        )
        await self._hub.broadcast(
            CORR_TOPIC,
            {
                "type": "corr",
                "computed_at": result.computed_at,
                "regime": result.regime,
                "broken": statuses.count("broken"),
                "watch": statuses.count("watch"),
                "stress": result.stress.get("on", False),
            },
        )
        return result
