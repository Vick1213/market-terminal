"""M3: onboarding & settings UI — /api/settings.

Replaces hand-edited .env for the BYO credential / provider-endpoint fields
whitelisted in ``app/settings_store.py`` (see that module's docstring for
the store > env > default precedence and the atomic-write/masking
guarantees). No secret is ever logged or returned in full.

  GET  /api/settings                    every whitelisted field (secrets
                                         masked to their last 4 chars) +
                                         provenance + the onboarding flag
  PUT  /api/settings                    partial update ({field: value});
                                         "" unsets a field. Re-applies the
                                         overlay so changes take effect
                                         without a restart where feasible.
  POST /api/settings/test/{provider}    one cheap live call with the
                                         currently-effective credential
                                         (fred|tiingo|alpaca|fmp|finnhub|llm|ntfy)
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Body, HTTPException, Request

from app.settings_store import apply_overlay, get_settings_store, settings_snapshot

router = APIRouter(prefix="/api/settings", tags=["settings"])
log = logging.getLogger("market.routers.settings")

_TEST_TIMEOUT = 5.0


def _settings(request: Request):
    return request.app.state.settings


@router.get("")
async def get_settings_endpoint(request: Request) -> dict:
    return settings_snapshot(_settings(request))


@router.put("")
async def put_settings_endpoint(request: Request, updates: dict = Body(...)) -> dict:
    store = get_settings_store()
    try:
        store.set_fields(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    apply_overlay(_settings(request), store)
    return settings_snapshot(_settings(request), store)


# --- per-provider connectivity tests: one cheap call, currently-effective creds ---


async def _test_fred(settings) -> dict:
    if not settings.fred_api_key:
        return {"ok": False, "detail": "no FRED API key configured"}
    try:
        async with httpx.AsyncClient(timeout=_TEST_TIMEOUT) as client:
            resp = await client.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": "DGS10",
                    "api_key": settings.fred_api_key,
                    "file_type": "json",
                    "limit": 1,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        if "observations" not in data:
            return {"ok": False, "detail": f"unexpected FRED response shape: {list(data)[:5]}"}
        return {"ok": True, "detail": "FRED key valid — fetched 1 DGS10 observation"}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "detail": f"FRED {exc.response.status_code}: {exc.response.text[:150]}"}
    except Exception as exc:
        return {"ok": False, "detail": f"FRED request failed: {exc}"}


async def _test_tiingo(settings) -> dict:
    if not settings.tiingo_api_key:
        return {"ok": False, "detail": "no Tiingo API key configured"}
    try:
        async with httpx.AsyncClient(timeout=_TEST_TIMEOUT) as client:
            resp = await client.get(
                "https://api.tiingo.com/tiingo/daily/AAPL",
                params={"token": settings.tiingo_api_key},
            )
            resp.raise_for_status()
            data = resp.json()
        name = data.get("name") or data.get("ticker") or "AAPL"
        return {"ok": True, "detail": f"Tiingo key valid — fetched metadata for {name}"}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "detail": f"Tiingo {exc.response.status_code}: {exc.response.text[:150]}"}
    except Exception as exc:
        return {"ok": False, "detail": f"Tiingo request failed: {exc}"}


async def _test_alpaca(settings) -> dict:
    key = settings.paper_trading_key_id
    secret = settings.paper_trading_secret_key
    if not key or not secret:
        return {"ok": False, "detail": "no Alpaca paper (or data) key/secret configured"}
    try:
        async with httpx.AsyncClient(timeout=_TEST_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.alpaca_trading_base_url.rstrip('/')}/v2/account",
                headers={
                    "APCA-API-KEY-ID": key,
                    "APCA-API-SECRET-KEY": secret,
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return {"ok": True, "detail": f"Alpaca account reachable — status={data.get('status', 'unknown')}"}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "detail": f"Alpaca {exc.response.status_code}: {exc.response.text[:150]}"}
    except Exception as exc:
        return {"ok": False, "detail": f"Alpaca request failed: {exc}"}


async def _test_fmp(settings) -> dict:
    if not settings.fmp_api_key:
        return {"ok": False, "detail": "no FMP API key configured"}
    try:
        async with httpx.AsyncClient(timeout=_TEST_TIMEOUT) as client:
            resp = await client.get(
                "https://financialmodelingprep.com/api/v3/quote/AAPL",
                params={"apikey": settings.fmp_api_key},
            )
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, list) or not data:
            return {"ok": False, "detail": f"unexpected FMP response: {str(data)[:150]}"}
        return {"ok": True, "detail": "FMP key valid — fetched 1 AAPL quote"}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "detail": f"FMP {exc.response.status_code}: {exc.response.text[:150]}"}
    except Exception as exc:
        return {"ok": False, "detail": f"FMP request failed: {exc}"}


async def _test_finnhub(settings) -> dict:
    if not settings.finnhub_api_key:
        return {"ok": False, "detail": "no Finnhub API key configured"}
    try:
        async with httpx.AsyncClient(timeout=_TEST_TIMEOUT) as client:
            resp = await client.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": "AAPL", "token": settings.finnhub_api_key},
            )
            resp.raise_for_status()
            data = resp.json()
        if "c" not in data:
            return {"ok": False, "detail": f"unexpected Finnhub response: {str(data)[:150]}"}
        return {"ok": True, "detail": "Finnhub key valid — fetched 1 AAPL quote"}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "detail": f"Finnhub {exc.response.status_code}: {exc.response.text[:150]}"}
    except Exception as exc:
        return {"ok": False, "detail": f"Finnhub request failed: {exc}"}


async def _test_llm(settings) -> dict:
    from app.edge.llm import LlmClient

    llm = LlmClient(
        provider=settings.llm_provider,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        ollama_url=settings.ollama_url,
        ollama_model=settings.ollama_model,
        timeout=_TEST_TIMEOUT,
    )
    try:
        text = await llm.generate(
            "Reply with a short sentence confirming this connectivity test succeeded."
        )
        return {"ok": True, "detail": f"{llm.label}: {text[:120]}"}
    except Exception as exc:
        return {"ok": False, "detail": f"{llm.label} failed: {exc}"}


async def _test_ntfy(settings) -> dict:
    if not settings.ntfy_topic:
        return {"ok": False, "detail": "no ntfy topic configured (push stays in-app only)"}
    url = f"{settings.ntfy_server.rstrip('/')}/{settings.ntfy_topic}"
    try:
        async with httpx.AsyncClient(timeout=_TEST_TIMEOUT) as client:
            resp = await client.post(
                url,
                content=b"Market Terminal settings test \xe2\x80\x94 if you see this, push works.",
                headers={"Title": "Settings test", "Priority": "default"},
            )
            resp.raise_for_status()
        return {"ok": True, "detail": f"test push sent to {settings.ntfy_server}"}
    except Exception as exc:
        return {"ok": False, "detail": f"ntfy publish failed: {exc}"}


_TESTERS = {
    "fred": _test_fred,
    "tiingo": _test_tiingo,
    "alpaca": _test_alpaca,
    "fmp": _test_fmp,
    "finnhub": _test_finnhub,
    "llm": _test_llm,
    "ntfy": _test_ntfy,
}


@router.post("/test/{provider}")
async def test_provider(request: Request, provider: str) -> dict:
    tester = _TESTERS.get(provider.lower())
    if tester is None:
        return {
            "ok": False,
            "detail": f"unknown test provider {provider!r} — expected one of {sorted(_TESTERS)}",
        }
    return await tester(_settings(request))
