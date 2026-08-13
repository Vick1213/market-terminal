"""GET /api/forecast — Kronos OHLCV candle projections for any watchlist symbol.

Serves a sampled future path (not a point estimate — see app/forecast/service.py)
plus the trailing actual bars so the chart can draw history and projection as
one continuous candlestick series. First call per process is slow: it lazily
downloads/loads the Kronos weights and runs autoregressive CPU inference
(~1 model step per predicted bar).
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.forecast import InsufficientHistoryError, ModelUnavailableError
from app.ingest.prices import ensure_daily_history

router = APIRouter(prefix="/api", tags=["forecast"])


class ForecastBarOut(BaseModel):
    t: int  # unix seconds (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float


class ForecastResponse(BaseModel):
    symbol: str
    asset_class: str
    model: str
    device: str
    context_bars: int
    horizon: int
    generated_at: datetime
    disclaimer: str
    history: list[ForecastBarOut]
    forecast: list[ForecastBarOut]


DISCLAIMER = (
    "Sampled scenario from a generative model (Kronos), not investment advice; "
    "re-running produces a different path."
)


@router.get("/forecast", response_model=ForecastResponse)
async def forecast(
    request: Request,
    symbol: str = Query(min_length=1, max_length=16),
    horizon: int = Query(default=30, ge=1, le=120, description="bars to predict"),
    # Upper bound covers the 2k tokenizer config; the service clamps to the
    # configured forecast_max_context, so 512-context models are unaffected.
    lookback: int = Query(default=400, ge=64, le=2048, description="context bars"),
    temperature: float = Query(default=1.0, ge=0.1, le=2.0),
    top_p: float = Query(default=0.9, ge=0.1, le=1.0),
    samples: int = Query(default=1, ge=1, le=5, description="paths averaged"),
) -> ForecastResponse:
    duck = request.app.state.duck
    sqlite = request.app.state.sqlite
    http = request.app.state.http
    service = request.app.state.forecast
    symbol = symbol.upper()

    loop = asyncio.get_running_loop()

    def _asset_class() -> str:
        row = sqlite.fetchone("SELECT asset_class FROM watchlist WHERE symbol = ?", [symbol])
        if row:
            return row["asset_class"]
        # Off-watchlist symbol: trust however its bars were ingested before
        # defaulting to equity — asset_class drives the business-day vs
        # calendar-day forecast grid, so guessing wrong skews crypto forecasts.
        prior = duck.fetchone(
            "SELECT asset_class FROM ts_price WHERE source = 'yahoo' AND symbol = ? LIMIT 1",
            [symbol],
        )
        return prior[0] if prior else "equity"

    asset_class = await loop.run_in_executor(None, _asset_class)

    # Same on-demand cache-fill every chart uses; forecast then reads ts_price.
    await ensure_daily_history(http, duck, symbol, asset_class)

    try:
        result = await service.forecast(
            symbol,
            asset_class,
            horizon=horizon,
            lookback=lookback,
            temperature=temperature,
            top_p=top_p,
            samples=samples,
        )
    except InsufficientHistoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ModelUnavailableError as exc:
        # First call downloads weights from HF; if that host is unreachable
        # the next request retries the load, so 503 (not 500) is honest.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return ForecastResponse(
        symbol=result.symbol,
        asset_class=result.asset_class,
        model=result.model_id,
        device=result.device,
        context_bars=result.context_bars,
        horizon=result.horizon,
        generated_at=result.generated_at,
        disclaimer=DISCLAIMER,
        history=[ForecastBarOut(**b.as_dict()) for b in result.history],
        forecast=[ForecastBarOut(**b.as_dict()) for b in result.forecast],
    )
