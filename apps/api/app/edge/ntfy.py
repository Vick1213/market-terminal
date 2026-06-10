"""ntfy.sh push publisher (PLAN §6 #7).

The topic name is the only auth, so it must be long and random — and it is the
user's opt-in switch: empty topic means nothing ever leaves the machine and
alerts stay in-app (WS + panel). Publish failures are logged and swallowed;
push is fan-out, never a dependency of the alert path.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("market.edge.ntfy")

_PRIORITY = {"info": "default", "warn": "high", "critical": "urgent"}


class NtfyPublisher:
    def __init__(self, server: str, topic: str) -> None:
        self._url = f"{server.rstrip('/')}/{topic}" if topic else ""
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def publish(self, title: str, body: str, severity: str = "info",
                      tags: str = "chart_with_upwards_trend") -> bool:
        if not self._url:
            return False
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        try:
            resp = await self._client.post(
                self._url,
                content=body.encode(),
                headers={
                    "Title": title.encode("ascii", "ignore").decode(),
                    "Priority": _PRIORITY.get(severity, "default"),
                    "Tags": tags,
                },
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            log.warning("ntfy publish failed: %s", exc)
            return False

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
