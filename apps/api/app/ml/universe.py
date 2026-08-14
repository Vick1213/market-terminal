"""Incremental daily price refresh for the bot-tradable symbol set (PLAN
§13.9 step 1, blocking issue described in §13.2, architecture in §13.5).

Promotes ``scratchpad/fetch_universe.py``'s one-shot 147-name pull into a
proper scheduler job so per-name vol scoring (§13.5's ``vol_scores.py``, not
yet built) always has fresh daily OHLC to work from. This module is pure data
plumbing — it fetches and stores prices only, and must never change trading
behaviour.

Writes the SAME ``data/ml/universe.duckdb`` the one-shot script wrote (its
own single-writer file, kept out of the live ``market.duckdb`` — same
"big/slow panel stays out of the main DB" convention as ``app.ml.snapshot``),
with the SAME ``ts_price`` schema: ``source, symbol, asset_class, ts,
open, high, low, close, volume``, ``PRIMARY KEY (source, symbol, ts)``.

Incremental means: for each symbol, look up ``max(ts)`` already stored and
fetch only the gap forward (``yf.Ticker(sym).history(start=...)``); a symbol
with no rows yet (or ``full=True``) gets a full ``period="max"`` pull. Upserts
via DuckDB's ``INSERT OR REPLACE`` against the ``(source, symbol, ts)``
primary key, so re-running is idempotent and never duplicates rows — and,
critically, an incremental run never deletes history outside its fetch
window (only ``full=True`` does a delete-then-refetch per symbol, mirroring
the original script's full-refresh behaviour).

Gentle-fetch discipline preserved from the original script: sequential
single-ticker pulls, ``time.sleep(sleep_s)`` between symbols, 3-attempt retry
with backoff. Yahoo throttles bulk/threaded pulls — this is why the job is
scheduled daily rather than fetched on demand (§13.2).

SPLIT/DIVIDEND RE-ADJUSTMENT DRIFT: ``auto_adjust=True`` means Yahoo
retroactively rewrites a symbol's ENTIRE history on a corporate action, but
an incremental fetch only re-writes the tail — left unchecked, the stored
series would splice stale unadjusted prices onto freshly-adjusted ones (a
single-day pseudo-return of up to ~90% at the join). That would make the
name look like the highest-vol name in the universe until the window rolls
past it, corrupting the exact Garman-Klass input §13.5 depends on — precisely
the "flaky upstream source silently poisons features" failure PLAN §8 warns
about. So every incremental fetch requests ``_OVERLAP_DAYS`` of bars we
already have, and ``_detect_readjustment`` compares fetched-vs-stored closes
on the overlap; a mismatch beyond ``_DRIFT_TOLERANCE`` (0.5%) triggers an
automatic full ``period="max"`` re-fetch (delete-then-insert) for that symbol
— logged as a WARNING and surfaced in the summary dict's ``readjusted`` list,
never left silently spliced.

CACHE COUPLING (PLAN §13.2, documented here, NOT fixed by this module):
``xtrain.py:170`` keys its ~390MB cross-sectional feature-matrix cache on
this DB file's **mtime**, so every refresh here invalidates that cache and
``xtrain`` deletes all stale ``.xs_matrix_*.pkl`` files on its next rebuild
(a multi-minute rebuild). Re-keying that cache on content (e.g. max(ts) + row
count) instead of mtime is future work tracked in PLAN §13.2 — out of scope
for this task, which must not touch ``xtrain.py``.

Run:  cd apps/api && .venv/bin/python -m app.ml.universe
"""
from __future__ import annotations

import argparse
import logging
import math
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

import duckdb
import pandas as pd
import yfinance as yf

log = logging.getLogger("ml.universe")

# ~150 liquid large caps across all 11 GICS sectors (current membership —
# mild survivorship bias, see PLAN §13.8). Moved verbatim from
# scratchpad/fetch_universe.py, the one-shot script this module supersedes.
UNIVERSE = [
    # Tech / semis / software
    "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "CSCO", "ACN", "AMD", "INTC", "QCOM",
    "TXN", "IBM", "AMAT", "MU", "ADI", "LRCX", "KLAC", "INTU", "NOW", "PANW", "SNPS", "CDNS", "ANET",
    # Communication services
    "GOOGL", "META", "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS", "CHTR", "EA",
    # Consumer discretionary
    "AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "BKNG", "TJX", "ORLY", "GM", "F", "MAR", "CMG",
    # Consumer staples
    "WMT", "PG", "KO", "PEP", "COST", "PM", "MO", "MDLZ", "CL", "KMB", "GIS", "KHC", "TGT",
    # Health care
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "BMY", "AMGN", "CVS", "MDT",
    "GILD", "ISRG", "ELV", "VRTX",
    # Financials
    "BRK-B", "JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW", "AXP", "BLK", "SPGI", "CB", "PGR", "PNC",
    "USB", "COF",
    # Industrials
    "CAT", "HON", "UPS", "BA", "GE", "RTX", "UNP", "LMT", "DE", "MMM", "GD", "EMR", "ETN", "CSX",
    "NSC", "FDX",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO", "OXY", "WMB", "KMI",
    # Materials
    "LIN", "APD", "SHW", "FCX", "NEM", "ECL", "DOW", "NUE", "DD",
    # Utilities
    "NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "XEL",
    # Real estate
    "PLD", "AMT", "EQIX", "CCI", "PSA", "O", "SPG", "WELL",
]
UNIVERSE = sorted(set(UNIVERSE))

# Symbols that are NOT plain Yahoo equity/ETF tickers, so target_symbols()
# excludes them instead of silently mis-fetching. Mirrors the split
# app.ingest.alpaca.alpaca_symbol() already makes: crypto pairs use Alpaca's
# "/"-delimited notation (BTC/USD, ETH/USD) and the metals SPOT pseudo-symbols
# (XAU/XAG) are fed by gold-api.com, not an exchange ticker — see
# app.ingest.multiasset. GLD/SLV etc. ARE ordinary ETF tickers and pass
# through untouched (same as alpaca_symbol's "metal but not XAU/XAG -> plain
# ETF" branch).
_METAL_SPOT_PSEUDOSYMBOLS = {"XAU", "XAG"}

_TS_PRICE_SCHEMA = """CREATE TABLE IF NOT EXISTS ts_price (
    source VARCHAR, symbol VARCHAR, asset_class VARCHAR, ts TIMESTAMP,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE,
    PRIMARY KEY (source, symbol, ts));"""

# Split/dividend re-adjustment drift detection (see module docstring). Yahoo
# retroactively re-adjusts a symbol's ENTIRE history on a corporate action;
# an incremental fetch that starts exactly at max(ts) would then splice old
# unadjusted prices onto newly-adjusted ones. Requesting a small overlap and
# comparing closes on the overlapping dates catches this before it's stored.
_OVERLAP_DAYS = 7          # calendar days of overlap requested on incremental fetches
_DRIFT_TOLERANCE = 0.005   # relative close-price tolerance (0.5%) before a mismatch counts as re-adjustment, not noise


def _is_yahoo_fetchable(symbol: str) -> bool:
    s = symbol.upper()
    if "/" in s:  # crypto pair notation (BTC/USD, ETH/USD) -> not a yahoo ticker
        return False
    if s in _METAL_SPOT_PSEUDOSYMBOLS:  # COMEX spot proxies, not exchange-listed
        return False
    return True


def universe_db_path() -> str:
    """``data/ml/universe.duckdb`` — separate single-writer file, same
    convention as ``app.ml.snapshot.snapshots_db_path()``."""
    repo = Path(__file__).resolve().parents[4]
    ml_dir = repo / "data" / "ml"
    ml_dir.mkdir(parents=True, exist_ok=True)
    return str(ml_dir / "universe.duckdb")


def target_symbols(sqlite: Any = None, settings: Any = None) -> tuple[list[str], list[str]]:
    """Union of ``UNIVERSE``, ``settings.day_universe``, and the watchlist
    SQLite table's symbols, split into (included, excluded) — both sorted.

    ``excluded`` holds anything that isn't a plain Yahoo-fetchable ticker
    (crypto pairs, metals spot pseudo-symbols — see ``_is_yahoo_fetchable``)
    so the caller can log what was dropped instead of it silently vanishing
    or silently being fetched as garbage.

    ``sqlite`` is any object with ``fetchall(sql, params)`` (a
    ``SqliteStore`` in production); pass ``None`` to skip the watchlist leg
    (e.g. in tests that only care about ``UNIVERSE``/``day_universe``).
    """
    if settings is None:
        from app.config import get_settings

        settings = get_settings()

    raw: set[str] = set(UNIVERSE) | set(settings.day_universe)
    if sqlite is not None:
        raw |= {row[0] for row in sqlite.fetchall("SELECT symbol FROM watchlist")}

    included = sorted(s for s in raw if _is_yahoo_fetchable(s))
    excluded = sorted(s for s in raw if not _is_yahoo_fetchable(s))
    return included, excluded


def _fetch_with_retry(sym: str, start: str | None, max_attempts: int = 3) -> pd.DataFrame | None:
    """Sequential single-ticker pull with backoff — gentle discipline
    preserved from scratchpad/fetch_universe.py (threaded bulk pulls trigger
    Yahoo's burst throttle)."""
    for attempt in range(max_attempts):
        try:
            t = yf.Ticker(sym)
            if start is not None:
                return t.history(start=start, auto_adjust=True)
            return t.history(period="max", auto_adjust=True)
        except Exception:
            if attempt == max_attempts - 1:
                raise
            time.sleep(5 * (attempt + 1))
    return None  # unreachable


def _rows_from_history(sym: str, h: pd.DataFrame | None) -> list[tuple]:
    if h is None or h.empty:
        return []
    h = h.dropna(subset=["Close"]).reset_index()
    if h.empty:
        return []
    h["ts"] = pd.to_datetime(h["Date"]).dt.tz_localize(None).dt.normalize()
    return [
        (
            "yahoo", sym, "equity", r.ts.to_pydatetime(),
            float(r.Open), float(r.High), float(r.Low), float(r.Close),
            float(r.Volume) if pd.notna(r.Volume) else None,
        )
        for r in h.itertuples()
    ]


def _detect_readjustment(
    con: "duckdb.DuckDBPyConnection", sym: str, rows: list[tuple], prior_max
) -> tuple[bool, str | None]:
    """Compare freshly-fetched bars against what's already stored, for the
    dates that overlap (``ts <= prior_max``). A close that moved by more than
    ``_DRIFT_TOLERANCE`` means Yahoo retroactively re-adjusted this symbol's
    entire history (split/dividend) since our last fetch — see module
    docstring. Returns ``(drifted, human-readable detail-or-None)``.

    ``rows`` are tuples shaped like ``_rows_from_history``'s output:
    ``(source, symbol, asset_class, ts, open, high, low, close, volume)``.
    """
    overlap = {r[3]: r[7] for r in rows if r[3] <= prior_max}  # ts -> new close
    if not overlap:
        return False, None

    placeholders = ",".join("?" * len(overlap))
    stored = dict(
        con.execute(
            f"SELECT ts, close FROM ts_price WHERE source='yahoo' AND symbol=? "
            f"AND ts IN ({placeholders})",
            [sym, *overlap.keys()],
        ).fetchall()
    )
    for ts, new_close in sorted(overlap.items()):
        old_close = stored.get(ts)
        if old_close is None or new_close is None:
            continue  # nothing to compare (row not previously stored)
        if math.isnan(old_close) or math.isnan(new_close) or old_close == 0:
            continue  # guard zero/NaN closes — can't compute a relative drift
        drift = abs(new_close - old_close) / abs(old_close)
        if drift > _DRIFT_TOLERANCE:
            return True, (
                f"{ts.date()} stored close={old_close:.4f} fetched close={new_close:.4f} "
                f"drift={drift:.1%}"
            )
    return False, None


def refresh(
    symbols: Iterable[str] | None = None,
    *,
    full: bool = False,
    sleep_s: float = 1.5,
    max_symbols: int | None = None,
    db_path: str | None = None,
    fetcher: Callable[[str, str | None], pd.DataFrame | None] | None = None,
) -> dict:
    """Incrementally refresh daily OHLC into ``universe.duckdb``.

    For each symbol: look up ``max(ts)`` already stored (``source='yahoo'``)
    and fetch from ``_OVERLAP_DAYS`` before that date forward — not exactly
    ``max(ts)`` — so the response includes bars we already store. A symbol
    with no rows yet, or ``full=True``, gets a full ``period="max"`` pull
    (and, only for ``full=True``, a delete-then-refetch so a shrunk history —
    e.g. a corporate action — doesn't leave stale rows behind).

    The overlap exists to catch split/dividend re-adjustment drift (see
    module docstring): ``_detect_readjustment`` compares fetched-vs-stored
    closes on the overlapping dates, and a mismatch beyond
    ``_DRIFT_TOLERANCE`` triggers an automatic full re-fetch + delete-then-
    insert for that symbol only — never a silently spliced series.

    ``symbols`` defaults to ``target_symbols()``'s included set. ``fetcher``
    is the network injection point for tests: a callable ``(symbol,
    start_or_None) -> DataFrame`` shaped like ``yf.Ticker(...).history(...)``;
    defaults to a real gentle Yahoo pull via ``_fetch_with_retry``.

    Returns a summary dict: ``attempted``/``updated`` (int counts),
    ``failed``/``readjusted`` (lists of symbols — failed after retries, or
    caught mid-re-adjustment and auto-recovered), ``rows_inserted``,
    ``max_ts`` (new max(ts) across the whole table, or None if empty).
    """
    if symbols is None:
        symbols, _excluded = target_symbols()
    symbols = list(symbols)
    if max_symbols is not None:
        symbols = symbols[:max_symbols]

    fetch = fetcher or (lambda sym, start: _fetch_with_retry(sym, start))

    path = db_path or universe_db_path()
    con = duckdb.connect(path)
    con.execute(_TS_PRICE_SCHEMA)

    attempted = 0
    updated = 0
    failed: list[str] = []
    readjusted: list[str] = []
    rows_inserted = 0

    for i, sym in enumerate(symbols):
        attempted += 1
        prior_max = None
        if not full:
            r = con.execute(
                "SELECT max(ts) FROM ts_price WHERE source='yahoo' AND symbol=?", [sym]
            ).fetchone()
            prior_max = r[0] if r else None

        # full refresh, or a symbol we've never stored -> fetch everything.
        # Otherwise fetch from _OVERLAP_DAYS before max(ts), not max(ts)
        # itself, so the drift check below has overlapping bars to compare.
        if full or prior_max is None:
            start = None
        else:
            start = (prior_max.date() - timedelta(days=_OVERLAP_DAYS)).isoformat()

        try:
            h = fetch(sym, start)
        except Exception as exc:
            log.warning("universe refresh: %s FAILED after retries: %s", sym, exc)
            failed.append(sym)
            if i < len(symbols) - 1:
                time.sleep(sleep_s)
            continue

        rows = _rows_from_history(sym, h)
        force_full_write = full

        if rows and not full and prior_max is not None:
            drifted, detail = _detect_readjustment(con, sym, rows, prior_max)
            if drifted:
                log.warning(
                    "universe refresh: %s upstream history re-adjusted (%s) -> "
                    "forcing a full re-fetch to avoid storing a spliced series",
                    sym, detail,
                )
                readjusted.append(sym)
                time.sleep(sleep_s)  # gentle before the extra call for this symbol
                try:
                    h = fetch(sym, None)
                except Exception as exc:
                    log.warning(
                        "universe refresh: %s full re-fetch after drift FAILED: %s", sym, exc
                    )
                    failed.append(sym)
                    if i < len(symbols) - 1:
                        time.sleep(sleep_s)
                    continue
                rows = _rows_from_history(sym, h)
                force_full_write = True

        if rows:
            if force_full_write:
                # A forced full refetch (explicit full=True, or a detected
                # re-adjustment) clears prior rows first — a plain incremental
                # fetch must never delete history outside its fetch window.
                con.execute("DELETE FROM ts_price WHERE source='yahoo' AND symbol=?", [sym])
            con.executemany(
                "INSERT OR REPLACE INTO ts_price VALUES (?,?,?,?,?,?,?,?,?)", rows
            )
            rows_inserted += len(rows)
            updated += 1
        if i < len(symbols) - 1:
            time.sleep(sleep_s)

    con.execute("CHECKPOINT")
    new_max = con.execute("SELECT max(ts) FROM ts_price WHERE source='yahoo'").fetchone()[0]
    con.close()

    return {
        "attempted": attempted,
        "updated": updated,
        "failed": failed,
        "readjusted": readjusted,
        "rows_inserted": rows_inserted,
        "max_ts": str(new_max) if new_max is not None else None,
    }


async def run_universe_refresh_job(
    sqlite: Any = None,
    settings: Any = None,
    *,
    full: bool = False,
    sleep_s: float = 1.5,
    max_symbols: int | None = None,
) -> None:
    """Scheduler entrypoint (default-OFF). Resolves the target symbol set
    (``UNIVERSE ∪ day_universe ∪ watchlist``, Yahoo-fetchable only) and runs
    the incremental refresh in a thread so it never stalls the event loop —
    mirrors ``app.ml.snapshot.run_snapshot_job``'s executor + logging
    pattern."""
    import asyncio

    included, excluded = target_symbols(sqlite, settings)
    if excluded:
        log.info("universe refresh: excluding non-Yahoo symbols: %s", excluded)

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        None,
        lambda: refresh(included, full=full, sleep_s=sleep_s, max_symbols=max_symbols),
    )
    log.info(
        "universe refresh: %s/%s symbols updated, %s rows inserted, %s failed, "
        "%s re-adjusted, max(ts)=%s",
        res["updated"], res["attempted"], res["rows_inserted"], len(res["failed"]),
        len(res["readjusted"]), res["max_ts"],
    )
    if res["readjusted"]:
        log.warning(
            "universe refresh: upstream re-adjustment detected + auto-recovered for: %s",
            res["readjusted"],
        )


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="force full refetch (ignores stored max(ts))")
    ap.add_argument("--max-symbols", type=int, default=None, dest="max_symbols")
    ap.add_argument("--sleep", type=float, default=1.5, dest="sleep_s")
    a = ap.parse_args()

    from app.config import get_settings
    from app.db.sqlite import SqliteStore

    settings = get_settings()
    sq = SqliteStore(settings.sqlite_path)
    try:
        included, excluded = target_symbols(sq, settings)
    finally:
        sq.close()

    if excluded:
        print(f"excluding non-Yahoo symbols: {excluded}")
    print(f"target universe: {len(included)} symbols -> {universe_db_path()}")

    res = refresh(included, full=a.full, sleep_s=a.sleep_s, max_symbols=a.max_symbols)
    print(
        f"DONE: {res['updated']}/{res['attempted']} updated, {res['rows_inserted']} rows "
        f"inserted, {len(res['failed'])} failed, {len(res['readjusted'])} re-adjusted, "
        f"max(ts)={res['max_ts']}"
    )
    if res["readjusted"]:
        print(f"re-adjusted (auto-recovered): {res['readjusted']}")
    if res["failed"]:
        print(f"failed: {res['failed']}")
    return 1 if res["failed"] and res["updated"] == 0 else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(_main())
