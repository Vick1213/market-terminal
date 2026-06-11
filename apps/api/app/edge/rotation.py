"""Relative-strength rotation (RRG-style) + seasonality (PLAN §6 #5).

Pure math on cached daily bars — the scheduled run only tops up sector-ETF
history via the cache-first yfinance path, then everything is DuckDB + pandas.

RS-Ratio / RS-Momentum use the standard open approximation of the JdK inputs:
  rs        = 100 * close / benchmark_close
  rs_ratio  = 100 + zscore(rs, 63d)
  rs_mom    = 100 + zscore(pct_change(rs_ratio, 21d), 63d)
Quadrants: leading (both>100) / weakening (ratio>100, mom<100) /
lagging (both<100) / improving (ratio<100, mom>100). An 8-point weekly trail
is included for the scatter.

Seasonality: per watchlist symbol with >=3y of history, the historical mean
return and win rate of the current and next calendar month.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pandas as pd

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ingest.prices import ensure_daily_history
from app.macro.breadth import compute_breadth_series

log = logging.getLogger("market.edge.rotation")

SECTOR_NAMES = {
    "XLK": "Tech", "XLF": "Financials", "XLE": "Energy", "XLV": "Health Care",
    "XLI": "Industrials", "XLP": "Staples", "XLY": "Discretionary",
    "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real Estate",
    "XLC": "Comm Svcs",
}

_WINDOW = 63   # ~one quarter of trading days
_MOM_LAG = 21  # ~one month


def _closes(duck: DuckStore, symbol: str) -> pd.Series | None:
    df = duck.fetchdf(
        "SELECT ts, close FROM ts_price WHERE source = 'yahoo' AND symbol = ? "
        "AND close IS NOT NULL ORDER BY ts",
        [symbol],
    )
    if df is None or len(df) < _WINDOW + _MOM_LAG + 10:
        return None
    s = df.set_index("ts")["close"]
    s.index = pd.to_datetime(s.index)
    return s


def _zseries(s: pd.Series, window: int) -> pd.Series:
    mu = s.rolling(window).mean()
    sd = s.rolling(window).std()
    return (s - mu) / sd


def compute_rrg(duck: DuckStore, sectors: list[str], benchmark: str) -> dict:
    bench = _closes(duck, benchmark)
    out: list[dict] = []
    if bench is None:
        return {"benchmark": benchmark, "sectors": out}
    for sym in sectors:
        s = _closes(duck, sym)
        if s is None:
            continue
        df = pd.concat([s.rename("s"), bench.rename("b")], axis=1).dropna()
        if len(df) < _WINDOW + _MOM_LAG + 10:
            continue
        rs = 100 * df["s"] / df["b"]
        rs_ratio = 100 + _zseries(rs, _WINDOW)
        rs_mom = 100 + _zseries(rs_ratio.pct_change(_MOM_LAG) * 100, _WINDOW)
        pair = pd.concat([rs_ratio.rename("r"), rs_mom.rename("m")], axis=1).dropna()
        if pair.empty:
            continue
        r, m = float(pair["r"].iloc[-1]), float(pair["m"].iloc[-1])
        quadrant = (
            "leading" if r >= 100 and m >= 100
            else "weakening" if r >= 100
            else "improving" if m >= 100
            else "lagging"
        )
        # Weekly trail (last 8 Fridays-ish: every 5th trading day).
        tail = pair.iloc[::-5].iloc[:8].iloc[::-1]
        out.append({
            "symbol": sym,
            "name": SECTOR_NAMES.get(sym, sym),
            "rs_ratio": round(r, 2),
            "rs_momentum": round(m, 2),
            "quadrant": quadrant,
            "trail": [
                {"ts": ts.strftime("%Y-%m-%d"), "r": round(float(row["r"]), 2),
                 "m": round(float(row["m"]), 2)}
                for ts, row in tail.iterrows()
            ],
        })
    return {"benchmark": benchmark, "sectors": out}


def compute_seasonality(duck: DuckStore, symbols: list[str]) -> list[dict]:
    now = datetime.now(timezone.utc)
    months = [now.month, now.month % 12 + 1]
    out: list[dict] = []
    for sym in symbols:
        s = _closes(duck, sym)
        if s is None or (s.index[-1] - s.index[0]).days < 3 * 365:
            continue
        monthly = s.resample("ME").last().pct_change().dropna() * 100
        row: dict = {"symbol": sym, "months": []}
        for m in months:
            rets = monthly[monthly.index.month == m]
            # Exclude the in-progress month from its own history.
            rets = rets[rets.index < now.replace(day=1, tzinfo=None)]
            if len(rets) < 3:
                continue
            row["months"].append({
                "month": m,
                "mean_return": round(float(rets.mean()), 2),
                "win_rate": round(float((rets > 0).mean() * 100), 0),
                "years": int(len(rets)),
            })
        if row["months"]:
            out.append(row)
    return out


class RotationPipeline:
    """Scheduled top-up of sector bars; compute happens on demand in the router."""

    def __init__(self, duck: DuckStore, sqlite: SqliteStore,
                 sectors: list[str], benchmark: str) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._sectors = sectors
        self._benchmark = benchmark

    @property
    def sectors(self) -> list[str]:
        return self._sectors

    @property
    def benchmark(self) -> str:
        return self._benchmark

    def watchlist_symbols(self) -> list[str]:
        rows = self._sqlite.fetchall(
            "SELECT symbol, asset_class FROM watchlist ORDER BY sort_order"
        )
        return [r[0] for r in rows if r[1] in ("equity", "metal", "crypto")]

    async def run(self) -> None:
        for sym in [self._benchmark, *self._sectors]:
            try:
                await ensure_daily_history(None, self._duck, sym, "equity")
            except Exception as exc:
                log.warning("sector leg %s failed: %s", sym, exc)
        log.info("rotation history ensured (%s sectors)", len(self._sectors))
        # The sector bars double as the composite's breadth input — recompute
        # the BREADTH_PCT_ABOVE_200DMA series while they're fresh.
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, compute_breadth_series, self._duck, self._sectors
            )
        except Exception as exc:
            log.warning("breadth compute failed: %s", exc)
