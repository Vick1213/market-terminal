"""Tests for app.ml.vol_shadow (PLAN §13.9 step 4, §13.6 — Phase A shadow
annotations). Same plain-assert-function style as test_vol_scores.py /
test_vol_rank_tool.py (each ``test_*`` is a normal pytest-collectible
function; ``_run_all()`` is a ``pytest``-free fallback runner). Network-free:
temp DuckDB/SQLite files stand in for market.duckdb/app.db — the real DBs are
never opened for writes.

Run:  cd apps/api && .venv/bin/python -m app.ml.tests.test_vol_shadow
  or: cd apps/api && .venv/bin/python -m pytest app/ml/tests/test_vol_shadow.py -q
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ml.vol_shadow import (
    _MIN_REFERENCE_PANEL_N,
    _SCALE_CLIP,
    annotate_and_persist_swing,
    day_vol_shadow,
    ensure_shadow_table,
    fetch_vol_context,
    swing_vol_shadow,
    write_swing_shadow,
)

_SCORES_DDL = """
CREATE TABLE ml_vol_scores (
    ts                  TIMESTAMP NOT NULL,
    symbol              VARCHAR NOT NULL,
    horizon             INTEGER NOT NULL,
    estimator           VARCHAR NOT NULL,
    pred_vol            DOUBLE,
    level_admissible    BOOLEAN NOT NULL,
    calib_a             DOUBLE,
    calib_b             DOUBLE,
    rank                INTEGER,
    pctile              DOUBLE,
    in_reference_panel  BOOLEAN NOT NULL,
    n_obs               INTEGER,
    created_at          TIMESTAMP NOT NULL,
    PRIMARY KEY (ts, symbol, horizon)
);
"""

_NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _mkduck() -> DuckStore:
    tmp = tempfile.TemporaryDirectory()
    db = DuckStore(Path(tmp.name) / "market.duckdb")
    db._tmp = tmp  # keep the TemporaryDirectory alive as long as the store is
    db.execute(_SCORES_DDL)
    return db


def _mksqlite() -> SqliteStore:
    tmp = tempfile.TemporaryDirectory()
    db = SqliteStore(Path(tmp.name) / "app.db")
    db._tmp = tmp
    return db


def _row(symbol: str, *, horizon: int = 21, ts=_NOW, pred_vol: float = 0.02,
         level_admissible: bool = True, in_reference_panel: bool = True,
         rank: int | None = 1, pctile: float | None = 0.5, n_obs: int = 4000) -> tuple:
    return (ts, symbol, horizon, "har", pred_vol, level_admissible, 0.0, 1.0,
            rank, pctile, in_reference_panel, n_obs, _NOW)


def _insert(db: DuckStore, rows: list[tuple]) -> None:
    db.executemany(
        "INSERT INTO ml_vol_scores (ts, symbol, horizon, estimator, pred_vol, "
        "level_admissible, calib_a, calib_b, rank, pctile, in_reference_panel, "
        "n_obs, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _seed_panel(db: DuckStore, *, n: int = 20, pred_vol: float = 0.02, ts=_NOW,
                 skip_symbol: str | None = None) -> None:
    """A reference panel of `n` names all at the SAME pred_vol -> its median
    is exactly `pred_vol`, so scale-factor math in the tests below is exact."""
    rows = [
        _row(f"REF{i}", ts=ts, pred_vol=pred_vol)
        for i in range(n) if f"REF{i}" != skip_symbol
    ]
    _insert(db, rows)


# --- fetch_vol_context: fail-soft paths -----------------------------------------


def test_missing_table_is_fail_soft_not_raise() -> None:
    tmp = tempfile.TemporaryDirectory()
    db = DuckStore(Path(tmp.name) / "market.duckdb")  # ml_vol_scores never created
    try:
        ctx = fetch_vol_context(db, "AAPL")
    finally:
        db.close()
    assert ctx["available"] is False
    assert "ml_vol_scores unavailable" in ctx["reason"]


def test_no_score_for_symbol_is_fail_soft() -> None:
    db = _mkduck()
    try:
        _seed_panel(db)
        ctx = fetch_vol_context(db, "NOSUCHSYM")
    finally:
        db.close()
    assert ctx["available"] is False
    assert ctx["reason"] == "no_score"


def test_h5_level_not_admissible_is_fail_soft() -> None:
    db = _mkduck()
    try:
        _insert(db, [_row("AAPL", horizon=5, level_admissible=False)])
        ctx = fetch_vol_context(db, "AAPL", horizon=5)
    finally:
        db.close()
    assert ctx["available"] is False
    assert "not admissible" in ctx["reason"]


def test_stale_score_available_but_no_scale_factor() -> None:
    old_ts = _NOW - timedelta(days=14)  # comfortably > 5 trading days
    db = _mkduck()
    try:
        _seed_panel(db, ts=old_ts)
        _insert(db, [_row("AAPL", ts=old_ts, pred_vol=0.02)])
        ctx = fetch_vol_context(db, "AAPL")
    finally:
        db.close()
    assert ctx["available"] is True
    assert ctx["stale"] is True
    assert ctx["age_trading_days"] > 5
    assert ctx["scale_factor"] is None
    assert "stale" in ctx["reason"]


def test_insufficient_reference_panel_no_scale_factor() -> None:
    db = _mkduck()
    try:
        _seed_panel(db, n=_MIN_REFERENCE_PANEL_N - 5)  # below the min-panel floor
        _insert(db, [_row("AAPL", pred_vol=0.02)])
        ctx = fetch_vol_context(db, "AAPL")
    finally:
        db.close()
    assert ctx["available"] is True
    assert ctx["scale_factor"] is None
    assert "insufficient reference-panel" in ctx["reason"]


# --- fetch_vol_context: exact scale-factor math ---------------------------------


def test_scale_factor_is_panel_median_over_symbol_level() -> None:
    db = _mkduck()
    try:
        _seed_panel(db, n=20, pred_vol=0.02)  # median == 0.02 exactly
        _insert(db, [_row("AAPL", pred_vol=0.01)])  # half the panel median
        ctx = fetch_vol_context(db, "AAPL")
    finally:
        db.close()
    assert ctx["available"] is True
    assert ctx["panel_reference_pred_vol"] == 0.02
    assert abs(ctx["scale_factor"] - 2.0) < 1e-9  # 0.02/0.01 = 2.0, inside [0.25, 4.0]


def test_scale_factor_clipped_to_band() -> None:
    lo, hi = _SCALE_CLIP
    db = _mkduck()
    try:
        _seed_panel(db, n=20, pred_vol=0.02)
        # raw = 0.02/1.0 = 0.02, far below the clip floor.
        _insert(db, [_row("JUMPY", pred_vol=1.0)])
        ctx = fetch_vol_context(db, "JUMPY")
    finally:
        db.close()
    assert ctx["scale_factor"] == lo


# --- day_vol_shadow: counterfactual math -----------------------------------------


def _long_decision(**overrides) -> dict:
    d = {
        "symbol": "AAPL", "asset_class": "equity", "act": True,
        "notional": 1000.0,
        "legs": [{"role": "primary", "entry": 100.0, "sl_price": 99.0, "qty": 10}],
    }
    d.update(overrides)
    return d


def test_day_counterfactual_scales_qty_and_rechecks_fee_gate() -> None:
    db = _mkduck()
    try:
        _seed_panel(db, n=20, pred_vol=0.02)
        _insert(db, [_row("AAPL", pred_vol=0.01)])  # scale = 2.0
        out = day_vol_shadow(db, _long_decision(), 5.0)
    finally:
        db.close()

    assert out["available"] is True
    cf = out["counterfactual"]
    assert cf is not None
    assert cf["scale_factor"] == 2.0
    assert cf["stop_dist"] == 1.0            # |100 - 99|
    assert cf["actual_qty"] == 10.0
    assert cf["actual_risk_dollars"] == 10.0  # 1.0 * 10
    assert cf["actual_clears_fee_gate"] is True
    assert cf["cf_qty"] == 20.0               # floor(2000/100)
    assert cf["cf_notional"] == 2000.0
    assert cf["cf_risk_dollars"] == 20.0      # 1.0 * 20
    assert cf["cf_clears_fee_gate"] is True


def test_day_counterfactual_can_fall_below_fee_gate_after_shrinking() -> None:
    """PLAN §13.7's explicit warning: vol-scaling shrinks size, which can push
    risk_d = stop_dist * qty BELOW the $5 gate and silently kill a trade the
    bot actually took. Construct exactly that: a small actual qty + a scale
    clipped to the 0.25 floor collapses cf_qty to 0 whole shares."""
    db = _mkduck()
    try:
        _seed_panel(db, n=20, pred_vol=0.02)
        _insert(db, [_row("JUMPY", pred_vol=1.0)])  # scale clipped to 0.25
        decision = _long_decision(symbol="JUMPY", notional=100.0,
                                   legs=[{"role": "primary", "entry": 100.0,
                                          "sl_price": 94.0, "qty": 1}])
        out = day_vol_shadow(db, decision, 5.0)
    finally:
        db.close()

    cf = out["counterfactual"]
    assert cf["scale_factor"] == 0.25
    assert cf["actual_risk_dollars"] == 6.0   # 6.0 stop_dist * 1 share
    assert cf["actual_clears_fee_gate"] is True
    assert cf["cf_qty"] == 0.0                # floor(25/100) = 0 whole shares
    assert cf["cf_risk_dollars"] == 0.0
    assert cf["cf_clears_fee_gate"] is False, "vol-scaling must be able to trip the fee gate"


def test_day_counterfactual_not_computed_when_decision_not_acted() -> None:
    db = _mkduck()
    try:
        _seed_panel(db, n=20, pred_vol=0.02)
        _insert(db, [_row("AAPL", pred_vol=0.01)])
        skipped = _long_decision(act=False, legs=[])
        out = day_vol_shadow(db, skipped, 5.0)
    finally:
        db.close()
    assert out["available"] is True   # the score itself is still available/informational
    assert out["counterfactual"] is None
    assert out["counterfactual_reason"] == "decision_not_acted"


def test_day_counterfactual_never_raises_on_missing_table() -> None:
    tmp = tempfile.TemporaryDirectory()
    db = DuckStore(Path(tmp.name) / "market.duckdb")
    try:
        out = day_vol_shadow(db, _long_decision(), 5.0)
    finally:
        db.close()
    assert out["available"] is False
    assert out["counterfactual"] is None


def test_day_counterfactual_crypto_uses_fractional_qty() -> None:
    db = _mkduck()
    try:
        _seed_panel(db, n=20, pred_vol=0.02)
        _insert(db, [_row("BTC/USD", pred_vol=0.01)])  # scale = 2.0
        decision = {
            "symbol": "BTC/USD", "asset_class": "crypto", "act": True,
            "legs": [{"role": "primary", "entry": 50000.0, "sl_price": 49000.0, "notional": 500.0}],
        }
        out = day_vol_shadow(db, decision, 5.0)
    finally:
        db.close()
    cf = out["counterfactual"]
    assert cf["actual_notional"] == 500.0
    assert abs(cf["cf_notional"] - 1000.0) < 1e-6   # 500 * 2.0, no whole-unit floor for crypto
    assert abs(cf["cf_qty"] - (1000.0 / 50000.0)) < 1e-9


# --- swing_vol_shadow: cash-only / never-scale-up-a-sell -------------------------


def _buy_proposal(**overrides) -> dict:
    p = {
        "symbol": "AAPL", "side": "buy", "notional": 1000.0, "qty": None,
        "target_value": 1000.0, "current_value": 0.0, "delta_value": 1000.0,
        "max_loss_est": 100.0,   # stop_pct = 10%
    }
    p.update(overrides)
    return p


def _sell_proposal(**overrides) -> dict:
    p = {
        "symbol": "AAPL", "side": "sell", "notional": None, "qty": 10.0,
        "target_value": 0.0, "current_value": 1000.0, "delta_value": -1000.0,
        "max_loss_est": 100.0,
    }
    p.update(overrides)
    return p


def test_swing_buy_counterfactual_capped_by_available_cash_never_margin() -> None:
    db = _mkduck()
    try:
        _seed_panel(db, n=20, pred_vol=0.02)
        _insert(db, [_row("AAPL", pred_vol=0.01)])  # scale = 2.0 -> raw cf = 2000
        out = swing_vol_shadow(db, _buy_proposal(), available_cash=1500.0)
    finally:
        db.close()
    cf = out["counterfactual"]
    assert cf["scale_factor"] == 2.0
    assert cf["cf_notional"] == 1500.0, "must be capped at available_cash, never implying margin"
    assert cf["cash_capped"] is True
    assert cf["cf_stop_dollars"] == 150.0  # 1500 * 10% stop_pct


def test_swing_buy_counterfactual_uncapped_when_cash_is_ample() -> None:
    db = _mkduck()
    try:
        _seed_panel(db, n=20, pred_vol=0.02)
        _insert(db, [_row("AAPL", pred_vol=0.01)])
        out = swing_vol_shadow(db, _buy_proposal(), available_cash=100000.0)
    finally:
        db.close()
    cf = out["counterfactual"]
    assert cf["cf_notional"] == 2000.0
    assert cf["cash_capped"] is False


def test_swing_sell_counterfactual_never_scales_up_past_actual() -> None:
    """Never implies a bigger sell than the bot already planned (which is
    itself capped at currently-held sleeve shares) -- so never implies a
    short. scale=2.0 here must NOT double the sell qty."""
    db = _mkduck()
    try:
        _seed_panel(db, n=20, pred_vol=0.02)
        _insert(db, [_row("AAPL", pred_vol=0.01)])  # scale = 2.0
        out = swing_vol_shadow(db, _sell_proposal(), available_cash=None)
    finally:
        db.close()
    cf = out["counterfactual"]
    assert cf["scale_factor"] == 2.0
    assert cf["cf_qty"] == 10.0, "a sell counterfactual must never scale ABOVE the actual qty"


def test_swing_sell_counterfactual_scales_down() -> None:
    db = _mkduck()
    try:
        _seed_panel(db, n=20, pred_vol=0.02)
        _insert(db, [_row("JUMPY", pred_vol=1.0)])  # scale clipped to 0.25
        out = swing_vol_shadow(db, _sell_proposal(symbol="JUMPY"), available_cash=None)
    finally:
        db.close()
    cf = out["counterfactual"]
    assert cf["scale_factor"] == 0.25
    assert cf["cf_qty"] == 2.5


def test_swing_counterfactual_not_computed_when_stale() -> None:
    old_ts = _NOW - timedelta(days=14)
    db = _mkduck()
    try:
        _seed_panel(db, n=20, pred_vol=0.02, ts=old_ts)
        _insert(db, [_row("AAPL", ts=old_ts, pred_vol=0.01)])
        out = swing_vol_shadow(db, _buy_proposal(), available_cash=5000.0)
    finally:
        db.close()
    assert out["available"] is True
    assert out["counterfactual"] is None
    assert "stale" in out["counterfactual_reason"]


def test_swing_counterfactual_never_raises_on_missing_table() -> None:
    tmp = tempfile.TemporaryDirectory()
    db = DuckStore(Path(tmp.name) / "market.duckdb")
    try:
        out = swing_vol_shadow(db, _buy_proposal(), available_cash=5000.0)
    finally:
        db.close()
    assert out["available"] is False
    assert out["counterfactual"] is None


# --- persistence: ml_vol_shadow -------------------------------------------------


def test_ensure_shadow_table_and_write_roundtrip() -> None:
    sq = _mksqlite()
    try:
        ensure_shadow_table(sq)
        shadow = {"available": True, "ts": "2026-08-13", "age_trading_days": 0, "stale": False,
                  "estimator": "har", "pred_vol": 0.01, "pctile": 0.3, "rank": 10,
                  "in_reference_panel": True, "n_obs": 4000,
                  "panel_reference_pred_vol": 0.02, "panel_reference_n": 20,
                  "scale_factor": 2.0, "reason": None,
                  "counterfactual": {"cf_notional": 2000.0}, "counterfactual_reason": None}
        write_swing_shadow(sq, 42, "AAPL", "2026-08-13T00:00:00", "run1", shadow)
        row = sq.fetchone("SELECT * FROM ml_vol_shadow WHERE proposal_id = ?", [42])
    finally:
        sq.close()
    assert row is not None
    assert row["symbol"] == "AAPL"
    assert row["score_available"] == 1
    assert row["counterfactual_available"] == 1
    assert row["scale_factor"] == 2.0
    assert "2000.0" in row["detail"]


def test_annotate_and_persist_swing_writes_one_row_per_valid_proposal() -> None:
    duck = _mkduck()
    sq = _mksqlite()
    try:
        _seed_panel(duck, n=20, pred_vol=0.02)
        _insert(duck, [_row("AAPL", pred_vol=0.01), _row("MSFT", pred_vol=0.01)])
        proposals = [
            {**_buy_proposal(symbol="AAPL"), "id": 1},
            {**_buy_proposal(symbol="MSFT"), "id": 2},
            {**_buy_proposal(symbol="NOID"), "id": None},   # never persisted -> must be skipped, not crash
        ]
        n = annotate_and_persist_swing(duck, sq, proposals, 5000.0, "run1", "2026-08-13T00:00:00")
        rows = sq.fetchall("SELECT symbol FROM ml_vol_shadow ORDER BY symbol")
    finally:
        duck.close()
        sq.close()
    assert n == 2
    assert [r["symbol"] for r in rows] == ["AAPL", "MSFT"]


def _run_all() -> int:
    checks = [
        test_missing_table_is_fail_soft_not_raise,
        test_no_score_for_symbol_is_fail_soft,
        test_h5_level_not_admissible_is_fail_soft,
        test_stale_score_available_but_no_scale_factor,
        test_insufficient_reference_panel_no_scale_factor,
        test_scale_factor_is_panel_median_over_symbol_level,
        test_scale_factor_clipped_to_band,
        test_day_counterfactual_scales_qty_and_rechecks_fee_gate,
        test_day_counterfactual_can_fall_below_fee_gate_after_shrinking,
        test_day_counterfactual_not_computed_when_decision_not_acted,
        test_day_counterfactual_never_raises_on_missing_table,
        test_day_counterfactual_crypto_uses_fractional_qty,
        test_swing_buy_counterfactual_capped_by_available_cash_never_margin,
        test_swing_buy_counterfactual_uncapped_when_cash_is_ample,
        test_swing_sell_counterfactual_never_scales_up_past_actual,
        test_swing_sell_counterfactual_scales_down,
        test_swing_counterfactual_not_computed_when_stale,
        test_swing_counterfactual_never_raises_on_missing_table,
        test_ensure_shadow_table_and_write_roundtrip,
        test_annotate_and_persist_swing_writes_one_row_per_valid_proposal,
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
