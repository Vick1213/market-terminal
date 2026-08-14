"""Tests for app.ml.universe (PLAN §13.9 step 1).

Same plain-assert style as test_leakage.py / test_settings_store.py — no
pytest in the api venv (collectible unchanged once it's added). Everything
here is network-free: refresh() is exercised against a temp DuckDB file with
a stubbed fetcher, never yfinance.

Run:  cd apps/api && .venv/bin/python -m app.ml.tests.test_universe
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import duckdb
import pandas as pd

from app.ml.universe import (
    UNIVERSE,
    _DRIFT_TOLERANCE,
    _detect_readjustment,
    refresh,
    target_symbols,
)

_SCHEMA = (
    "CREATE TABLE ts_price (source VARCHAR, symbol VARCHAR, asset_class VARCHAR, "
    "ts TIMESTAMP, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, "
    "PRIMARY KEY (source, symbol, ts))"
)


class _FakeSettings:
    day_universe = ["ZZZTOP", "BTC/USD", "ETH/USD"]


class _FakeSqlite:
    """Minimal stand-in for SqliteStore — only fetchall(sql, params)."""

    def __init__(self, rows: list[tuple[str]]) -> None:
        self._rows = rows

    def fetchall(self, sql: str, params=None):
        return list(self._rows)


def _fake_history(dates: list[str], base: float = 100.0) -> pd.DataFrame:
    idx = pd.DatetimeIndex(pd.to_datetime(dates), name="Date")
    n = len(dates)
    closes = [base + i for i in range(n)]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 1 for c in closes],
            "Low": [c - 1 for c in closes],
            "Close": closes,
            "Volume": [1000.0] * n,
        },
        index=idx,
    )


def _seed(db_path: str, rows: list[tuple]) -> None:
    con = duckdb.connect(db_path)
    con.execute(_SCHEMA)
    con.executemany("INSERT INTO ts_price VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.close()


def _closes(db_path: str, symbol: str) -> list[tuple]:
    con = duckdb.connect(db_path, read_only=True)
    got = con.execute(
        "SELECT ts, close FROM ts_price WHERE symbol=? ORDER BY ts", [symbol]
    ).fetchall()
    con.close()
    return got


# --- target_symbols: union + non-Yahoo exclusion ----------------------------


def test_target_symbols_union_and_exclusion() -> None:
    sq = _FakeSqlite([("WATCHONLY",), ("XAU",), ("GLD",), ("AAPL",)])
    included, excluded = target_symbols(sq, _FakeSettings())

    # union: UNIVERSE, day_universe, and watchlist symbols all represented.
    assert "AAPL" in included  # in UNIVERSE and watchlist
    assert "ZZZTOP" in included  # day_universe-only
    assert "WATCHONLY" in included  # watchlist-only
    assert "GLD" in included  # metal ETF -> ordinary Yahoo ticker, NOT excluded
    assert set(UNIVERSE) <= set(included) | set(excluded)

    # exclusion: crypto pairs and metal spot pseudo-symbols are not Yahoo tickers.
    assert "BTC/USD" in excluded
    assert "ETH/USD" in excluded
    assert "XAU" in excluded
    assert "BTC/USD" not in included
    assert "XAU" not in included

    # no overlap, no duplicates.
    assert set(included).isdisjoint(excluded)
    assert len(included) == len(set(included))


def test_target_symbols_without_sqlite() -> None:
    # sqlite=None skips the watchlist leg entirely rather than raising.
    included, excluded = target_symbols(None, _FakeSettings())
    assert "AAPL" in included
    assert "ZZZTOP" in included
    assert "BTC/USD" in excluded


# --- refresh: incremental date logic (with overlap window) -----------------


def test_refresh_incremental_start_dates() -> None:
    calls: list[tuple[str, str | None]] = []

    def stub_fetcher(sym: str, start: str | None):
        calls.append((sym, start))
        if sym == "AAA":
            # Overlap bar (2024-01-05) matches the seeded close exactly (no
            # drift) + 2 NEW bars beyond what's stored.
            return _fake_history(["2024-01-05", "2024-01-06", "2024-01-07"], base=100.0)
        return _fake_history(["2020-01-02", "2020-01-03"])  # BBB: "period=max" stand-in

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "universe.duckdb")

        # Seed AAA with prior history through 2024-01-05 (close=100 on -05,
        # matching the stub's overlap bar -> no drift); BBB has no rows.
        _seed(
            db_path,
            [
                ("yahoo", "AAA", "equity", pd.Timestamp("2024-01-04"), 99.0, 100.0, 98.0, 99.0, 1000.0),
                ("yahoo", "AAA", "equity", pd.Timestamp("2024-01-05"), 100.0, 101.0, 99.0, 100.0, 1000.0),
            ],
        )

        res = refresh(["AAA", "BBB"], db_path=db_path, fetcher=stub_fetcher, sleep_s=0.0)

        # Incremental fetch requests _OVERLAP_DAYS (7) before max(ts), not
        # max(ts) itself; a never-seen symbol still gets a full period="max" pull.
        assert dict(calls) == {"AAA": "2023-12-29", "BBB": None}
        assert res["attempted"] == 2
        assert res["updated"] == 2
        assert res["failed"] == []
        assert res["readjusted"] == []  # overlap bar matched -> no drift
        assert res["rows_inserted"] == 5  # AAA: 3 fetched rows, BBB: 2 fetched rows

        aaa_rows = _closes(db_path, "AAA")
        # 2 pre-seeded (2024-01-04, -05) + 2 NEW fetched (-06, -07); -05 was
        # re-fetched (inside the overlap window) and replaced, not duplicated.
        assert len(aaa_rows) == 4


# --- refresh: idempotent upsert ---------------------------------------------


def test_refresh_idempotent_upsert() -> None:
    call_count = {"n": 0}

    def stub_fetcher(sym: str, start: str | None):
        call_count["n"] += 1
        return _fake_history(["2024-01-01", "2024-01-02", "2024-01-03"])

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "universe.duckdb")

        refresh(["AAA"], db_path=db_path, fetcher=stub_fetcher, sleep_s=0.0)
        refresh(["AAA"], db_path=db_path, fetcher=stub_fetcher, sleep_s=0.0)

        con = duckdb.connect(db_path, read_only=True)
        rows = con.execute(
            "SELECT count(*) FROM ts_price WHERE symbol='AAA'"
        ).fetchone()[0]
        dupes = con.execute(
            "SELECT source, symbol, ts, count(*) c FROM ts_price "
            "GROUP BY 1,2,3 HAVING c > 1"
        ).fetchall()
        con.close()

        assert call_count["n"] == 2  # both refreshes actually ran the fetcher
        assert rows == 3  # same 3 bars re-fetched -> replaced, never duplicated
        assert dupes == []


def test_refresh_full_forces_refetch_and_clears_stale_rows() -> None:
    def stub_fetcher(sym: str, start: str | None):
        assert start is None  # full=True always uses period="max"
        return _fake_history(["2024-02-01"])  # shrunk vs. the seeded history

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "universe.duckdb")
        _seed(
            db_path,
            [
                ("yahoo", "AAA", "equity", d, 1.0, 1.0, 1.0, 1.0, 1.0)
                for d in pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"])
            ],
        )

        refresh(["AAA"], full=True, db_path=db_path, fetcher=stub_fetcher, sleep_s=0.0)

        assert len(_closes(db_path, "AAA")) == 1  # stale rows cleared


def test_refresh_records_failures_without_raising() -> None:
    def stub_fetcher(sym: str, start: str | None):
        raise RuntimeError("network down")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "universe.duckdb")
        res = refresh(["AAA"], db_path=db_path, fetcher=stub_fetcher, sleep_s=0.0)
        assert res["failed"] == ["AAA"]
        assert res["updated"] == 0
        assert res["rows_inserted"] == 0


# --- split/dividend re-adjustment drift detection ---------------------------


def test_refresh_detects_split_and_recovers_full_history() -> None:
    """A 2:1 split: the overlap fetch comes back at half the stored price.
    refresh() must detect it, force a full re-fetch, and end up with the
    fully-adjusted series (no old-scale rows, no discontinuity)."""

    def stub_fetcher(sym: str, start: str | None):
        if start is None:
            # Full period="max" pull after detection: entire history on the
            # NEW (post-split) scale.
            return _fake_history(
                ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"],
                base=99.0,
            )
        # Overlap window: Yahoo already reports 01-03 on the NEW scale (~100)
        # even though our stored copy of 01-03 is still the OLD scale (~200).
        return _fake_history(["2024-01-03", "2024-01-04", "2024-01-05"], base=100.0)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "universe.duckdb")
        # Pre-split (unadjusted) history: close ~200 on the last stored date.
        _seed(
            db_path,
            [
                ("yahoo", "SPLT", "equity", pd.Timestamp("2024-01-01"), 198.0, 199.0, 197.0, 198.0, 500.0),
                ("yahoo", "SPLT", "equity", pd.Timestamp("2024-01-02"), 199.0, 200.0, 198.0, 199.0, 500.0),
                ("yahoo", "SPLT", "equity", pd.Timestamp("2024-01-03"), 200.0, 201.0, 199.0, 200.0, 500.0),
            ],
        )

        res = refresh(["SPLT"], db_path=db_path, fetcher=stub_fetcher, sleep_s=0.0)

        assert res["readjusted"] == ["SPLT"]
        assert res["failed"] == []
        assert res["updated"] == 1

        got = _closes(db_path, "SPLT")
        expected = [99.0, 100.0, 101.0, 102.0, 103.0]  # base=99.0, 5 bars, no old-scale survivors
        assert [round(c, 6) for _, c in got] == expected

        # no discontinuity: every consecutive day's pseudo-return is small
        # (the ~90%-jump failure mode this whole feature exists to prevent).
        rets = [
            abs(expected[i + 1] - expected[i]) / expected[i] for i in range(len(expected) - 1)
        ]
        assert max(rets) < 0.02


def test_refresh_no_drift_skips_full_refetch() -> None:
    """Regression guard: a within-tolerance mismatch (float noise, not a
    real re-adjustment) must NOT trigger a second start=None fetcher call —
    that would refetch every one of the ~150 universe names in full, daily."""
    calls: list[tuple[str, str | None]] = []

    def stub_fetcher(sym: str, start: str | None):
        calls.append((sym, start))
        return _fake_history(["2024-01-03", "2024-01-04"], base=100.02)  # 0.02% off stored

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "universe.duckdb")
        _seed(
            db_path,
            [("yahoo", "AAA", "equity", pd.Timestamp("2024-01-03"), 100.0, 100.0, 100.0, 100.0, 1000.0)],
        )

        res = refresh(["AAA"], db_path=db_path, fetcher=stub_fetcher, sleep_s=0.0)

        assert res["readjusted"] == []
        # exactly ONE fetcher call for AAA — no extra full re-fetch call.
        assert calls == [("AAA", "2023-12-27")]


def test_detect_readjustment_boundary() -> None:
    """Drift just under _DRIFT_TOLERANCE does not trigger; just over does."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "boundary.duckdb")
        con = duckdb.connect(db_path)
        con.execute(_SCHEMA)
        ts = pd.Timestamp("2024-01-03").to_pydatetime()
        old_close = 100.0
        con.execute(
            "INSERT INTO ts_price VALUES (?,?,?,?,?,?,?,?,?)",
            ["yahoo", "AAA", "equity", ts, old_close, old_close, old_close, old_close, 1000.0],
        )

        just_under = old_close * (1 + _DRIFT_TOLERANCE * 0.9)
        just_over = old_close * (1 + _DRIFT_TOLERANCE * 1.1)
        rows_under = [("yahoo", "AAA", "equity", ts, 0.0, 0.0, 0.0, just_under, 0.0)]
        rows_over = [("yahoo", "AAA", "equity", ts, 0.0, 0.0, 0.0, just_over, 0.0)]

        drifted_under, _ = _detect_readjustment(con, "AAA", rows_under, ts)
        drifted_over, detail_over = _detect_readjustment(con, "AAA", rows_over, ts)
        con.close()

        assert drifted_under is False
        assert drifted_over is True
        assert detail_over is not None


def _run_all() -> int:
    checks = [
        test_target_symbols_union_and_exclusion,
        test_target_symbols_without_sqlite,
        test_refresh_incremental_start_dates,
        test_refresh_idempotent_upsert,
        test_refresh_full_forces_refetch_and_clears_stale_rows,
        test_refresh_records_failures_without_raising,
        test_refresh_detects_split_and_recovers_full_history,
        test_refresh_no_drift_skips_full_refetch,
        test_detect_readjustment_boundary,
    ]
    failed = 0
    for c in checks:
        try:
            c()
            print(f"PASS  {c.__name__}")
        except Exception as exc:  # noqa: BLE001 - test harness reporting
            failed += 1
            print(f"FAIL  {c.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
