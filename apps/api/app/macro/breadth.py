"""Breadth bucket input — % of sector ETFs above their 200DMA (Phase 9 §1).

Completes the composite's empty 20% Breadth bucket: the 11 SPDR sector ETFs
(the rotation pipeline already keeps ~5y of their daily bars in ts_price)
stand in for "the market" — the share trading above their own 200DMA is the
classic participation gauge. The whole history is recomputed and upserted on
every run (cheap: 11 series of ~1250 closes), so the series backfills as far
as the cached bars allow and the composite's 20-point z-score floor is met
immediately instead of after a month of daily prints.

Pure pandas + DuckDB — callers dispatch via ``run_in_executor``.
"""

from __future__ import annotations

import logging

import pandas as pd

from app.db.duck import DuckStore

log = logging.getLogger("market.macro.breadth")

SERIES_ID = "BREADTH_PCT_ABOVE_200DMA"
DMA_WINDOW = 200
# A day only counts when most of the universe has a valid close + 200DMA —
# a 3-of-11 reading during backfill warm-up would be noise, not breadth.
MIN_VALID = 8


def compute_breadth_series(duck: DuckStore, sectors: list[str]) -> int:
    """Recompute the full daily breadth series from cached bars and store it.
    Returns the number of days written."""
    ph = ", ".join("?" for _ in sectors)
    df = duck.fetchdf(
        f"SELECT symbol, ts, close FROM ts_price "
        f"WHERE source = 'yahoo' AND symbol IN ({ph}) AND close IS NOT NULL "
        f"ORDER BY ts",
        list(sectors),
    )
    if df is None or df.empty:
        return 0
    df["date"] = pd.to_datetime(df["ts"]).dt.normalize()
    closes = df.pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
    ma = closes.rolling(DMA_WINDOW, min_periods=DMA_WINDOW).mean()
    valid = closes.notna() & ma.notna()
    above = (closes > ma) & valid
    n_valid = valid.sum(axis=1)
    pct = (above.sum(axis=1) / n_valid * 100.0)[n_valid >= MIN_VALID].dropna()
    if pct.empty:
        return 0
    duck.executemany(
        "INSERT OR REPLACE INTO ts_macro (series_id, ts, value, source) "
        f"VALUES ('{SERIES_ID}', ?, ?, 'computed')",
        [(ts.to_pydatetime(), round(float(v), 2)) for ts, v in pct.items()],
    )
    log.info("breadth: %s days stored (latest %s = %.1f%%)",
             len(pct), pct.index[-1].date(), float(pct.iloc[-1]))
    return len(pct)
