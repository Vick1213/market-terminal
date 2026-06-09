"""The single most important reliability investment in the whole backend.

Every outbound request goes through one ``HttpClient`` that provides:
  * a global descriptive User-Agent (SEC EDGAR requires one),
  * per-host token-bucket rate limiting (aiolimiter) so we never hammer a free
    source into a ban,
  * exponential-backoff retries on 429 / 5xx / transport errors (tenacity),
  * conditional GET (ETag / If-Modified-Since) backed by diskcache, so unchanged
    RSS/JSON is not re-downloaded and a 304 returns the cached body for free.

Async-first; blocking parsing should still be offloaded by callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

import diskcache
import httpx
from aiolimiter import AsyncLimiter
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class RetryableStatus(Exception):
    """Raised for 429/5xx so tenacity retries; carries the response."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"retryable status {response.status_code} for {response.request.url}")


@dataclass
class HttpResponse:
    url: str
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        import json

        return json.loads(self.body)


# Per-host default rate limits (requests per `period` seconds). Sources with
# documented hard limits (GDELT 1/5s, SEC ≤10/s) get tightened by their
# ingestors; this is the polite default for everything else.
_DEFAULT_RATE = (3, 1.0)  # 3 requests/second
_HOST_RATES: dict[str, tuple[int, float]] = {
    "api.gdeltproject.org": (1, 5.0),
    "data.sec.gov": (8, 1.0),
    "efts.sec.gov": (8, 1.0),
    "stooq.com": (1, 2.0),
    "finnhub.io": (50, 60.0),
}


class HttpClient:
    def __init__(self, user_agent: str, cache_dir: str, default_timeout: float = 20.0) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=default_timeout,
            follow_redirects=True,
        )
        self._cache = diskcache.Cache(cache_dir)
        self._limiters: dict[str, AsyncLimiter] = {}

    def _limiter(self, host: str) -> AsyncLimiter:
        if host not in self._limiters:
            rate, period = _HOST_RATES.get(host, _DEFAULT_RATE)
            self._limiters[host] = AsyncLimiter(rate, period)
        return self._limiters[host]

    @retry(
        retry=retry_if_exception_type((RetryableStatus, httpx.TransportError)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=20),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict | None = None,
        conditional: bool = True,
    ) -> HttpResponse:
        host = urlsplit(url).netloc
        req_headers = dict(headers or {})

        cached = self._cache.get(url) if conditional else None
        if cached:
            if cached.get("etag"):
                req_headers.setdefault("If-None-Match", cached["etag"])
            if cached.get("last_modified"):
                req_headers.setdefault("If-Modified-Since", cached["last_modified"])

        async with self._limiter(host):
            resp = await self._client.get(url, headers=req_headers, params=params)

        if resp.status_code == 304 and cached:
            return HttpResponse(
                url=url,
                status=200,
                body=cached["body"],
                headers=cached.get("headers", {}),
                from_cache=True,
            )

        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableStatus(resp)

        resp.raise_for_status()

        if conditional and (resp.headers.get("ETag") or resp.headers.get("Last-Modified")):
            self._cache.set(
                url,
                {
                    "etag": resp.headers.get("ETag"),
                    "last_modified": resp.headers.get("Last-Modified"),
                    "body": resp.content,
                    "headers": dict(resp.headers),
                },
            )

        return HttpResponse(
            url=url,
            status=resp.status_code,
            body=resp.content,
            headers=dict(resp.headers),
        )

    async def get_json(self, url: str, **kw):
        return (await self.get(url, **kw)).json()

    async def get_text(self, url: str, **kw) -> str:
        return (await self.get(url, **kw)).text

    async def aclose(self) -> None:
        await self._client.aclose()
        self._cache.close()
