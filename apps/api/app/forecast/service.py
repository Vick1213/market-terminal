"""Kronos OHLCV forecast service — probabilistic candle projections per symbol.

Model strategy (evaluated 2026-08-13): Kronos (arXiv:2508.02739, AAAI 2026,
MIT, 32k★) is a decoder-only transformer pre-trained on 12B+ K-lines from 45
exchanges. It tokenizes normalized OHLCV+amount bars and autoregressively
samples future tokens, so output is a *sampled scenario*, not a point
estimate — surface it as "what the distribution looks like", never as a
trading signal. Shipped default checkpoint is Kronos-small (24.7M params,
512-bar context): big enough to beat the mini model in upstream benchmarks,
small enough that CPU inference of a 30-bar horizon stays in single-digit
seconds. Callers may override per request via ``variant`` (see ``VARIANTS``
below) — e.g. a beefier machine can default to Kronos-base through
``MARKET_FORECAST_MODEL_ID`` in its local .env without changing the
committed default, and the panel can let a user trade latency for accuracy
on demand.

Design (mirrors SentimentService):
  * Lazy load on first request inside a single-worker ThreadPoolExecutor —
    weights download from HF once per model id (~100MB small / ~400MB base /
    ~16MB mini, plus a ~25MB tokenizer), the event loop never blocks on
    torch, and one worker serializes device access. Every model id fetched
    this way stays resident in ``_predictors`` for the life of the process
    (~100-400MB each) — cheap to keep around on a 64GB machine and it means
    switching variants mid-session never re-downloads or re-loads weights.
  * Context comes straight from ts_price (source='yahoo' daily bars namespace,
    same series every chart reads); rows with NULL OHLC are dropped, NULL
    volume becomes 0 (Kronos tolerates zero volume, not NaN).
  * Future timestamps: business days for equities/ETFs, calendar days for
    crypto — Kronos consumes calendar features (weekday/day/month), so the
    grid matters.
  * ``sample_count`` on ``KronosPredictor.predict`` averages N sampled paths
    internally (see ``auto_regressive_inference``'s ``np.mean(preds, axis=1)``
    in the vendored code) — that's upstream's own variance reduction for a
    single point estimate, and it destroys exactly the information an
    ensemble needs. ``forecast_distribution`` below instead calls
    ``predictor.predict_batch`` with the *same* context repeated across the
    batch dimension and ``sample_count=1`` per row: each batch row samples
    its own tokens independently via ``torch.multinomial`` at every
    autoregressive step, so the batch comes back as N genuinely distinct
    paths from one batched forward pass — no reseeding needed, and no
    vendored code touched (see ``forecast/kronos/kronos.py``, read-only).
    temperature/top_p are exposed for the panel's "volatility dial" in both
    modes.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from app.db.duck import DuckStore

if TYPE_CHECKING:
    from app.forecast.kronos import KronosPredictor

log = logging.getLogger("market.forecast")

DEFAULT_MODEL = "NeoQuasar/Kronos-small"
DEFAULT_TOKENIZER = "NeoQuasar/Kronos-Tokenizer-base"

MIN_CONTEXT_BARS = 64  # below this the normalization stats are too noisy
MAX_HORIZON = 120
MIN_PATHS = 4
MAX_PATHS = 64


@dataclass(frozen=True)
class VariantSpec:
    model_id: str
    tokenizer_id: str
    max_context: int


# Named checkpoints selectable per request via the `variant` argument /
# `model` query param. Keys are the only strings the API surface accepts —
# router validates against exactly these (Literal["mini", "small", "base"]).
VARIANTS: dict[str, VariantSpec] = {
    "mini": VariantSpec(
        model_id="NeoQuasar/Kronos-mini",
        tokenizer_id="NeoQuasar/Kronos-Tokenizer-2k",
        max_context=2048,
    ),
    "small": VariantSpec(
        model_id="NeoQuasar/Kronos-small",
        tokenizer_id="NeoQuasar/Kronos-Tokenizer-base",
        max_context=512,
    ),
    "base": VariantSpec(
        model_id="NeoQuasar/Kronos-base",
        tokenizer_id="NeoQuasar/Kronos-Tokenizer-base",
        max_context=512,
    ),
}


@dataclass
class ForecastBar:
    t: int  # unix seconds UTC
    open: float
    high: float
    low: float
    close: float
    volume: float

    def as_dict(self) -> dict:
        return {
            "t": self.t,
            "open": round(self.open, 6),
            "high": round(self.high, 6),
            "low": round(self.low, 6),
            "close": round(self.close, 6),
            "volume": round(self.volume, 2),
        }


@dataclass
class ForecastResult:
    symbol: str
    asset_class: str
    model_id: str
    device: str
    context_bars: int
    horizon: int
    generated_at: datetime
    history: list[ForecastBar]
    forecast: list[ForecastBar]


def _bar_from_row(ts: pd.Timestamp, r) -> ForecastBar:
    """Shared by both forecast modes: one ts_price-shaped row -> ForecastBar."""
    return ForecastBar(
        t=int(ts.replace(tzinfo=timezone.utc).timestamp()),
        open=float(r["open"]),
        high=float(r["high"]),
        low=float(r["low"]),
        close=float(r["close"]),
        volume=max(0.0, float(r["volume"])),  # sampled volume can dip below 0
    )


@dataclass
class QuantilePoint:
    """One horizon step's close-price quantiles across the path ensemble."""

    t: int  # unix seconds UTC — same future grid as the path mode
    p10: float
    p25: float
    p50: float
    p75: float
    p90: float


@dataclass
class DistributionStats:
    """Terminal (horizon-end) and path-level summary stats across the ensemble.

    Return stats are log(close_terminal / last_real_close) per path.
    Drawdown stats are the worst (last_real_close - path_low) / last_real_close
    seen anywhere in each path, i.e. "how far below today's close did this
    scenario ever trade".
    """

    mean_return: float
    median_return: float
    std_return: float
    skew_return: float
    p_up: float  # share of paths ending above last_close
    median_max_drawdown: float
    p_dd_5: float  # share of paths whose max drawdown from last_close exceeds 5%
    p_dd_10: float  # ...exceeds 10%


@dataclass
class LevelTouch:
    level: float
    direction: str  # "above" | "below" last_close
    p_touch: float  # share of paths whose high (above) / low (below) reaches level


@dataclass
class ForecastDistributionResult:
    symbol: str
    asset_class: str
    model_id: str
    device: str
    context_bars: int
    horizon: int
    paths: int
    generated_at: datetime
    history: list[ForecastBar]
    quantiles: list[QuantilePoint]
    stats: DistributionStats
    levels: list[LevelTouch]


class InsufficientHistoryError(RuntimeError):
    """Raised when ts_price has too few clean bars to condition the model."""


class ModelUnavailableError(RuntimeError):
    """Raised when the Kronos weights can't be loaded (HF unreachable, bad id).

    Left unset on failure so the next request retries the load — a transient
    HF outage shouldn't wedge the process until restart.
    """


class KronosForecastService:
    def __init__(
        self,
        duck: DuckStore,
        *,
        model_id: str = DEFAULT_MODEL,
        tokenizer_id: str = DEFAULT_TOKENIZER,
        device: str = "",
        max_context: int = 512,
    ) -> None:
        self._duck = duck
        # Settings-configured default, used whenever a request doesn't name a
        # registry variant. Kept independent of VARIANTS so a machine-local
        # .env override (or the offline smoke test's random local checkpoint
        # dirs) works with arbitrary model/tokenizer ids, not just mini/small/base.
        self._default_model_id = model_id
        self._default_tokenizer_id = tokenizer_id
        self._default_max_context = max_context
        self._device_override = device
        # Single worker: serializes every lazy load + all device access,
        # never the event loop. Because loads only ever happen inside this
        # one pool thread, _predictors below needs no lock even though
        # several variants can be cached in it over the process lifetime.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kronos")
        self._predictors: dict[str, KronosPredictor] = {}
        self.device: str = "unloaded"

    # ------------------------------------------------------------------ load

    def _resolve_variant(self, variant: str | None) -> tuple[str, str, int]:
        """(model_id, tokenizer_id, max_context) for a request's `variant`.

        None -> the settings-configured default (arbitrary ids allowed).
        A registry key -> that variant's pinned ids/context.
        """
        if variant is None:
            return self._default_model_id, self._default_tokenizer_id, self._default_max_context
        spec = VARIANTS[variant]  # router already validated membership
        return spec.model_id, spec.tokenizer_id, spec.max_context

    def _ensure_loaded(self, model_id: str, tokenizer_id: str, max_context: int) -> KronosPredictor:
        """Runs inside the pool thread; idempotent per model_id."""
        predictor = self._predictors.get(model_id)
        if predictor is not None:
            return predictor

        import torch

        from app.forecast.kronos import Kronos, KronosPredictor, KronosTokenizer

        if self._device_override:
            device = self._device_override
        elif torch.cuda.is_available():
            device = "cuda:0"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"

        log.info("loading %s + %s on %s …", model_id, tokenizer_id, device)
        try:
            tokenizer = KronosTokenizer.from_pretrained(tokenizer_id)
            model = Kronos.from_pretrained(model_id)
        except Exception as exc:
            log.exception("kronos weight load failed")
            raise ModelUnavailableError(f"Kronos weights unavailable ({model_id}): {exc}") from exc
        predictor = KronosPredictor(model, tokenizer, device=device, max_context=max_context)
        self._predictors[model_id] = predictor
        self.device = device
        n_params = sum(p.numel() for p in model.parameters())
        log.info(
            "kronos ready — %s, %.1fM params, context %d (%d model(s) resident)",
            model_id, n_params / 1e6, max_context, len(self._predictors),
        )
        return predictor

    # ------------------------------------------------------------------ data

    def _load_context(self, symbol: str, lookback: int) -> pd.DataFrame:
        rows = self._duck.fetchall(
            "SELECT ts, open, high, low, close, volume FROM ts_price "
            "WHERE source = 'yahoo' AND symbol = ? "
            "AND open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL AND close IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?",
            [symbol, lookback],
        )
        df = pd.DataFrame(
            rows[::-1], columns=["timestamps", "open", "high", "low", "close", "volume"]
        )
        if not df.empty:
            df["timestamps"] = pd.to_datetime(df["timestamps"])
            df["volume"] = df["volume"].fillna(0.0)
        return df

    @staticmethod
    def _future_index(last: pd.Timestamp, horizon: int, asset_class: str) -> pd.DatetimeIndex:
        if asset_class == "crypto":  # 24/7 markets trade calendar days
            return pd.date_range(last + pd.Timedelta(days=1), periods=horizon, freq="D")
        # Known limitation: bdate_range skips weekends but not exchange holidays
        # (~9 US days/yr get calendar features for a non-session). Kronos only
        # reads weekday/day/month from these, so the skew is mild — an exchange
        # calendar dep isn't worth it for a scenario generator.
        return pd.bdate_range(last + pd.Timedelta(days=1), periods=horizon)

    # ------------------------------------------------------------------ api

    def _forecast_sync(
        self,
        symbol: str,
        asset_class: str,
        horizon: int,
        lookback: int,
        temperature: float,
        top_p: float,
        samples: int,
        history_bars: int,
        variant: str | None,
    ) -> ForecastResult:
        model_id, tokenizer_id, max_context = self._resolve_variant(variant)
        predictor = self._ensure_loaded(model_id, tokenizer_id, max_context)

        # Per-variant context window — clamp to whichever model actually ran,
        # not a global constant (mini's 2048-bar tokenizer vs. 512 for the rest).
        df = self._load_context(symbol, min(lookback, max_context))
        if len(df) < MIN_CONTEXT_BARS:
            raise InsufficientHistoryError(
                f"{symbol}: {len(df)} clean daily bars in ts_price, need >= {MIN_CONTEXT_BARS}"
            )

        x_ts = df["timestamps"]
        y_index = self._future_index(x_ts.iloc[-1], horizon, asset_class)
        y_ts = pd.Series(y_index)

        pred_df = predictor.predict(
            df=df[["open", "high", "low", "close", "volume"]],
            x_timestamp=x_ts,
            y_timestamp=y_ts,
            pred_len=horizon,
            T=temperature,
            top_p=top_p,
            sample_count=samples,
            verbose=False,
        )

        tail = df.iloc[-history_bars:]
        return ForecastResult(
            symbol=symbol,
            asset_class=asset_class,
            model_id=model_id,
            device=self.device,
            context_bars=len(df),
            horizon=horizon,
            generated_at=datetime.now(timezone.utc),
            history=[_bar_from_row(r["timestamps"], r) for _, r in tail.iterrows()],
            forecast=[_bar_from_row(ts, r) for ts, r in pred_df.iterrows()],
        )

    def _forecast_distribution_sync(
        self,
        symbol: str,
        asset_class: str,
        horizon: int,
        lookback: int,
        temperature: float,
        top_p: float,
        paths: int,
        history_bars: int,
        variant: str | None,
        levels: list[float] | None,
    ) -> ForecastDistributionResult:
        model_id, tokenizer_id, max_context = self._resolve_variant(variant)
        predictor = self._ensure_loaded(model_id, tokenizer_id, max_context)

        df = self._load_context(symbol, min(lookback, max_context))
        if len(df) < MIN_CONTEXT_BARS:
            raise InsufficientHistoryError(
                f"{symbol}: {len(df)} clean daily bars in ts_price, need >= {MIN_CONTEXT_BARS}"
            )

        x_ts = df["timestamps"]
        y_index = self._future_index(x_ts.iloc[-1], horizon, asset_class)
        y_ts = pd.Series(y_index)
        features = df[["open", "high", "low", "close", "volume"]]

        # One batched autoregressive pass over `paths` identical contexts.
        # sample_count=1 per row is load-bearing: predictor.predict's own
        # sample_count>1 averages N samples into a single row internally
        # (see module docstring / auto_regressive_inference's np.mean), which
        # is exactly the variance we want to keep here. Each row in this
        # batch instead draws its own tokens independently at every step, so
        # the N pred_dfs below are genuinely distinct sampled scenarios.
        pred_dfs = predictor.predict_batch(
            df_list=[features] * paths,
            x_timestamp_list=[x_ts] * paths,
            y_timestamp_list=[y_ts] * paths,
            pred_len=horizon,
            T=temperature,
            top_p=top_p,
            sample_count=1,
            verbose=False,
        )

        closes = np.stack([p["close"].to_numpy(dtype=float) for p in pred_dfs])  # (paths, horizon)
        highs = np.stack([p["high"].to_numpy(dtype=float) for p in pred_dfs])
        lows = np.stack([p["low"].to_numpy(dtype=float) for p in pred_dfs])

        last_close = float(df["close"].iloc[-1])

        # np.percentile guarantees p10 <= p25 <= p50 <= p75 <= p90 by construction.
        q = np.percentile(closes, [10, 25, 50, 75, 90], axis=0)  # (5, horizon)
        quantiles = [
            QuantilePoint(
                t=int(ts.replace(tzinfo=timezone.utc).timestamp()),
                p10=round(float(q[0, i]), 6),
                p25=round(float(q[1, i]), 6),
                p50=round(float(q[2, i]), 6),
                p75=round(float(q[3, i]), 6),
                p90=round(float(q[4, i]), 6),
            )
            for i, ts in enumerate(y_index)
        ]

        terminal_close = closes[:, -1]
        log_ret = np.log(terminal_close / last_close)
        mean_r = float(np.mean(log_ret))
        std_r = float(np.std(log_ret))
        # Fisher-Pearson skewness coefficient (population, no scipy dep needed).
        skew_r = float(np.mean(((log_ret - mean_r) / std_r) ** 3)) if std_r > 1e-12 else 0.0

        # Worst drawdown from *today's* close (not peak-to-trough within the
        # path) — the panel-relevant question is "how far under where we are
        # now did this scenario ever trade", using path lows per the spec.
        path_min_low = lows.min(axis=1)  # (paths,)
        max_dd = np.clip((last_close - path_min_low) / last_close, 0.0, None)

        stats = DistributionStats(
            mean_return=round(mean_r, 6),
            median_return=round(float(np.median(log_ret)), 6),
            std_return=round(std_r, 6),
            skew_return=round(skew_r, 6),
            p_up=round(float(np.mean(terminal_close > last_close)), 6),
            median_max_drawdown=round(float(np.median(max_dd)), 6),
            p_dd_5=round(float(np.mean(max_dd > 0.05)), 6),
            p_dd_10=round(float(np.mean(max_dd > 0.10)), 6),
        )

        level_list = levels if levels else [last_close * 0.95, last_close * 1.05]
        level_results = []
        for level in level_list:
            if level >= last_close:
                touched = np.any(highs >= level, axis=1)
                direction = "above"
            else:
                touched = np.any(lows <= level, axis=1)
                direction = "below"
            level_results.append(
                LevelTouch(
                    level=round(float(level), 6),
                    direction=direction,
                    p_touch=round(float(np.mean(touched)), 6),
                )
            )

        tail = df.iloc[-history_bars:]
        return ForecastDistributionResult(
            symbol=symbol,
            asset_class=asset_class,
            model_id=model_id,
            device=self.device,
            context_bars=len(df),
            horizon=horizon,
            paths=paths,
            generated_at=datetime.now(timezone.utc),
            history=[_bar_from_row(r["timestamps"], r) for _, r in tail.iterrows()],
            quantiles=quantiles,
            stats=stats,
            levels=level_results,
        )

    async def forecast(
        self,
        symbol: str,
        asset_class: str = "equity",
        *,
        horizon: int = 30,
        lookback: int = 400,
        temperature: float = 1.0,
        top_p: float = 0.9,
        samples: int = 1,
        history_bars: int = 90,
        variant: str | None = None,
    ) -> ForecastResult:
        """variant: None uses the settings-configured default ids (works with
        arbitrary/local checkpoint dirs, e.g. the offline smoke test); a
        registry key ("mini"/"small"/"base") pins that variant's ids and
        clamps lookback to its own max_context.
        """
        horizon = max(1, min(horizon, MAX_HORIZON))
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._pool,
            self._forecast_sync,
            symbol,
            asset_class,
            horizon,
            lookback,
            temperature,
            top_p,
            samples,
            history_bars,
            variant,
        )

    async def forecast_distribution(
        self,
        symbol: str,
        asset_class: str = "equity",
        *,
        horizon: int = 30,
        lookback: int = 400,
        temperature: float = 1.0,
        top_p: float = 0.9,
        paths: int = 16,
        variant: str | None = None,
        levels: list[float] | None = None,
    ) -> ForecastDistributionResult:
        """N-path ensemble: quantile cone + terminal/drawdown/level-touch stats.

        `paths` is clamped to [MIN_PATHS, MAX_PATHS] here so callers (router
        included) can't request a batch that stalls the single pool worker
        or blows up CPU memory. `levels` is an optional list of absolute
        price levels for touch-probability; None defaults to last_close's
        +-5% band inside `_forecast_distribution_sync`.
        """
        horizon = max(1, min(horizon, MAX_HORIZON))
        paths = max(MIN_PATHS, min(paths, MAX_PATHS))
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._pool,
            self._forecast_distribution_sync,
            symbol,
            asset_class,
            horizon,
            lookback,
            temperature,
            top_p,
            paths,
            90,  # history_bars — same default tail length as the path mode
            variant,
            levels,
        )

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
