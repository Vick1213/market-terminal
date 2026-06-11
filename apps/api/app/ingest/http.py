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
from urllib.parse import urlencode, urlsplit

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
    "www.sec.gov": (8, 1.0),
    "publicreporting.cftc.gov": (2, 1.0),
    "stooq.com": (1, 2.0),
    "finnhub.io": (50, 60.0),
    "api.stlouisfed.org": (100, 60.0),
    "fred.stlouisfed.org": (1, 1.0),
    "cdn.cboe.com": (2, 1.0),
    "www.cboe.com": (1, 2.0),
    "cdn.finra.org": (2, 1.0),
    "api.finra.org": (2, 1.0),
    "financialmodelingprep.com": (1, 2.0),  # free tier ~250 req/day
    "naaim.org": (1, 2.0),
    "www.aaii.com": (1, 2.0),
    "apewisdom.io": (1, 2.0),
    "api.stocktwits.com": (1, 3.0),
    "api.bsky.app": (3, 1.0),
    "tradestie.com": (1, 5.0),
    "api.fiscaldata.treasury.gov": (2, 1.0),
    "markets.newyorkfed.org": (2, 1.0),
    "efdsearch.senate.gov": (1, 2.0),
    "api.db.nomics.world": (2, 1.0),
    "www.federalreserve.gov": (1, 2.0),
    "data.alpaca.markets": (150, 60.0),  # free tier allows 200/min
}


# Hosts whose failures arrive as HTTP 200 with a poison body (PLAN §8): Stooq
# returns plain-text "Exceeded the daily hits limit" on quota and an HTML JS
# challenge to headless clients — both fatal for a CSV consumer.
_BODY_FAILURE_PREFIXES: dict[str, tuple[bytes, ...]] = {
    "stooq.com": (b"Exceeded", b"<"),
}


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        cache_dir: str,
        default_timeout: float = 20.0,
        health=None,  # SourceHealthRegistry | None — duck-typed, optional
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=default_timeout,
            follow_redirects=True,
        )
        self._cache = diskcache.Cache(cache_dir)
        self._limiters: dict[str, AsyncLimiter] = {}
        self._health = health

    def _record(self, host: str, resp: HttpResponse | None, exc: Exception | None) -> None:
        """Final outcome (after retries) -> watchdog. A 304-from-cache is a
        success (the source answered); a poison 200 body is a failure."""
        if self._health is None:
            return
        if exc is not None:
            self._health.record_failure(host, str(exc))
            return
        if resp is not None and not resp.from_cache:
            for prefix in _BODY_FAILURE_PREFIXES.get(host, ()):
                if resp.body.lstrip()[:32].startswith(prefix):
                    self._health.record_failure(
                        host, f"poison body: {resp.body.lstrip()[:60]!r}"
                    )
                    return
        self._health.record_success(host)

    def _limiter(self, host: str) -> AsyncLimiter:
        if host not in self._limiters:
            rate, period = _HOST_RATES.get(host, _DEFAULT_RATE)
            self._limiters[host] = AsyncLimiter(rate, period)
        return self._limiters[host]

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict | None = None,
        conditional: bool = True,
    ) -> HttpResponse:
        host = urlsplit(url).netloc
        try:
            resp = await self._get_with_retry(
                url, headers=headers, params=params, conditional=conditional
            )
        except Exception as exc:
            self._record(host, None, exc)
            raise
        self._record(host, resp, None)
        return resp

    @retry(
        retry=retry_if_exception_type((RetryableStatus, httpx.TransportError)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=20),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _get_with_retry(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict | None = None,
        conditional: bool = True,
    ) -> HttpResponse:
        host = urlsplit(url).netloc
        req_headers = dict(headers or {})

        # Cache key must include query params — two requests to the same path
        # with different params are different resources (FRED, Socrata, ...).
        cache_key = f"{url}?{urlencode(sorted(params.items()))}" if params else url

        cached = self._cache.get(cache_key) if conditional else None
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
                cache_key,
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

    async def post(
        self,
        url: str,
        *,
        data: dict | None = None,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        host = urlsplit(url).netloc
        try:
            resp = await self._post_with_retry(
                url, data=data, json_body=json_body, headers=headers
            )
        except Exception as exc:
            self._record(host, None, exc)
            raise
        self._record(host, resp, None)
        return resp

    @retry(
        retry=retry_if_exception_type((RetryableStatus, httpx.TransportError)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=20),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _post_with_retry(
        self,
        url: str,
        *,
        data: dict | None = None,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        """Rate-limited POST, form (``data``) or JSON (``json_body``) — no
        conditional caching, POSTs aren't cacheable. Session cookies set by
        prior requests ride along on the shared client."""
        host = urlsplit(url).netloc
        async with self._limiter(host):
            resp = await self._client.post(
                url, data=data, json=json_body, headers=headers or {}
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableStatus(resp)
        resp.raise_for_status()
        return HttpResponse(
            url=url, status=resp.status_code, body=resp.content, headers=dict(resp.headers)
        )

    def cookie(self, name: str) -> str | None:
        """A cookie from the shared jar (e.g. the eFD csrftoken)."""
        return self._client.cookies.get(name)

    async def get_json(self, url: str, **kw):
        return (await self.get(url, **kw)).json()

    async def get_text(self, url: str, **kw) -> str:
        return (await self.get(url, **kw)).text

    async def aclose(self) -> None:
        await self._client.aclose()
        self._cache.close()
