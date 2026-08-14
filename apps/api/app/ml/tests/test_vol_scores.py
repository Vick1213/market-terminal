"""Tests for app.ml.vol_scores (PLAN §13.9 step 2).

Same plain-assert-function style as test_leakage.py / test_universe.py (each
``test_*`` is a normal pytest-collectible function; ``_run_all()`` is a
``pytest``-free fallback runner). Everything here is network-free: synthetic
OHLC is seeded into temp DuckDB files standing in for universe.duckdb /
market.duckdb -- the real ``data/market.duckdb`` is never opened for writes.

Run:  cd apps/api && .venv/bin/python -m app.ml.tests.test_vol_scores
  or: cd apps/api && .venv/bin/python -m pytest app/ml/tests/test_vol_scores.py -q
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

import app.ml.vol_scores as vol_scores
from app.db.duck import DuckStore
from app.ml.vol_scores import (
    _RawReader,
    score_universe,
    write_scores,
)

_TS_PRICE_DDL = """CREATE TABLE ts_price (
    source VARCHAR, symbol VARCHAR, asset_class VARCHAR, ts TIMESTAMP,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE)"""

# The horizon/parameter-aware PIT fit guard (`_min_fit_obs`) needs ~3,360 rows
# at h=21 for the 3-lookback HAR/HAR-63 specs (n_params=4) -- see vol_scores.py.
# Tests that want a symbol to legitimately clear the HAR fit at h=21 use this
# plus a buffer for lookback warm-up (63d) and the unresolved forward tail.
_N_ROWS_FOR_H21_HAR = vol_scores._min_fit_obs(vol_scores._HAR_LOOKBACKS, 21) + 300


# --- synthetic OHLC -----------------------------------------------------------


def _synth_ohlc(
    n: int, *, seed: int = 0, daily_vol: float = 0.012, start: str = "2015-01-02",
    start_price: float = 100.0,
) -> pd.DataFrame:
    """Deterministic geometric-random-walk OHLC with a controllable daily
    Garman-Klass-scale volatility -- high/low widened around open/close by
    ``daily_vol`` so GK vol comes out in the right ballpark."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n)
    rets = rng.normal(0.0002, daily_vol, size=n)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.r_[start_price, close[:-1]] * np.exp(rng.normal(0.0, daily_vol * 0.2, size=n))
    spread = np.abs(rng.normal(0.0, daily_vol * 0.6, size=n))
    high = np.maximum(open_, close) * (1.0 + spread)
    low = np.minimum(open_, close) * (1.0 - spread)
    low = np.clip(low, 0.01, None)
    return pd.DataFrame(
        {"ts": dates, "open": open_, "high": high, "low": low, "close": close}
    )


def _two_regime_ohlc(
    n1: int, n2: int, *, seed: int = 0, vol_lo: float = 0.006, vol_hi: float = 0.07,
    start: str = "2014-01-02",
) -> pd.DataFrame:
    """A calm regime (``vol_lo``) followed by a much stormier one (``vol_hi``),
    price-continuous across the join -- for the PIT / no-future-leakage test."""
    lo = _synth_ohlc(n1, seed=seed, daily_vol=vol_lo, start=start)
    hi = _synth_ohlc(
        n2, seed=seed + 1, daily_vol=vol_hi,
        start=(pd.Timestamp(start) + pd.tseries.offsets.BDay(n1)).date().isoformat(),
        start_price=float(lo["close"].iloc[-1]),
    )
    return pd.concat([lo, hi], ignore_index=True)


def _seed_db(path: str, frames: dict[str, pd.DataFrame]) -> None:
    con = duckdb.connect(path)
    con.execute(_TS_PRICE_DDL)
    for sym, df in frames.items():
        rows = [
            ("yahoo", sym, "equity", r.ts.to_pydatetime(), float(r.open), float(r.high),
             float(r.low), float(r.close), 1000.0)
            for r in df.itertuples()
        ]
        con.executemany("INSERT INTO ts_price VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.close()


def _reader(path: str) -> tuple[_RawReader, duckdb.DuckDBPyConnection]:
    con = duckdb.connect(path, read_only=True)
    return _RawReader(con), con


# --- 1. PIT boundary: no future data leaks into the fit ----------------------


def test_pit_truncation_matches_physically_shorter_history() -> None:
    """Scoring with ``as_of=D`` over a dataset that has MORE rows after D must
    be bit-identical to scoring a dataset that physically ENDS at D -- proof
    the truncation path used by every live/backtest caller cannot see rows
    beyond the scoring date."""
    full = _two_regime_ohlc(260, 260, seed=11)  # 520 rows total
    cutoff = full["ts"].iloc[259]  # last row of the calm regime

    with tempfile.TemporaryDirectory() as tmp:
        full_db = str(Path(tmp) / "full.duckdb")
        short_db = str(Path(tmp) / "short.duckdb")
        _seed_db(full_db, {"AAA": full})
        _seed_db(short_db, {"AAA": full[full["ts"] <= cutoff]})

        r_full, c_full = _reader(full_db)
        r_short, c_short = _reader(short_db)
        try:
            df_asof = score_universe(
                ["AAA"], (5, 21), uni_reader=r_full, main_reader=None,
                reference_panel=["AAA"], as_of=cutoff,
            )
            df_short = score_universe(
                ["AAA"], (5, 21), uni_reader=r_short, main_reader=None,
                reference_panel=["AAA"],
            )
        finally:
            c_full.close()
            c_short.close()

    assert not df_asof.empty and not df_short.empty
    a = df_asof.set_index("horizon").sort_index()
    b = df_short.set_index("horizon").sort_index()
    for h in (5, 21):
        assert a.loc[h, "estimator"] == b.loc[h, "estimator"]
        assert np.isclose(a.loc[h, "pred_vol"], b.loc[h, "pred_vol"], rtol=1e-9), (
            f"h={h}: as_of truncation produced a different prediction than "
            f"physically-shorter history -- the fit saw data past the cutoff"
        )
        assert a.loc[h, "n_obs"] == b.loc[h, "n_obs"]


def test_pit_forecast_does_not_anticipate_a_future_vol_regime_shift() -> None:
    """The economically-meaningful version of the same guarantee: scored
    BEFORE a large vol spike, the forecast must stay low -- it must not
    somehow "know" the storm is coming."""
    data = _two_regime_ohlc(300, 300, seed=3, vol_lo=0.005, vol_hi=0.08)
    cutoff = data["ts"].iloc[299]  # right at the regime boundary

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "regime.duckdb")
        _seed_db(db, {"AAA": data})
        reader, con = _reader(db)
        try:
            before = score_universe(
                ["AAA"], (21,), uni_reader=reader, main_reader=None,
                reference_panel=["AAA"], as_of=cutoff,
            )
            after = score_universe(
                ["AAA"], (21,), uni_reader=reader, main_reader=None,
                reference_panel=["AAA"],
            )
        finally:
            con.close()

    v_before = before.iloc[0]["pred_vol"]
    v_after = after.iloc[0]["pred_vol"]
    assert v_before < 0.02, f"pre-spike forecast already elevated: {v_before}"
    assert v_after > v_before * 3, (
        f"post-spike forecast ({v_after}) should be far above the pre-spike "
        f"forecast ({v_before}) once the storm is actually in history"
    )


# --- 2. fixed reference panel: adding a watchlist name must not reshuffle ---


def test_rank_is_against_the_fixed_reference_panel_only() -> None:
    n = _N_ROWS_FOR_H21_HAR  # comfortably enough for the HAR-63 fit to fire at h=21 too
    frames = {
        "LOWV": _synth_ohlc(n, seed=1, daily_vol=0.006),
        "MIDV": _synth_ohlc(n, seed=2, daily_vol=0.014),
        "HIGHV": _synth_ohlc(n, seed=3, daily_vol=0.03),
        "WATCH1": _synth_ohlc(n, seed=4, daily_vol=0.05),   # not in the panel
        "WATCH2": _synth_ohlc(n, seed=5, daily_vol=0.001),  # not in the panel
    }
    panel = ["LOWV", "MIDV", "HIGHV"]

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "uni.duckdb")
        _seed_db(db, frames)
        reader, con = _reader(db)
        try:
            base = score_universe(
                panel + ["WATCH1"], (5, 21), uni_reader=reader, main_reader=None,
                reference_panel=panel,
            )
            widened = score_universe(
                panel + ["WATCH1", "WATCH2"], (5, 21), uni_reader=reader, main_reader=None,
                reference_panel=panel,
            )
        finally:
            con.close()

    # Panel members rank in the expected vol order.
    h21 = base[base.horizon == 21].set_index("symbol")
    assert h21.loc["LOWV", "rank"] < h21.loc["MIDV", "rank"] < h21.loc["HIGHV", "rank"]
    assert h21.loc["HIGHV", "pctile"] == 1.0

    # WATCH1 is scored and ranked, but flagged as not a panel member.
    w1 = base[(base.symbol == "WATCH1") & (base.horizon == 21)].iloc[0]
    assert w1["in_reference_panel"] == False  # noqa: E712
    assert w1["rank"] is not None

    # The crux: adding WATCH2 to the scored set (still outside the panel)
    # must NOT change any panel member's rank/pctile.
    for sym in panel:
        b = base[(base.symbol == sym) & (base.horizon == 21)].iloc[0]
        w = widened[(widened.symbol == sym) & (widened.horizon == 21)].iloc[0]
        assert b["rank"] == w["rank"], f"{sym} rank reshuffled by adding an unrelated watchlist name"
        assert b["pctile"] == w["pctile"]


# --- 3. level_admissible: False at h=5, True at h=21 --------------------------


def test_level_admissible_false_at_h5_true_at_h21() -> None:
    n = _N_ROWS_FOR_H21_HAR
    frames = {"AAA": _synth_ohlc(n, seed=7, daily_vol=0.015)}
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "uni.duckdb")
        _seed_db(db, frames)
        reader, con = _reader(db)
        try:
            df = score_universe(
                ["AAA"], (5, 21), uni_reader=reader, main_reader=None,
                reference_panel=["AAA"],
            )
        finally:
            con.close()

    assert not df.empty
    h5 = df[df.horizon == 5]
    h21 = df[df.horizon == 21]
    assert len(h5) == 1 and len(h21) == 1
    assert h5.iloc[0]["level_admissible"] == False  # noqa: E712
    assert h21.iloc[0]["level_admissible"] == True  # noqa: E712


# --- 4. idempotent upsert -----------------------------------------------------


def test_write_scores_idempotent_upsert() -> None:
    df1 = pd.DataFrame([
        {
            "ts": pd.Timestamp("2026-08-13"), "symbol": "AAA", "horizon": 21,
            "estimator": "har", "pred_vol": 0.0150, "level_admissible": True,
            "calib_a": 0.001, "calib_b": 1.05, "rank": 10, "pctile": 0.5,
            "in_reference_panel": True, "n_obs": 400,
        },
        {
            "ts": pd.Timestamp("2026-08-13"), "symbol": "BBB", "horizon": 5,
            "estimator": "naive_gk", "pred_vol": 0.0220, "level_admissible": False,
            "calib_a": 0.0, "calib_b": 1.0, "rank": 3, "pctile": 0.9,
            "in_reference_panel": False, "n_obs": 50,
        },
    ])
    df2 = df1.copy()
    df2.loc[df2.symbol == "AAA", "pred_vol"] = 0.0999  # same key, changed value

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "market.duckdb"  # NEVER the real data/market.duckdb
        duck = DuckStore(db_path)
        try:
            n1 = write_scores(duck, df1)
            n2 = write_scores(duck, df2)
            rows = duck.fetchall(
                "SELECT symbol, pred_vol FROM ml_vol_scores ORDER BY symbol"
            )
            total = duck.fetchone("SELECT count(*) FROM ml_vol_scores")[0]
        finally:
            duck.close()

    assert n1 == 2 and n2 == 2
    assert total == 2, "re-run duplicated rows instead of replacing them"
    by_symbol = dict(rows)
    assert np.isclose(by_symbol["AAA"], 0.0999), "upsert did not replace the changed value"
    assert np.isclose(by_symbol["BBB"], 0.0220)


# --- 5. graceful handling of insufficient history -----------------------------


def test_insufficient_history_is_skipped_and_reported() -> None:
    frames = {"TOOSHORT": _synth_ohlc(10, seed=9)}  # far too little even for naive_gk
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "uni.duckdb")
        _seed_db(db, frames)
        reader, con = _reader(db)
        try:
            skips: list[dict] = []
            df = score_universe(
                ["TOOSHORT"], (5, 21), uni_reader=reader, main_reader=None,
                reference_panel=["TOOSHORT"], skip_report=skips,
            )
        finally:
            con.close()

    assert df.empty, "a 10-row symbol should not produce a scored row"
    assert any(s["symbol"] == "TOOSHORT" for s in skips)


def test_naive_gk_fallback_for_a_short_but_scoreable_symbol() -> None:
    """Enough history for naive_gk (needs ~11+ GK obs) but nowhere near the
    ~200-obs HAR/HAR-63 fit minimum -- must degrade to naive_gk, not crash."""
    frames = {"SHORTISH": _synth_ohlc(60, seed=10, daily_vol=0.02)}
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "uni.duckdb")
        _seed_db(db, frames)
        reader, con = _reader(db)
        try:
            df = score_universe(
                ["SHORTISH"], (5, 21), uni_reader=reader, main_reader=None,
                reference_panel=["SHORTISH"],
            )
        finally:
            con.close()

    assert not df.empty
    assert (df["estimator"] == "naive_gk").all()
    # Identity calibration -- no shrinkage was fit for the fallback path.
    assert (df["calib_a"] == 0.0).all()
    assert (df["calib_b"] == 1.0).all()
    # Still degrades gracefully, not a crash, and admissibility still holds.
    h5 = df[df.horizon == 5].iloc[0]
    h21 = df[df.horizon == 21].iloc[0]
    assert h5["level_admissible"] == False  # noqa: E712
    assert h21["level_admissible"] == True  # noqa: E712
    assert np.isfinite(h5["pred_vol"]) and h5["pred_vol"] > 0
    assert np.isfinite(h21["pred_vol"]) and h21["pred_vol"] > 0


# --- 6. pooled shrinkage calibration (fix for the per-symbol-noise defect) ---


def _fabricated_fits(n_symbols: int, n_obs_each: int, *, seed: int, real_fn) -> dict:
    """Hand-built ``fits_by_symbol`` for ``_pooled_calibration`` -- fabricated
    (fitted_log, actual_log) pairs so the true predicted->realised relationship
    is known exactly, rather than relying on organic HAR fits over synthetic
    prices (which can't be steered to an adversarial relationship on demand).
    """
    rng = np.random.default_rng(seed)
    fits: dict = {}
    for i in range(n_symbols):
        fitted_level = rng.uniform(0.008, 0.05, size=n_obs_each)
        real_level = np.clip(real_fn(fitted_level, rng), 1e-4, None)
        fits[f"SYM{i}"] = {
            21: {"har_fit": {"fitted_log": np.log(fitted_level), "actual_log": np.log(real_level)}}
        }
    return fits


def test_pooled_calibration_recovers_a_known_slope() -> None:
    true_a, true_b = 0.001, 1.15
    fits = _fabricated_fits(
        25, 80, seed=42,
        real_fn=lambda pred, rng: true_a + true_b * pred + rng.normal(0.0, 0.0005, size=pred.shape),
    )
    a, b, meta = vol_scores._pooled_calibration(fits, 21, window=1000)
    assert meta["fallback_reason"] is None, meta
    assert abs(b - true_b) < 0.05, f"recovered slope {b} too far from the true {true_b}"
    assert abs(a - true_a) < 0.01, f"recovered intercept {a} too far from the true {true_a}"


def test_pooled_calibration_rejects_negative_and_out_of_band_slopes() -> None:
    # Anti-correlated: higher predicted vol -> LOWER realised vol -> negative b.
    fits_neg = _fabricated_fits(
        25, 80, seed=7,
        real_fn=lambda pred, rng: 0.06 - 0.8 * pred + rng.normal(0.0, 0.0005, size=pred.shape),
    )
    a, b, meta = vol_scores._pooled_calibration(fits_neg, 21, window=1000)
    assert (a, b) == (0.0, 1.0), "a negative slope must NEVER be applied -- expected identity fallback"
    assert meta["fallback_reason"] is not None
    assert meta["raw_b"] is not None and meta["raw_b"] <= 0

    # Positive but far outside the [0.5, 2.0] sanity band -> also falls back.
    fits_big = _fabricated_fits(
        25, 80, seed=8,
        real_fn=lambda pred, rng: 3.0 * pred + rng.normal(0.0, 0.0002, size=pred.shape),
    )
    a2, b2, meta2 = vol_scores._pooled_calibration(fits_big, 21, window=1000)
    assert (a2, b2) == (0.0, 1.0)
    assert meta2["fallback_reason"] is not None
    assert meta2["raw_b"] is not None and meta2["raw_b"] > vol_scores._CALIB_BAND[1]


def test_score_universe_emits_raw_har_level_and_warns_when_pool_falls_back() -> None:
    """End-to-end: when the pooled calibration degenerates, score_universe
    must (a) log a WARNING with the reason, (b) report identity calib_a/b on
    the row, and (c) emit the RAW (uncalibrated) HAR level -- never a
    corrupted number."""
    n = _N_ROWS_FOR_H21_HAR
    frames = {"AAA": _synth_ohlc(n, seed=21, daily_vol=0.015)}

    class _Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.WARNING)
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    cap = _Capture()
    logger = logging.getLogger(vol_scores.log_name)
    logger.addHandler(cap)

    def _forced_fallback(fits_by_symbol, h, window):
        return 0.0, 1.0, {"n_symbols": 0, "n_obs": 0, "fallback_reason": "engineered test fallback"}

    original = vol_scores._pooled_calibration
    vol_scores._pooled_calibration = _forced_fallback
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "uni.duckdb")
            _seed_db(db, frames)
            reader, con = _reader(db)
            try:
                ohlc = vol_scores._load_ohlc_routed("AAA", reader, None)
                expected = vol_scores._raw_fits(ohlc, (21,))
                df = score_universe(
                    ["AAA"], (21,), uni_reader=reader, main_reader=None,
                    reference_panel=["AAA"],
                )
            finally:
                con.close()
    finally:
        vol_scores._pooled_calibration = original
        logger.removeHandler(cap)

    assert any("engineered test fallback" in r.getMessage() for r in cap.records), (
        "expected a WARNING logged when the pooled calibration falls back"
    )
    row = df.iloc[0]
    assert row["estimator"] == "har"
    assert row["calib_a"] == 0.0 and row["calib_b"] == 1.0
    expected_raw_level = float(np.exp(expected[21]["har_fit"]["pred_log"]))
    assert np.isclose(row["pred_vol"], expected_raw_level, rtol=1e-9), (
        "expected the RAW (uncalibrated) HAR level when the pool falls back"
    )


def test_pooled_calibration_is_uniform_across_symbols_at_a_given_ts() -> None:
    """Regression guard for the fix itself: at a fixed horizon, every symbol
    that used the HAR estimator must carry the IDENTICAL calib_a/calib_b --
    proof the calibration is genuinely pooled, not fit per symbol again."""
    n_symbols = vol_scores._CALIB_MIN_SYMBOLS + 5
    n = _N_ROWS_FOR_H21_HAR
    frames = {
        f"SYM{i}": _synth_ohlc(n, seed=100 + i, daily_vol=0.01 + 0.001 * i)
        for i in range(n_symbols)
    }
    panel = list(frames.keys())

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "uni.duckdb")
        _seed_db(db, frames)
        reader, con = _reader(db)
        try:
            df = score_universe(
                panel, (5, 21), uni_reader=reader, main_reader=None, reference_panel=panel,
            )
        finally:
            con.close()

    for h in (5, 21):
        har_rows = df[(df.horizon == h) & (df.estimator == "har")]
        assert len(har_rows) == n_symbols, "expected every synthetic symbol to get a valid HAR fit"
        assert har_rows["calib_a"].nunique() == 1, "calib_a differs across symbols -- not pooled"
        assert har_rows["calib_b"].nunique() == 1, "calib_b differs across symbols -- not pooled"
        assert har_rows["ts"].nunique() == 1, "fixture symbols share one date range/length"


# --- 7. horizon/parameter-aware PIT fit guard (raised from a flat row count) -


def test_raised_fit_guard_falls_back_to_naive_gk_below_new_threshold_at_h21() -> None:
    """A symbol with enough rows to clear the OLD flat 200-row guard but not
    the NEW horizon/parameter-aware one must fall back to naive_gk at h=21,
    not emit a thin/unstable HAR level -- the parallel-experiment failure mode
    (QLIKE 8.8-15.8 at n_fit=2,448 rows under a naive row-count floor)."""
    old_flat_guard = 200
    thin_n = old_flat_guard + 100  # clears the OLD guard comfortably
    new_guard_h21 = vol_scores._min_fit_obs(vol_scores._HAR_LOOKBACKS, 21)
    assert thin_n < new_guard_h21, (
        "test fixture must sit strictly between the old and the raised guard"
    )
    ample_n = new_guard_h21 + 300

    frames = {
        "THIN": _synth_ohlc(thin_n, seed=31, daily_vol=0.015),
        "AMPLE": _synth_ohlc(ample_n, seed=32, daily_vol=0.015),
    }
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "uni.duckdb")
        _seed_db(db, frames)
        reader, con = _reader(db)
        try:
            df = score_universe(
                ["THIN", "AMPLE"], (21,), uni_reader=reader, main_reader=None,
                reference_panel=["THIN", "AMPLE"],
            )
        finally:
            con.close()

    thin_row = df[df.symbol == "THIN"].iloc[0]
    ample_row = df[df.symbol == "AMPLE"].iloc[0]
    assert thin_row["estimator"] == "naive_gk", (
        f"expected the raised h=21 guard ({new_guard_h21} rows) to reject a "
        f"{thin_n}-row fit that would have cleared the old flat 200-row guard"
    )
    assert ample_row["estimator"] == "har"


def _run_all() -> int:
    checks = [
        test_pit_truncation_matches_physically_shorter_history,
        test_pit_forecast_does_not_anticipate_a_future_vol_regime_shift,
        test_rank_is_against_the_fixed_reference_panel_only,
        test_level_admissible_false_at_h5_true_at_h21,
        test_write_scores_idempotent_upsert,
        test_insufficient_history_is_skipped_and_reported,
        test_naive_gk_fallback_for_a_short_but_scoreable_symbol,
        test_pooled_calibration_recovers_a_known_slope,
        test_pooled_calibration_rejects_negative_and_out_of_band_slopes,
        test_score_universe_emits_raw_har_level_and_warns_when_pool_falls_back,
        test_pooled_calibration_is_uniform_across_symbols_at_a_given_ts,
        test_raised_fit_guard_falls_back_to_naive_gk_below_new_threshold_at_h21,
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
