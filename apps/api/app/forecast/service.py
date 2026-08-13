"""Kronos OHLCV forecast service — probabilistic candle projections per symbol.

Model strategy (evaluated 2026-08-13): Kronos (arXiv:2508.02739, AAAI 2026,
MIT, 32k★) is a decoder-only transformer pre-trained on 12B+ K-lines from 45
exchanges. It tokenizes normalized OHLCV+amount bars and autoregressively
samples future tokens, so output is a *sampled scenario*, not a point
estimate — surface it as "what the distribution looks like", never as a
trading signal. Default checkpoint is Kronos-small (24.7M params, 512-bar
context): big enough to beat the mini model in upstream benchmarks, small
enough that CPU inference of a 30-bar horizon stays in single-digit seconds.

Design (mirrors SentimentService):
  * Lazy load on first request inside a single-worker ThreadPoolExecutor —
    weights download from HF once (~100MB small / ~25MB tokenizer), the event
    loop never blocks on torch, and one worker serializes device access.
  * Context comes straight from ts_price (source='yahoo' daily bars namespace,
    same series every chart reads); rows with NULL OHLC are dropped, NULL
    volume becomes 0 (Kronos tolerates zero volume, not NaN).
  * Future timestamps: business days for equities/ETFs, calendar days for
    crypto — Kronos consumes calendar features (weekday/day/month), so the
    grid matters.
  * ``sample_count`` averages N sampled paths (upstream's own variance
    reduction); temperature/top_p are exposed for the panel's "volatility
    dial".
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from app.db.duck import DuckStore

log = logging.getLogger("market.forecast")

DEFAULT_MODEL = "NeoQuasar/Kronos-small"
DEFAULT_TOKENIZER = "NeoQuasar/Kronos-Tokenizer-base"

MIN_CONTEXT_BARS = 64  # below this the normalization stats are too noisy
MAX_HORIZON = 120


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
        self._model_id = model_id
        self._tokenizer_id = tokenizer_id
        self._device_override = device
        self._max_context = max_context
        # Single worker: serializes lazy load + device access, never the loop.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kronos")
        self._load_lock = threading.Lock()
        self._predictor = None
        self.device: str = "unloaded"

    # ------------------------------------------------------------------ load

    def _ensure_loaded(self) -> None:
        """Runs inside the pool thread; idempotent."""
        with self._load_lock:
            if self._predictor is not None:
                return
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

            log.info("loading %s + %s on %s …", self._model_id, self._tokenizer_id, device)
            try:
                tokenizer = KronosTokenizer.from_pretrained(self._tokenizer_id)
                model = Kronos.from_pretrained(self._model_id)
            except Exception as exc:
                log.exception("kronos weight load failed")
                raise ModelUnavailableError(
                    f"Kronos weights unavailable ({self._model_id}): {exc}"
                ) from exc
            self._predictor = KronosPredictor(
                model, tokenizer, device=device, max_context=self._max_context
            )
            self.device = device
            n_params = sum(p.numel() for p in model.parameters())
            log.info("kronos ready — %.1fM params, context %d", n_params / 1e6, self._max_context)

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
    ) -> ForecastResult:
        self._ensure_loaded()

        df = self._load_context(symbol, min(lookback, self._max_context))
        if len(df) < MIN_CONTEXT_BARS:
            raise InsufficientHistoryError(
                f"{symbol}: {len(df)} clean daily bars in ts_price, need >= {MIN_CONTEXT_BARS}"
            )

        x_ts = df["timestamps"]
        y_index = self._future_index(x_ts.iloc[-1], horizon, asset_class)
        y_ts = pd.Series(y_index)

        pred_df = self._predictor.predict(
            df=df[["open", "high", "low", "close", "volume"]],
            x_timestamp=x_ts,
            y_timestamp=y_ts,
            pred_len=horizon,
            T=temperature,
            top_p=top_p,
            sample_count=samples,
            verbose=False,
        )

        def bar(ts: pd.Timestamp, r) -> ForecastBar:
            return ForecastBar(
                t=int(ts.replace(tzinfo=timezone.utc).timestamp()),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=max(0.0, float(r["volume"])),  # sampled volume can dip below 0
            )

        tail = df.iloc[-history_bars:]
        return ForecastResult(
            symbol=symbol,
            asset_class=asset_class,
            model_id=self._model_id,
            device=self.device,
            context_bars=len(df),
            horizon=horizon,
            generated_at=datetime.now(timezone.utc),
            history=[bar(r["timestamps"], r) for _, r in tail.iterrows()],
            forecast=[bar(ts, r) for ts, r in pred_df.iterrows()],
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
    ) -> ForecastResult:
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
        )

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
