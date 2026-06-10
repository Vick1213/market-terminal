"""CCXT Pro live crypto streams — Panel (e)'s genuinely real-time leg.

Long-running asyncio tasks (started in the app lifespan, NOT APScheduler):
  * ``watch_trades`` per (exchange, symbol) — every print updates an in-memory
    quote; prints above a notional threshold OR a rolling size z-score are
    "large", broadcast immediately on WS topic ``trades`` and batch-persisted
    into the DuckDB ``trades`` table (the full firehose is never stored).
  * ``watch_order_book`` per (exchange, symbol) — top-of-book depth imbalance
    recomputed continuously, broadcast on the same topic at a throttled cadence.

Public endpoints only, no keys. Each loop reconnects with exponential backoff
so one exchange outage never kills the panel; stream health is exposed for the
REST snapshot's LIVE badge.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.duck import DuckStore
from app.ws.hub import ConnectionManager

log = logging.getLogger("market.ingest.crypto")

TRADES_TOPIC = "trades"

BOOK_DEPTH_LEVELS = 10      # levels per side in the imbalance calc
BOOK_EMIT_SECONDS = 2.0     # min seconds between depth broadcasts per stream
QUOTE_EMIT_SECONDS = 2.0    # cadence of the consolidated quote broadcast
FLUSH_SECONDS = 5.0         # large-print DB flush cadence
PRUNE_EVERY_FLUSHES = 720   # ~1h between retention deletes
STATS_WINDOW = 500          # rolling trade-size window per stream
LIVE_STALE_SECONDS = 30     # quote older than this => stream not "live"


class _RollingStats:
    """Rolling mean/std of trade notionals over the last N prints."""

    def __init__(self, window: int = STATS_WINDOW) -> None:
        self._values: deque[float] = deque(maxlen=window)
        self._sum = 0.0
        self._sum_sq = 0.0

    def push(self, v: float) -> None:
        if len(self._values) == self._values.maxlen:
            old = self._values[0]
            self._sum -= old
            self._sum_sq -= old * old
        self._values.append(v)
        self._sum += v
        self._sum_sq += v * v

    def z(self, v: float) -> float | None:
        n = len(self._values)
        if n < 30:  # not enough tape yet for a meaningful z
            return None
        mean = self._sum / n
        var = max(self._sum_sq / n - mean * mean, 0.0)
        std = math.sqrt(var)
        if std <= 0:
            return None
        return (v - mean) / std


class CryptoStreamer:
    def __init__(
        self,
        duck: DuckStore,
        hub: ConnectionManager,
        *,
        exchanges: list[str],
        symbols: list[str],
        large_notional: float = 250_000,
        large_z: float = 4.0,
        large_floor: float = 25_000,
        retention_days: int = 14,
    ) -> None:
        self._duck = duck
        self._hub = hub
        self._exchange_ids = exchanges
        self._symbols = symbols
        self._large_notional = large_notional
        self._large_z = large_z
        self._large_floor = large_floor
        self._retention_days = retention_days

        self._tasks: list[asyncio.Task] = []
        self._exchanges: dict[str, Any] = {}
        self._stats: dict[tuple[str, str], _RollingStats] = {}
        # Latest trade per stream: {exchange, symbol, price, ts, trades_seen, connected}
        self._quotes: dict[tuple[str, str], dict[str, Any]] = {}
        # Latest depth snapshot per stream.
        self._books: dict[tuple[str, str], dict[str, Any]] = {}
        self._book_emitted: dict[tuple[str, str], float] = {}
        self._pending: list[tuple] = []  # large prints awaiting DB flush
        self._flush_count = 0

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        try:
            import ccxt.pro as ccxtpro  # heavy; imported once here
        except Exception as exc:  # pragma: no cover - missing optional dep
            log.error("ccxt.pro unavailable — crypto streams disabled: %s", exc)
            return

        for ex_id in self._exchange_ids:
            klass = getattr(ccxtpro, ex_id, None)
            if klass is None:
                log.warning("unknown ccxt exchange %r — skipped", ex_id)
                continue
            self._exchanges[ex_id] = klass({"enableRateLimit": True})

        for ex_id in self._exchanges:
            for symbol in self._symbols:
                key = (ex_id, symbol)
                self._stats[key] = _RollingStats()
                self._quotes[key] = {
                    "exchange": ex_id, "symbol": symbol, "price": None,
                    "ts": None, "trades_seen": 0, "connected": False,
                }
                self._tasks.append(asyncio.create_task(
                    self._trade_loop(ex_id, symbol), name=f"trades:{ex_id}:{symbol}"
                ))
                self._tasks.append(asyncio.create_task(
                    self._book_loop(ex_id, symbol), name=f"book:{ex_id}:{symbol}"
                ))
        if self._tasks:
            self._tasks.append(asyncio.create_task(self._flush_loop(), name="trades:flush"))
            self._tasks.append(asyncio.create_task(self._quote_loop(), name="trades:quotes"))
        log.info(
            "crypto streams started: %s x %s (%s tasks)",
            list(self._exchanges), self._symbols, len(self._tasks),
        )

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for ex in self._exchanges.values():
            try:
                await ex.close()
            except Exception:
                pass
        await self._flush()  # persist any tail prints

    # --- snapshot accessors (REST router reads these) ----------------------

    def quotes(self) -> list[dict[str, Any]]:
        return [dict(q) for q in self._quotes.values()]

    def books(self) -> list[dict[str, Any]]:
        return [dict(b) for b in self._books.values()]

    def is_live(self) -> bool:
        """Any stream with a trade in the last LIVE_STALE_SECONDS."""
        now = datetime.now(timezone.utc)
        for q in self._quotes.values():
            ts = q.get("ts")
            if ts and (now - ts).total_seconds() < LIVE_STALE_SECONDS:
                return True
        return False

    # --- stream loops -------------------------------------------------------

    async def _trade_loop(self, ex_id: str, symbol: str) -> None:
        ex = self._exchanges[ex_id]
        key = (ex_id, symbol)
        backoff = 1.0
        while True:
            try:
                trades = await ex.watch_trades(symbol)
                backoff = 1.0
                self._quotes[key]["connected"] = True
                for t in trades:
                    await self._on_trade(key, t)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._quotes[key]["connected"] = False
                log.warning("watch_trades %s %s: %s (retry in %.0fs)", ex_id, symbol, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _on_trade(self, key: tuple[str, str], t: dict) -> None:
        ex_id, symbol = key
        price = t.get("price")
        amount = t.get("amount")
        if not price or not amount:
            return
        notional = float(price) * float(amount)
        ts = (
            datetime.fromtimestamp(t["timestamp"] / 1000, tz=timezone.utc)
            if t.get("timestamp")
            else datetime.now(timezone.utc)
        )

        q = self._quotes[key]
        q["price"] = float(price)
        q["ts"] = ts
        q["trades_seen"] += 1

        stats = self._stats[key]
        z = stats.z(notional)
        stats.push(notional)

        large = notional >= self._large_notional or (
            z is not None and z >= self._large_z and notional >= self._large_floor
        )
        if not large:
            return

        side = t.get("side") or ""
        self._pending.append(
            (ex_id, symbol, "crypto", ts.replace(tzinfo=None), float(price),
             float(amount), side, notional)
        )
        await self._hub.broadcast(TRADES_TOPIC, {
            "type": "trade",
            "exchange": ex_id,
            "symbol": symbol,
            "ts": ts.isoformat(),
            "price": float(price),
            "size": float(amount),
            "side": side,
            "notional": notional,
            "z": round(z, 2) if z is not None else None,
        })

    async def _book_loop(self, ex_id: str, symbol: str) -> None:
        ex = self._exchanges[ex_id]
        key = (ex_id, symbol)
        backoff = 1.0
        loop = asyncio.get_running_loop()
        while True:
            try:
                ob = await ex.watch_order_book(symbol)
                backoff = 1.0
                bids = ob.get("bids") or []
                asks = ob.get("asks") or []
                if not bids or not asks:
                    continue
                bid_depth = sum(b[1] for b in bids[:BOOK_DEPTH_LEVELS])
                ask_depth = sum(a[1] for a in asks[:BOOK_DEPTH_LEVELS])
                total = bid_depth + ask_depth
                if total <= 0:
                    continue
                mid = (bids[0][0] + asks[0][0]) / 2
                snap = {
                    "exchange": ex_id,
                    "symbol": symbol,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "imbalance": (bid_depth - ask_depth) / total,
                    "bid_depth": bid_depth,
                    "ask_depth": ask_depth,
                    "mid": mid,
                    "spread_bp": (asks[0][0] - bids[0][0]) / mid * 10_000,
                }
                self._books[key] = snap
                now = loop.time()
                if now - self._book_emitted.get(key, 0.0) >= BOOK_EMIT_SECONDS:
                    self._book_emitted[key] = now
                    await self._hub.broadcast(TRADES_TOPIC, {"type": "depth", **snap})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("watch_order_book %s %s: %s (retry in %.0fs)", ex_id, symbol, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # --- periodic broadcasters / persistence --------------------------------

    async def _quote_loop(self) -> None:
        """Consolidated last-price tick so the panel moves between large prints."""
        while True:
            await asyncio.sleep(QUOTE_EMIT_SECONDS)
            quotes = [
                {**q, "ts": q["ts"].isoformat() if q["ts"] else None}
                for q in self._quotes.values()
                if q["price"] is not None
            ]
            if quotes:
                await self._hub.broadcast(TRADES_TOPIC, {"type": "quote", "quotes": quotes})

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            await self._flush()

    async def _flush(self) -> None:
        if not self._pending:
            return
        rows, self._pending = self._pending, []
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                self._duck.executemany,
                "INSERT INTO trades (source, symbol, asset_class, ts, price, size, side, notional) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        except Exception as exc:
            log.warning("trade flush failed (%s rows dropped): %s", len(rows), exc)
            return
        self._flush_count += 1
        if self._flush_count % PRUNE_EVERY_FLUSHES == 1:  # boot + hourly thereafter
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                days=self._retention_days
            )
            await loop.run_in_executor(
                None, self._duck.execute, "DELETE FROM trades WHERE ts < ?", [cutoff]
            )
