"""Topic-based WebSocket fan-out.

Panels subscribe to topics (``heartbeat``, ``news``, ``trades`` ...). Ingestors
and the scheduler publish to a topic and every subscribed client receives the
message. Dead sockets are pruned on send. A module-level singleton ``hub`` is
shared across the app.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._topics: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, topic: str) -> None:
        await ws.accept()
        async with self._lock:
            self._topics.setdefault(topic, set()).add(ws)

    async def disconnect(self, ws: WebSocket, topic: str) -> None:
        async with self._lock:
            conns = self._topics.get(topic)
            if conns:
                conns.discard(ws)
                if not conns:
                    self._topics.pop(topic, None)

    async def broadcast(self, topic: str, message: Any) -> None:
        async with self._lock:
            targets = list(self._topics.get(topic, set()))
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                conns = self._topics.get(topic)
                if conns:
                    for ws in dead:
                        conns.discard(ws)

    def client_count(self) -> int:
        return sum(len(s) for s in self._topics.values())

    def topics(self) -> dict[str, int]:
        return {t: len(s) for t, s in self._topics.items()}


# Shared singleton.
hub = ConnectionManager()
