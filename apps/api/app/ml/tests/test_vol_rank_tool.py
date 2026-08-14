"""Tests for the strategist `vol_rank` tool (PLAN §13.9 step 3).

Same plain-assert-function style as test_leakage.py / test_universe.py /
test_vol_scores.py (each ``test_*`` is a normal pytest-collectible function;
``_run_all()`` is a ``pytest``-free fallback runner). Network-free: a temp
DuckDB file stands in for ``data/market.duckdb`` -- the real DB is never
opened for writes. This module intentionally does NOT import
``app.ml.vol_scores`` (the tool itself doesn't either -- see the "vol_rank"
section of strategist_tools.py) so it stays decoupled from that
concurrently-developed module's internals; rows are inserted directly against
the ``ml_vol_scores`` DDL the two modules share by convention.

Run:  cd apps/api && .venv/bin/python -m app.ml.tests.test_vol_rank_tool
  or: cd apps/api && .venv/bin/python -m pytest app/ml/tests/test_vol_rank_tool.py -q
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db.duck import DuckStore
from app.edge.strategist_tools import TOOLS, _tool_vol_rank

_DDL = """
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


def _mkdb() -> DuckStore:
    tmp = tempfile.TemporaryDirectory()
    db = DuckStore(Path(tmp.name) / "market.duckdb")
    db._tmp = tmp  # keep the TemporaryDirectory alive as long as the store is
    db.execute(_DDL)
    return db


def _row(
    symbol: str, horizon: int, *, ts: datetime, estimator: str = "har",
    pred_vol: float = 0.015, level_admissible: bool | None = None,
    rank: int | None = 1, pctile: float | None = 0.5,
    in_reference_panel: bool = True, n_obs: int = 4000,
) -> tuple:
    if level_admissible is None:
        level_admissible = horizon == 21
    return (
        ts, symbol, horizon, estimator, pred_vol, level_admissible,
        0.001, 1.1, rank, pctile, in_reference_panel, n_obs, _NOW,
    )


def _insert(db: DuckStore, rows: list[tuple]) -> None:
    db.executemany(
        "INSERT INTO ml_vol_scores (ts, symbol, horizon, estimator, pred_vol, "
        "level_admissible, calib_a, calib_b, rank, pctile, in_reference_panel, "
        "n_obs, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _call(db: DuckStore, args: dict) -> dict:
    return _tool_vol_rank(db, None, {}, args)


# --- registration -------------------------------------------------------------


def test_vol_rank_registered_in_tools() -> None:
    assert "vol_rank" in TOOLS
    tool = TOOLS["vol_rank"]
    assert tool.executor is _tool_vol_rank
    desc = tool.description
    # The description is shown verbatim to the LLM -- it must state the args
    # schema and the h=5 rank-only constraint (PLAN §13.9 step 3 deliverable 3).
    assert "symbol" in desc and "top" in desc and "bottom" in desc
    assert "h=5" in desc and "rank" in desc.lower()


# --- single-symbol lookup ------------------------------------------------------


def test_single_symbol_lookup_h21_exposes_pred_vol_as_admissible() -> None:
    db = _mkdb()
    try:
        _insert(db, [_row("AAPL", 21, ts=_NOW, pred_vol=0.0143, pctile=0.71, rank=40)])
        out = _call(db, {"symbol": "aapl", "horizon": 21})
    finally:
        db.close()

    assert out["symbol"] == "AAPL"  # normalizes case
    assert out["horizon"] == 21
    assert out["level_admissible"] is True
    assert out["pred_vol"] == 0.0143
    assert "pred_vol_rank_only" not in out
    assert out["pctile"] == 0.71
    assert out["rank"] == 40
    assert out["in_reference_panel"] is True
    assert "panel_note" not in out
    assert out["age_trading_days"] == 0
    assert out["stale"] is False
    assert "staleness_note" not in out


def test_single_symbol_lookup_defaults_to_horizon_21() -> None:
    db = _mkdb()
    try:
        _insert(db, [
            _row("MSFT", 5, ts=_NOW, pred_vol=0.02),
            _row("MSFT", 21, ts=_NOW, pred_vol=0.012),
        ])
        out = _call(db, {"symbol": "MSFT"})
    finally:
        db.close()
    assert out["horizon"] == 21


def test_unknown_symbol_returns_error_dict_not_raise() -> None:
    db = _mkdb()
    try:
        out = _call(db, {"symbol": "ZZZNOPE", "horizon": 21})
    finally:
        db.close()
    assert "error" in out
    assert out["symbol"] == "ZZZNOPE"


# --- h=5 level suppression / labelling (PLAN §13.3) ----------------------------


def test_h5_level_is_labelled_rank_only_not_presented_as_pred_vol() -> None:
    db = _mkdb()
    try:
        _insert(db, [_row("NVDA", 5, ts=_NOW, pred_vol=0.031, level_admissible=False)])
        out = _call(db, {"symbol": "NVDA", "horizon": 5})
    finally:
        db.close()

    assert out["level_admissible"] is False
    assert "pred_vol" not in out, "h=5 must not expose a bare pred_vol key"
    assert out["pred_vol_rank_only"] == 0.031
    assert "level_note" in out
    assert "not" in out["level_note"].lower() and "sizing" in out["level_note"].lower()


def test_h21_never_gets_the_rank_only_label() -> None:
    db = _mkdb()
    try:
        _insert(db, [_row("NVDA", 21, ts=_NOW, pred_vol=0.02, level_admissible=True)])
        out = _call(db, {"symbol": "NVDA", "horizon": 21})
    finally:
        db.close()
    assert "pred_vol_rank_only" not in out
    assert "level_note" not in out


def test_bad_horizon_raises_value_error() -> None:
    db = _mkdb()
    try:
        raised = False
        try:
            _call(db, {"symbol": "AAPL", "horizon": 10})
        except ValueError:
            raised = True
        assert raised, "horizon outside {5, 21} must raise ValueError, not silently coerce"
    finally:
        db.close()


def test_non_integer_top_raises_value_error() -> None:
    db = _mkdb()
    try:
        raised = False
        try:
            _call(db, {"top": "lots"})
        except ValueError:
            raised = True
        assert raised
    finally:
        db.close()


# --- top / bottom cross-section listing -----------------------------------------


def test_top_bottom_listing_orders_by_pctile() -> None:
    # 5 names so a top=2/bottom=2 request (4 total) fits inside the panel
    # with room to spare -- purely an ordering test, no truncation involved
    # (truncation/non-overlap behavior is covered separately below).
    db = _mkdb()
    try:
        rows = [
            _row("LOWV", 21, ts=_NOW, pctile=0.02, rank=1, pred_vol=0.005),
            _row("LOW2", 21, ts=_NOW, pctile=0.20, rank=30, pred_vol=0.009),
            _row("MIDV", 21, ts=_NOW, pctile=0.50, rank=70, pred_vol=0.015),
            _row("HIGH2", 21, ts=_NOW, pctile=0.80, rank=120, pred_vol=0.04),
            _row("HIGHV", 21, ts=_NOW, pctile=0.98, rank=145, pred_vol=0.06),
        ]
        _insert(db, rows)
        out = _call(db, {"horizon": 21, "top": 2, "bottom": 2})
    finally:
        db.close()

    assert out["horizon"] == 21
    assert out["n_scored"] == 5
    assert "extremes_note" not in out, "top+bottom fit inside the panel -- no truncation expected"
    top_syms = [r["symbol"] for r in out["top"]]
    bottom_syms = [r["symbol"] for r in out["bottom"]]
    assert top_syms == ["HIGHV", "HIGH2"], "top must be highest-pctile first"
    assert bottom_syms == ["LOWV", "LOW2"], "bottom must be lowest-pctile first"
    assert set(top_syms).isdisjoint(bottom_syms), "top and bottom must never overlap"
    # every row in one answer shares the exact same scoring ts (single-date fixture).
    all_rows = out["top"] + out["bottom"]
    assert len({r["ts"] for r in all_rows}) == 1


def test_no_args_defaults_to_extremes_of_latest_cross_section() -> None:
    db = _mkdb()
    try:
        rows = [
            _row(f"SYM{i}", 21, ts=_NOW, pctile=i / 10.0, rank=i)
            for i in range(11)
        ]
        _insert(db, rows)
        out = _call(db, {})
    finally:
        db.close()
    assert len(out["top"]) == 5
    assert len(out["bottom"]) == 5
    # extremes: SYM10 (pctile 1.0) highest, SYM0 (pctile 0.0) lowest.
    assert out["top"][0]["symbol"] == "SYM10"
    assert out["bottom"][0]["symbol"] == "SYM0"


def test_cross_section_uses_each_symbols_own_latest_row_not_a_global_max_ts() -> None:
    """Regression test for a real bug found in end-to-end testing: pinning
    the cross-section to a single global max(ts) silently collapsed a
    147-name panel down to only the 1-2 names whose OWN latest bar happened
    to be freshest that day (vol_scores.py deliberately stamps each row with
    that SYMBOL's own last available price bar, so staleness is visible per
    name), while reporting that tiny slice's freshness as if it applied to
    the whole cross-section. The fix takes each symbol's own latest row
    (matching the shape reported live: 2 fresh names, 145/147 stale names),
    reports an as_of_min/as_of_max RANGE instead of one ts, and judges
    overall staleness from the OLDEST constituent, not the newest."""
    older = _NOW - timedelta(days=14)  # comfortably > 5 trading days stale
    db = _mkdb()
    try:
        rows = [_row(f"FRESH{i}", 21, ts=_NOW, pctile=0.9 + i / 100, rank=140 + i) for i in range(2)]
        rows += [_row(f"OLD{i}", 21, ts=older, pctile=i / 100.0, rank=i + 1) for i in range(20)]
        _insert(db, rows)
        # top=2 so the assertion below cleanly isolates the 2 fresh (highest-pctile) names.
        out = _call(db, {"horizon": 21, "top": 2, "bottom": 5})
    finally:
        db.close()

    # The core bug: ALL 22 symbols must be scored, not just the 2 fresh ones.
    assert out["n_scored"] == 22, "per-symbol latest row must include every symbol, not just the freshest"
    assert "ts" not in out, "multi-symbol payload must report a range, not one shared ts"
    assert out["as_of_min"] == str(older.date())
    assert out["as_of_max"] == str(_NOW.date())
    assert out["mixed_scoring_dates"] is True

    # Staleness must reflect the OLDEST constituent (14 days), not the
    # newest (0 days) -- the exact bug: reporting fresh/healthy while most
    # of the panel is stale.
    assert out["stale"] is True
    assert out["age_trading_days"] > 5
    assert "staleness_note" in out

    # Thin cross-section (22 of 147) must announce itself.
    assert "coverage_note" in out
    assert "22" in out["coverage_note"] and "147" in out["coverage_note"]

    # The 2 fresh (highest-pctile) names are correctly the jumpiest.
    top_syms = {r["symbol"] for r in out["top"]}
    assert top_syms == {"FRESH0", "FRESH1"}


def test_top_bottom_never_overlap_when_panel_smaller_than_requested() -> None:
    """When top+bottom together ask for more names than are actually
    ranked, the same symbol must never be listed as both jumpiest and
    calmest -- the tool must truncate one side and say so, not duplicate."""
    db = _mkdb()
    try:
        rows = [
            _row("A", 21, ts=_NOW, pctile=0.10, rank=1),
            _row("B", 21, ts=_NOW, pctile=0.50, rank=2),
            _row("C", 21, ts=_NOW, pctile=0.90, rank=3),
        ]
        _insert(db, rows)
        out = _call(db, {"horizon": 21, "top": 5, "bottom": 5})  # 10 requested, only 3 available
    finally:
        db.close()

    assert out["n_scored"] == 3
    top_syms = [r["symbol"] for r in out["top"]]
    bottom_syms = [r["symbol"] for r in out["bottom"]]
    assert set(top_syms).isdisjoint(bottom_syms), "top and bottom must not list the same symbol twice"
    assert set(top_syms) | set(bottom_syms) == {"A", "B", "C"}, "every available name shown exactly once"
    assert "extremes_note" in out
    assert "5" in out["extremes_note"]  # states what was requested


# --- out-of-panel flag (PLAN §13.8) ---------------------------------------------


def test_out_of_panel_symbol_is_flagged_not_hidden() -> None:
    db = _mkdb()
    try:
        _insert(db, [_row("GLD", 21, ts=_NOW, in_reference_panel=False, pctile=0.4)])
        out = _call(db, {"symbol": "GLD", "horizon": 21})
    finally:
        db.close()
    assert out["in_reference_panel"] is False
    assert "panel_note" in out
    assert "reference panel" in out["panel_note"].lower()
    # it is NOT hidden -- the percentile is still returned, just flagged.
    assert out["pctile"] == 0.4


def test_unranked_symbols_excluded_from_top_bottom_but_reported() -> None:
    db = _mkdb()
    try:
        _insert(db, [
            _row("RANKED", 21, ts=_NOW, pctile=0.5, rank=1),
            _row("NORANK", 21, ts=_NOW, pctile=None, rank=None),
        ])
        out = _call(db, {"horizon": 21, "top": 5, "bottom": 5})
    finally:
        db.close()
    all_syms = {r["symbol"] for r in out["top"] + out["bottom"]}
    assert "NORANK" not in all_syms
    assert "unranked_note" in out


# --- staleness (PLAN §13.9 step 3 deliverable 2) --------------------------------


def test_stale_score_is_flagged() -> None:
    old_ts = _NOW - timedelta(days=14)  # comfortably > 5 trading days
    db = _mkdb()
    try:
        _insert(db, [_row("OLDNAME", 21, ts=old_ts, pctile=0.5)])
        out = _call(db, {"symbol": "OLDNAME", "horizon": 21})
    finally:
        db.close()
    assert out["stale"] is True
    assert out["age_trading_days"] > 5
    assert "staleness_note" in out


def test_fresh_score_is_not_flagged() -> None:
    db = _mkdb()
    try:
        _insert(db, [_row("NEWNAME", 21, ts=_NOW, pctile=0.5)])
        out = _call(db, {"symbol": "NEWNAME", "horizon": 21})
    finally:
        db.close()
    assert out["stale"] is False
    assert "staleness_note" not in out


def test_cross_section_staleness_reflects_the_oldest_row_when_only_one_symbol() -> None:
    old_ts = _NOW - timedelta(days=14)
    db = _mkdb()
    try:
        _insert(db, [_row("ONLYOLD", 21, ts=old_ts, pctile=0.5)])
        out = _call(db, {"horizon": 21})
    finally:
        db.close()
    assert out["stale"] is True
    assert "staleness_note" in out
    assert out["as_of_min"] == out["as_of_max"] == str(old_ts.date())


# --- empty / missing table -> graceful, never raises ----------------------------


def test_empty_table_returns_error_dict_not_raise() -> None:
    db = _mkdb()
    try:
        out_symbol = _call(db, {"symbol": "AAPL"})
        out_cross = _call(db, {})
    finally:
        db.close()
    assert "error" in out_symbol
    assert "error" in out_cross


def test_missing_table_returns_error_dict_not_raise() -> None:
    tmp = tempfile.TemporaryDirectory()
    db = DuckStore(Path(tmp.name) / "market.duckdb")  # ml_vol_scores never created
    try:
        out_symbol = _call(db, {"symbol": "AAPL"})
        out_cross = _call(db, {})
    finally:
        db.close()
    assert "error" in out_symbol
    assert "error" in out_cross


def _run_all() -> int:
    checks = [
        test_vol_rank_registered_in_tools,
        test_single_symbol_lookup_h21_exposes_pred_vol_as_admissible,
        test_single_symbol_lookup_defaults_to_horizon_21,
        test_unknown_symbol_returns_error_dict_not_raise,
        test_h5_level_is_labelled_rank_only_not_presented_as_pred_vol,
        test_h21_never_gets_the_rank_only_label,
        test_bad_horizon_raises_value_error,
        test_non_integer_top_raises_value_error,
        test_top_bottom_listing_orders_by_pctile,
        test_no_args_defaults_to_extremes_of_latest_cross_section,
        test_cross_section_uses_each_symbols_own_latest_row_not_a_global_max_ts,
        test_top_bottom_never_overlap_when_panel_smaller_than_requested,
        test_out_of_panel_symbol_is_flagged_not_hidden,
        test_unranked_symbols_excluded_from_top_bottom_but_reported,
        test_stale_score_is_flagged,
        test_fresh_score_is_not_flagged,
        test_cross_section_staleness_reflects_the_oldest_row_when_only_one_symbol,
        test_empty_table_returns_error_dict_not_raise,
        test_missing_table_returns_error_dict_not_raise,
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
