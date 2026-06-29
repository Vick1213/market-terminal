"""Shared, TTL-cached view of the paper broker — the main IP-block guard.

Both bots, the optimizer, and the /api/bot/status endpoint all read the same
account / positions / clock / open-orders. Without coordination a burst of UI
polls plus two bot ticks would fire a dozen paper-api calls a second. This wraps
the broker so all those reads inside a short window collapse to ONE call.

Reads are cached for ``ttl`` seconds (per-key, with a lock so concurrent callers
don't stampede). Writes go straight through and then invalidate the cache so the
next read reflects the new state. ``fresh=True`` bypasses the cache when a caller
genuinely needs current state (e.g. order re-validation right before submit).
"""

from __future__ import annotations

import asyncio
import time

from app.trading.broker import AlpacaPaperBroker


class BrokerState:
    def __init__(self, broker: AlpacaPaperBroker, ttl: float = 4.0) -> None:
        self._broker = broker
        self._ttl = ttl
        self._cache: dict[str, tuple[object, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # passthrough identity / safety flags
    @property
    def enabled(self) -> bool:
        return self._broker.enabled

    @property
    def is_paper(self) -> bool:
        return self._broker.is_paper

    @property
    def base_url(self) -> str:
        return self._broker.base_url

    @property
    def raw(self) -> AlpacaPaperBroker:
        return self._broker

    def _lock(self, key: str) -> asyncio.Lock:
        lk = self._locks.get(key)
        if lk is None:
            lk = self._locks[key] = asyncio.Lock()
        return lk

    async def _get(self, key: str, fetch, fresh: bool = False):
        if not fresh:
            ent = self._cache.get(key)
            if ent is not None and (time.monotonic() - ent[1]) < self._ttl:
                return ent[0]
        async with self._lock(key):
            ent = self._cache.get(key)
            if not fresh and ent is not None and (time.monotonic() - ent[1]) < self._ttl:
                return ent[0]
            val = await fetch()
            self._cache[key] = (val, time.monotonic())
            return val

    # cached reads
    async def account(self, fresh: bool = False) -> dict:
        return await self._get("account", self._broker.get_account, fresh)

    async def positions(self, fresh: bool = False) -> list[dict]:
        return await self._get("positions", self._broker.get_positions, fresh)

    async def clock(self, fresh: bool = False) -> dict:
        return await self._get("clock", self._broker.get_clock, fresh)

    async def open_orders(self, fresh: bool = False) -> list[dict]:
        return await self._get("open_orders", lambda: self._broker.list_orders("open", 200), fresh)

    async def is_market_open(self) -> bool:
        """True when the US equity market is open (cached). Defaults to True on
        a broker error so a transient hiccup doesn't silently freeze the bot."""
        try:
            return bool((await self.clock()).get("is_open"))
        except Exception:
            return True

    # uncached read (full history) — callers throttle this themselves
    async def list_orders(self, status: str = "all", limit: int = 200) -> list[dict]:
        return await self._broker.list_orders(status, limit)

    async def fractionable(self, symbol: str) -> bool:
        """Whether the asset supports fractional / notional orders. Cached for the
        process lifetime (the flag never changes). Unknown / error -> True so the
        normal order path still runs and surfaces any genuine rejection."""
        cache = getattr(self, "_frac", None)
        if cache is None:
            cache = self._frac = {}
        key = (symbol or "").upper()
        if key in cache:
            return cache[key]
        try:
            asset = await self._broker.get_asset(symbol)
            frac = bool(asset.get("fractionable", True))
        except Exception:
            frac = True
        cache[key] = frac
        return frac

    # writes: pass through, then drop the cache so the next read is fresh
    async def submit_order(self, *args, **kwargs) -> dict:
        try:
            return await self._broker.submit_order(*args, **kwargs)
        finally:
            self.invalidate()

    async def cancel_all(self) -> int:
        try:
            return await self._broker.cancel_all()
        finally:
            self.invalidate()

    def invalidate(self) -> None:
        self._cache.clear()
