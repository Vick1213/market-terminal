"""Point-in-time snapshot logger (PLAN §12 lever #3) — the compounding fix.

Every run appends, keyed by ``snapshot_date``, the value of every signal *as it
is knowable today*:

  * the engineered ML feature vector (latest row per asset from the leakage-safe
    ``features.build_model_matrix``), and
  * every raw ``ts_macro`` series' current value (captures the as-of value of
    *revised* series before the revision overwrites it — the same leak ALFRED
    fixed for NFCI, now captured going-forward for ALL series), and
  * recent-only aggregates (news/FinBERT sentiment, retail, divergence) that have
    NO usable history today and are therefore NOT backtestable — but logged daily
    they accrete into a true PIT training set in 12–24 months.

Why this matters: our richest signals (FinBERT news, GEX, Kalshi, divergence)
started ingesting ~weeks ago, so they're ~99.9% NaN over any backtest and were
correctly excluded from the models. They can never be reconstructed historically
— but from today we can *record* them as-known. This is the only way they ever
become model inputs honestly.

Reads the live market DB READ-ONLY and writes a SEPARATE
``data/ml/snapshots.duckdb`` (its own single-writer file), so it never contends
with the terminal's writer. Ships **default-OFF** as a scheduler job, exactly
like the trading bots; run the CLI to seed/backfill-from-today manually.

Run:  cd apps/api && .venv/bin/python -m app.ml.snapshot
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


def _ensure(con) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS ml_feature_snapshots (
            snapshot_date DATE NOT NULL,
            asset    VARCHAR NOT NULL,   -- ticker, or '_macro' / '_recent' for broadcast
            feature  VARCHAR NOT NULL,
            value    DOUBLE,
            captured_at TIMESTAMP,
            PRIMARY KEY (snapshot_date, asset, feature)
        );"""
    )


def _recent_only_aggregates(reader) -> dict[str, float]:
    """Best-effort scalars from the recent-only tables (skipped if absent/empty).
    These are the signals with no backtestable history — the whole point of the
    logger is to start accruing them. Each guarded so a missing table is a no-op.
    """
    out: dict[str, float] = {}

    def one(sql, params=None):
        try:
            r = reader.fetchall(sql, params or [])
            return float(r[0][0]) if r and r[0] and r[0][0] is not None else None
        except Exception:
            return None

    # FinBERT news sentiment, month-to-date and trailing 5 days (score in [-1,1]).
    v = one("SELECT avg(score) FROM ts_sentiment WHERE ts >= ?",
            [date.today().replace(day=1)])
    if v is not None:
        out["news_sentiment_mtd"] = v
    v = one("SELECT avg(score) FROM ts_sentiment WHERE ts >= current_date - 5")
    if v is not None:
        out["news_sentiment_5d"] = v
    # Divergence engine latest score, if present.
    v = one("SELECT score FROM divergence_snapshots ORDER BY ts DESC LIMIT 1")
    if v is not None:
        out["divergence_latest"] = v
    # Retail message sentiment, trailing window, if present.
    v = one("SELECT avg(score) FROM retail_messages WHERE ts >= current_date - 5")
    if v is not None:
        out["retail_sentiment_5d"] = v
    return out


def take_snapshot(reader, snap_db: str, snapshot_date: date | None = None) -> dict:
    """Capture today's knowable signal vector into the snapshot store.

    ``reader`` is any object with ``fetchall(sql, params)`` — pass the live
    DuckStore from the scheduler (avoids a second read-only open of the
    single-writer DB in the same process) or a read-only connection wrapper from
    the CLI. Writes a SEPARATE ``snap_db`` file on its own connection.
    """
    from app.ml.features import build_model_matrix

    snapshot_date = snapshot_date or date.today()
    captured = datetime.now(timezone.utc)
    rows: list[tuple] = []

    # 1. Engineered ML feature vector — latest knowable row per asset.
    matrix, _report = build_model_matrix(reader)
    if not matrix.empty:
        last_date = matrix.index.get_level_values("date").max()
        latest = matrix.xs(last_date, level="date")
        for asset, srow in latest.iterrows():
            for feat, val in srow.items():
                if pd.notna(val) and np.isfinite(val):
                    rows.append((snapshot_date, asset, feat, float(val), captured))

    # 2. Raw ts_macro current value per series (captures as-of value of revised
    #    series before the next revision overwrites it).
    for sid, val in reader.fetchall(
        "SELECT series_id, last(value ORDER BY ts) FROM ts_macro "
        "WHERE value IS NOT NULL GROUP BY series_id"):
        if val is not None:
            rows.append((snapshot_date, "_macro", sid, float(val), captured))

    # 3. Recent-only aggregates (the irrecoverable ones).
    for k, v in _recent_only_aggregates(reader).items():
        rows.append((snapshot_date, "_recent", k, v, captured))

    snap = duckdb.connect(snap_db)
    _ensure(snap)
    snap.executemany(
        "INSERT OR REPLACE INTO ml_feature_snapshots "
        "(snapshot_date, asset, feature, value, captured_at) VALUES (?,?,?,?,?)",
        rows,
    )
    n_dates = snap.execute(
        "SELECT count(DISTINCT snapshot_date) FROM ml_feature_snapshots").fetchone()[0]
    snap.close()
    return {"snapshot_date": str(snapshot_date), "rows": len(rows),
            "total_snapshot_dates": int(n_dates)}


def snapshots_db_path() -> str:
    repo = Path(__file__).resolve().parents[4]
    snap_dir = repo / "data" / "ml"
    snap_dir.mkdir(parents=True, exist_ok=True)
    return str(snap_dir / "snapshots.duckdb")


async def run_snapshot_job(reader) -> None:
    """Scheduler entrypoint (default-OFF). Runs the (blocking) snapshot in a
    thread so it never stalls the event loop; reuses the live DB reader."""
    import asyncio
    import logging

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, take_snapshot, reader, snapshots_db_path())
    logging.getLogger("ml.snapshot").info(
        "ml snapshot %s: %s values (%s dates total)",
        res["snapshot_date"], res["rows"], res["total_snapshot_dates"])


if __name__ == "__main__":
    repo = Path(__file__).resolve().parents[4]
    con = duckdb.connect(str(repo / "data" / "market.duckdb"), read_only=True)

    class _RO:
        def fetchall(self, sql, params=None):
            return con.execute(sql, params or []).fetchall()

    res = take_snapshot(_RO(), snapshots_db_path())
    print(f"snapshot {res['snapshot_date']}: {res['rows']} signal values logged "
          f"({res['total_snapshot_dates']} snapshot date(s) total)")
