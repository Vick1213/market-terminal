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
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.forecast import InsufficientHistoryError, ModelUnavailableError
from app.ingest.prices import ensure_daily_history

router = APIRouter(prefix="/api", tags=["forecast"])

# Registry keys from app/forecast/service.py VARIANTS, restated as a Literal
# so FastAPI/pydantic reject anything else with a 422 instead of reaching the
# service (which would otherwise KeyError on an unknown variant).
ModelVariant = Literal["mini", "small", "base"]


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


class QuantilePointOut(BaseModel):
    t: int  # unix seconds (UTC)
    p10: float
    p25: float
    p50: float
    p75: float
    p90: float


class DistributionStatsOut(BaseModel):
    mean_return: float
    median_return: float
    std_return: float
    skew_return: float
    p_up: float
    median_max_drawdown: float
    p_dd_5: float
    p_dd_10: float


class LevelTouchOut(BaseModel):
    level: float
    direction: str
    p_touch: float


class ForecastDistributionResponse(BaseModel):
    symbol: str
    asset_class: str
    model: str
    device: str
    context_bars: int
    horizon: int
    paths: int
    generated_at: datetime
    disclaimer: str
    history: list[ForecastBarOut]
    quantiles: list[QuantilePointOut]
    stats: DistributionStatsOut
    levels: list[LevelTouchOut]


DISCLAIMER = (
    "Sampled scenario from a generative model (Kronos), not investment advice; "
    "re-running produces a different path."
)

DISTRIBUTION_DISCLAIMER = (
    "Distribution of N sampled scenarios from a generative model (Kronos), not "
    "investment advice; quantiles and probabilities summarize model uncertainty, "
    "not a guaranteed range."
)


async def _resolve_asset_class(request: Request, symbol: str) -> str:
    sqlite = request.app.state.sqlite
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    def _lookup() -> str:
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

    return await loop.run_in_executor(None, _lookup)


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
    model: ModelVariant | None = Query(
        default=None,
        description="Kronos checkpoint size (mini/small/base); omit for the server's configured default",
    ),
) -> ForecastResponse:
    duck = request.app.state.duck
    http = request.app.state.http
    service = request.app.state.forecast
    symbol = symbol.upper()

    asset_class = await _resolve_asset_class(request, symbol)

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
            variant=model,
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


def _parse_levels(raw: str | None) -> list[float] | None:
    """Comma-separated absolute price levels, or None to use the service's
    last_close +-5% default. Raises HTTPException(422) on malformed input —
    FastAPI can't validate a free-form CSV string via Query() constraints."""
    if not raw:
        return None
    try:
        levels = [float(part) for part in raw.split(",") if part.strip() != ""]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid levels: {raw!r}") from exc
    if not levels:
        raise HTTPException(status_code=422, detail="levels, if given, must contain at least one value")
    return levels


@router.get("/forecast/distribution", response_model=ForecastDistributionResponse)
async def forecast_distribution(
    request: Request,
    symbol: str = Query(min_length=1, max_length=16),
    horizon: int = Query(default=30, ge=1, le=120, description="bars to predict"),
    lookback: int = Query(default=400, ge=64, le=2048, description="context bars"),
    temperature: float = Query(default=1.0, ge=0.1, le=2.0),
    top_p: float = Query(default=0.9, ge=0.1, le=1.0),
    paths: int = Query(default=16, ge=4, le=64, description="independent sampled paths in the ensemble"),
    levels: str | None = Query(
        default=None,
        description="comma-separated absolute price levels for touch probability; "
        "omit for last_close +-5%",
    ),
    model: ModelVariant | None = Query(
        default=None,
        description="Kronos checkpoint size (mini/small/base); omit for the server's configured default",
    ),
) -> ForecastDistributionResponse:
    duck = request.app.state.duck
    http = request.app.state.http
    service = request.app.state.forecast
    symbol = symbol.upper()
    level_list = _parse_levels(levels)

    asset_class = await _resolve_asset_class(request, symbol)

    # Same on-demand cache-fill every chart uses; forecast then reads ts_price.
    await ensure_daily_history(http, duck, symbol, asset_class)

    try:
        result = await service.forecast_distribution(
            symbol,
            asset_class,
            horizon=horizon,
            lookback=lookback,
            temperature=temperature,
            top_p=top_p,
            paths=paths,
            variant=model,
            levels=level_list,
        )
    except InsufficientHistoryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ModelUnavailableError as exc:
        # First call downloads weights from HF; if that host is unreachable
        # the next request retries the load, so 503 (not 500) is honest.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return ForecastDistributionResponse(
        symbol=result.symbol,
        asset_class=result.asset_class,
        model=result.model_id,
        device=result.device,
        context_bars=result.context_bars,
        horizon=result.horizon,
        paths=result.paths,
        generated_at=result.generated_at,
        disclaimer=DISTRIBUTION_DISCLAIMER,
        history=[ForecastBarOut(**b.as_dict()) for b in result.history],
        quantiles=[
            QuantilePointOut(t=q.t, p10=q.p10, p25=q.p25, p50=q.p50, p75=q.p75, p90=q.p90)
            for q in result.quantiles
        ],
        stats=DistributionStatsOut(
            mean_return=result.stats.mean_return,
            median_return=result.stats.median_return,
            std_return=result.stats.std_return,
            skew_return=result.stats.skew_return,
            p_up=result.stats.p_up,
            median_max_drawdown=result.stats.median_max_drawdown,
            p_dd_5=result.stats.p_dd_5,
            p_dd_10=result.stats.p_dd_10,
        ),
        levels=[
            LevelTouchOut(level=lv.level, direction=lv.direction, p_touch=lv.p_touch)
            for lv in result.levels
        ],
    )
