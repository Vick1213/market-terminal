"""Tool-use for the strategist LLM.

The strategist narrates a deterministic allocation JSON blob today
(``StrategistService._notes`` in edge/strategist.py). This module gives it a
bounded ReAct-style loop instead: a math tool (``calc``) and read-only
information-access tools (price history, quotes, macro series, news,
portfolio, per-signal detail) it can call before writing its final notes.

Provider-agnostic by design: this does NOT use any provider's native
tool-calling API (OpenAI "tools"/"function_call", Anthropic "tool_use",
etc.) because it has to work identically against a local Ollama model
(qwen3:8b) that speaks plain chat messages only. Instead the protocol is a
fenced ```json {"tool": ..., "args": {...}}``` block described in the
prompt, parsed defensively out of whatever prose the model wraps around it.

Every tool is read-only: no order placement, no writes, no broker mutation.
``run_tool_loop`` never lets a single bad tool call kill the run — unknown
tools, bad args, and executor exceptions all become an ``{"error": ...}``
tool result fed back to the model, which is expected to recover (or a small
local model at least gets a chance to).
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import math
import re
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.edge.llm import LlmClient
from app.edge.strategist import _pct_return, _price_series

log = logging.getLogger("market.edge.strategist_tools")

MAX_RESULT_CHARS = 2000


# --------------------------------------------------------------- safe_calc

_ALLOWED_FUNCS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "len": len,
    "sqrt": math.sqrt,
    "log": math.log,
    "exp": math.exp,
    "mean": statistics.mean,
    "median": statistics.median,
    "std": statistics.pstdev,
    "pct_change": lambda xs: [
        (xs[i] / xs[i - 1] - 1) * 100 if xs[i - 1] else 0.0
        for i in range(1, len(xs))
    ],
}

_BIN_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_UNARY_OPS = (ast.UAdd, ast.USub)
_CMP_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)


def _validate_calc_node(node: ast.AST) -> None:
    """Whitelist-only AST walk. Anything not explicitly handled raises —
    this is deliberately closed-world (no names/attributes/subscripts/calls
    outside the whitelist), so dunder/import escapes have no path through."""
    if isinstance(node, ast.Expression):
        _validate_calc_node(node.body)
    elif isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return
        if not isinstance(node.value, (int, float)):
            raise ValueError(f"calc: literal {node.value!r} is not a number")
    elif isinstance(node, ast.List):
        for el in node.elts:
            _validate_calc_node(el)
    elif isinstance(node, ast.BinOp):
        if not isinstance(node.op, _BIN_OPS):
            raise ValueError(f"calc: operator {type(node.op).__name__} not allowed")
        _validate_calc_node(node.left)
        _validate_calc_node(node.right)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _UNARY_OPS):
            raise ValueError(f"calc: unary operator {type(node.op).__name__} not allowed")
        _validate_calc_node(node.operand)
    elif isinstance(node, ast.Compare):
        for op in node.ops:
            if not isinstance(op, _CMP_OPS):
                raise ValueError(f"calc: comparison {type(op).__name__} not allowed")
        _validate_calc_node(node.left)
        for comparator in node.comparators:
            _validate_calc_node(comparator)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise ValueError(
                "calc: only these functions may be called: "
                + ", ".join(sorted(_ALLOWED_FUNCS))
            )
        if node.keywords:
            raise ValueError("calc: keyword arguments are not allowed")
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                raise ValueError("calc: *args are not allowed")
            _validate_calc_node(arg)
    else:
        raise ValueError(f"calc: expression type {type(node).__name__} is not allowed")


def safe_calc(expression: str) -> float | int | bool | list:
    """Evaluate a bounded arithmetic expression. Allows numeric literals,
    list literals, +-*/ // % ** and unary variants, comparisons, and a
    whitelisted function set (see _ALLOWED_FUNCS). No names, attributes,
    subscripts, string literals, or calls outside the whitelist — rejects
    with a clear ValueError instead of ever reaching Python's eval on
    anything untrusted (defense in depth: eval also runs with
    __builtins__ stripped and a locals dict limited to the whitelist)."""
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("calc: expression must be a non-empty string")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"calc: invalid syntax ({exc.msg})") from exc
    _validate_calc_node(tree)
    code = compile(tree, "<calc>", "eval")
    try:
        result = eval(code, {"__builtins__": {}}, dict(_ALLOWED_FUNCS))  # noqa: S307
    except ZeroDivisionError as exc:
        raise ValueError(f"calc: division by zero ({exc})") from exc
    except Exception as exc:  # statistics errors, math domain errors, etc.
        raise ValueError(f"calc: could not evaluate ({exc})") from exc
    if isinstance(result, (bool, int, float, list)):
        return result
    raise ValueError("calc: expression did not evaluate to a number or list")


# ----------------------------------------------------------------- tools

def _truncate_json(obj: Any, limit: int = MAX_RESULT_CHARS) -> str:
    text = json.dumps(obj, default=str)
    if len(text) <= limit:
        return text
    return text[: limit - 16] + '..."<truncated>"}'


def _tool_calc(_duck: DuckStore, _sqlite: SqliteStore, _ctx: dict, args: dict,
               _broker_state: Any = None) -> dict:
    expr = args.get("expression")
    return {"expression": expr, "result": safe_calc(expr)}


def _tool_price_history(duck: DuckStore, _sqlite: SqliteStore, _ctx: dict, args: dict,
                         _broker_state: Any = None) -> dict:
    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("price_history: 'symbol' is required")
    bars = args.get("bars", 60)
    try:
        bars = max(2, min(int(bars), 260))
    except (TypeError, ValueError):
        bars = 60
    rows = _price_series(duck, symbol, bars)
    if not rows:
        return {"symbol": symbol, "error": f"no price history for {symbol} (source=yahoo)"}
    dates = [str(ts)[:10] for ts, _ in rows]
    closes = [round(float(c), 4) for _, c in rows]
    first, last = closes[0], closes[-1]
    ret_pct = (last / first - 1) * 100 if first else None
    peak = closes[0]
    max_dd = 0.0
    for c in closes:
        peak = max(peak, c)
        if peak:
            max_dd = min(max_dd, (c / peak - 1) * 100)
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]
    vol_pct = None
    if len(rets) >= 2:
        mu = sum(rets) / len(rets)
        var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
        vol_pct = (var**0.5) * (252**0.5) * 100
    return {
        "symbol": symbol,
        "bars": len(closes),
        "dates": dates,
        "closes": closes,
        "summary": {
            "last_close": last,
            "return_pct": round(ret_pct, 2) if ret_pct is not None else None,
            "max_drawdown_pct": round(max_dd, 2),
            "realized_vol_annualized_pct": round(vol_pct, 2) if vol_pct is not None else None,
        },
    }


def _tool_quote(duck: DuckStore, _sqlite: SqliteStore, _ctx: dict, args: dict,
                 _broker_state: Any = None) -> dict:
    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("quote: 'symbol' is required")
    rows = _price_series(duck, symbol, 1)
    if not rows:
        return {"symbol": symbol, "error": f"no price data for {symbol} (source=yahoo)"}
    out: dict[str, Any] = {"symbol": symbol, "last_close": round(float(rows[-1][1]), 4)}
    for label, bars in (
        ("return_1d_pct", 1), ("return_5d_pct", 5),
        ("return_20d_pct", 20), ("return_60d_pct", 60),
    ):
        r = _pct_return(duck, symbol, bars)
        out[label] = round(r, 2) if r is not None else None
    return out


# --- vol_rank (PLAN §13.9 step 3) -------------------------------------------
#
# Read-only lookup over ``ml_vol_scores`` (written by app.ml.vol_scores, a
# concurrently-developed module this file only ever SELECTs from — no
# import, no coupling to its internals, so this tool cannot break if that
# module changes shape mid-edit). Three correctness constraints from PLAN
# §13.3/§13.4/§13.8 that must reach the LLM, not just exist in the DB:
#   1. level_admissible is False at h=5 — its pred_vol is a rank input only,
#      never a usable sizing level (§13.3's h=5 kill switch). Emitted under a
#      differently-named key (``pred_vol_rank_only``) at h=5 so the LLM can't
#      mistake it for the admissible h=21 ``pred_vol``.
#   2. in_reference_panel flags names (ETFs/crypto/metals) ranked against a
#      147-name panel they were never a member of (§13.8).
#   3. Staleness — 145/147 symbols are pinned at one date right now, so a
#      stale answer is the norm, not the exception; every payload carries the
#      score's ts (or, for a multi-symbol cross-section, an as_of_min/max
#      range), its trading-day age, and an explicit "stale" flag/note past a
#      5-trading-day threshold. A cross-section's overall staleness is judged
#      by its OLDEST constituent, never its newest -- see the "per-symbol
#      latest row" note below for why a single shared ts is unsafe here.
#
# CORRECTNESS FIX (found in live end-to-end testing, not by any test written
# against synthetic fixtures): a cross-section listing must take EACH
# SYMBOL'S OWN latest row -- the same rule the single-symbol lookup already
# used -- never a single WHERE ts = max(ts) filter. vol_scores.py stamps
# every row with that SYMBOL's own last available OHLC bar (deliberately, so
# per-name price staleness stays visible rather than being papered over), so
# different symbols legitimately carry different `ts` values on the same
# scoring run. Pinning to one global max(ts) silently collapsed a 147-name
# panel down to only the 1-2 names whose price happened to be freshest that
# day, while reporting the (correct, but wildly unrepresentative) freshness
# of just those names as if it applied to the whole cross-section -- a
# confidently wrong answer, the worst failure mode for something an LLM
# consumes. The fix: always take every symbol's own latest row, report the
# resulting ts RANGE explicitly (`as_of_min`/`as_of_max`), and derive
# staleness from the oldest row in that range.

_VOL_RANK_TABLE = "ml_vol_scores"
_VOL_RANK_HORIZONS = (5, 21)           # the only horizons app.ml.vol_scores ever scores
_VOL_RANK_DEFAULT_HORIZON = 21
_VOL_RANK_STALE_TRADING_DAYS = 5       # PLAN §13.9 step 3 / §13.6 staleness note threshold
_VOL_RANK_MAX_N = 25                   # top/bottom cap, mirrors _tool_portfolio's positions[:25]
_VOL_RANK_DEFAULT_N = 5                # extremes size when neither top nor bottom is given
# Fixed panel size (PLAN §13.5: "the FIXED 147-name research universe"),
# duplicated here as a plain literal rather than importing app.ml.universe /
# app.ml.vol_scores -- same "no coupling to a concurrently-edited module"
# reasoning as the rest of this file. Used only for an advisory coverage
# note, never for gating, so an eventual panel-size drift is low-risk.
_VOL_RANK_REFERENCE_PANEL_SIZE = 147
_VOL_RANK_COVERAGE_MIN_RATIO = 0.8     # coverage_note fires below this fraction of the panel
_VOL_RANK_COLS = (
    "ts, symbol, horizon, estimator, pred_vol, level_admissible, "
    "rank, pctile, in_reference_panel, n_obs"
)
# Latest row PER SYMBOL (not a single global max(ts) -- see the module note
# above): QUALIFY + row_number() is the DuckDB-native way to express this.
_VOL_RANK_LATEST_PER_SYMBOL_SQL = (
    f"SELECT {_VOL_RANK_COLS} FROM {_VOL_RANK_TABLE} WHERE horizon = ? "
    "QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY ts DESC) = 1"
)


def _trading_days_stale(ts: Any, now: datetime) -> int:
    """Weekday count strictly between ``ts``'s date and ``now``'s date — a
    deliberate simplification (no market-holiday calendar exists anywhere
    else in this codebase either; see the plain calendar-day ``_age_days`` in
    edge/strategist.py for the analogous staleness helper this project
    already uses elsewhere). ``ts`` may be a datetime, pandas Timestamp, or
    an ISO string (DuckDB python bindings can hand back any of these)."""
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts[:19])
    d0, d1 = ts.date(), now.date()
    if d1 <= d0:
        return 0
    days = 0
    d = d0
    while d < d1:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def _vol_rank_row(row: tuple, now: datetime) -> dict[str, Any]:
    (ts, symbol, horizon, estimator, pred_vol, level_admissible,
     rank, pctile, in_panel, n_obs) = row
    age = _trading_days_stale(ts, now)
    stale = age > _VOL_RANK_STALE_TRADING_DAYS
    out: dict[str, Any] = {
        "symbol": symbol,
        "horizon": int(horizon),
        "ts": str(ts)[:10],
        "age_trading_days": age,
        "stale": stale,
        "estimator": estimator,
        "rank": int(rank) if rank is not None else None,
        "pctile": round(float(pctile), 4) if pctile is not None else None,
        "in_reference_panel": bool(in_panel),
        "n_obs": int(n_obs) if n_obs is not None else None,
    }
    if level_admissible:
        out["level_admissible"] = True
        out["pred_vol"] = round(float(pred_vol), 4) if pred_vol is not None else None
    else:
        out["level_admissible"] = False
        out["pred_vol_rank_only"] = round(float(pred_vol), 4) if pred_vol is not None else None
        out["level_note"] = (
            f"h={horizon} pred_vol is NOT an admissible sizing level -- rank/percentile "
            "only (PLAN §13.3); do not size off this number"
        )
    if not in_panel:
        out["panel_note"] = (
            "not a member of the 147-name reference panel -- its percentile is not "
            "apples-to-apples with panel members (PLAN §13.8)"
        )
    if stale:
        out["staleness_note"] = (
            f"score is {age} trading day(s) old (> {_VOL_RANK_STALE_TRADING_DAYS}-day "
            "staleness threshold) -- treat as informational, not current"
        )
    return out


def _vol_rank_count_arg(args: dict, name: str) -> int | None:
    v = args.get(name)
    if v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"vol_rank: {name!r} must be an integer")
    if n <= 0:
        raise ValueError(f"vol_rank: {name!r} must be positive")
    return min(n, _VOL_RANK_MAX_N)


def _tool_vol_rank(duck: DuckStore, _sqlite: SqliteStore, _ctx: dict, args: dict,
                    _broker_state: Any = None) -> dict:
    symbol = str(args.get("symbol") or "").strip().upper()

    horizon_raw = args.get("horizon", _VOL_RANK_DEFAULT_HORIZON)
    try:
        horizon = int(horizon_raw)
    except (TypeError, ValueError):
        raise ValueError("vol_rank: 'horizon' must be an integer (5 or 21)")
    if horizon not in _VOL_RANK_HORIZONS:
        raise ValueError("vol_rank: 'horizon' must be 5 or 21 (the only scored horizons)")

    top_n = _vol_rank_count_arg(args, "top")
    bottom_n = _vol_rank_count_arg(args, "bottom")

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if symbol:
        try:
            rows = duck.fetchall(
                f"SELECT {_VOL_RANK_COLS} FROM {_VOL_RANK_TABLE} "
                "WHERE symbol = ? AND horizon = ? ORDER BY ts DESC LIMIT 1",
                [symbol, horizon],
            )
        except Exception as exc:  # missing table, corrupt DB, etc. -- never raise out of a tool
            return {"error": f"vol_rank data unavailable: {exc}"}
        if not rows:
            return {
                "symbol": symbol, "horizon": horizon,
                "error": f"no vol score for {symbol} at horizon={horizon}",
            }
        return _vol_rank_row(rows[0], now)

    # No symbol requested: extremes across EACH SYMBOL'S OWN latest row for
    # this horizon (never a single global max(ts) -- see the module note
    # above). Different symbols can legitimately carry different `ts`
    # values, so the payload reports an explicit as_of_min/as_of_max range
    # instead of one shared date, and staleness is judged by the OLDEST
    # constituent row, not the newest.
    try:
        rows = duck.fetchall(_VOL_RANK_LATEST_PER_SYMBOL_SQL, [horizon])
    except Exception as exc:  # missing table, corrupt DB, etc. -- never raise out of a tool
        return {"error": f"vol_rank data unavailable: {exc}"}
    if not rows:
        return {"horizon": horizon, "error": "no vol scores available (table empty or missing)"}

    if top_n is None and bottom_n is None:
        top_n = bottom_n = _VOL_RANK_DEFAULT_N

    ts_idx, pctile_idx = 0, 7  # positions of `ts`/`pctile` in _VOL_RANK_COLS
    n_scored = len(rows)
    ts_values = [r[ts_idx] for r in rows]
    min_ts, max_ts = min(ts_values), max(ts_values)  # earliest/oldest, latest/newest
    # Age is monotonic in ts (earlier date -> equal-or-larger trading-day age vs `now`), so the
    # earliest ts is exactly the stalest constituent -- no need to scan every row's age.
    age_trading_days = _trading_days_stale(min_ts, now)
    stale = age_trading_days > _VOL_RANK_STALE_TRADING_DAYS

    out: dict[str, Any] = {
        "horizon": horizon,
        "as_of_min": str(min_ts)[:10],
        "as_of_max": str(max_ts)[:10],
        "age_trading_days": age_trading_days,
        "stale": stale,
        "n_scored": n_scored,
    }
    if str(min_ts)[:10] != str(max_ts)[:10]:
        out["mixed_scoring_dates"] = True
    # rank and level come from DIFFERENT estimators by design (PLAN §13.3:
    # HAR-63 wins on ranks, plain HAR wins on levels), and they are only ~0.80
    # correlated -- so pred_vol is NOT monotonic in pctile. Without this note a
    # reader takes the non-monotonicity for a data error and distrusts the tool.
    out["estimator_note"] = (
        "rank/pctile and pred_vol come from different estimators (HAR-63 for ranks, "
        "HAR for levels), so pred_vol is not strictly monotonic in pctile -- ranks "
        "order the names, levels size them"
    )
    if stale:
        out["staleness_note"] = (
            f"oldest score in this cross-section is {age_trading_days} trading day(s) old "
            f"(> {_VOL_RANK_STALE_TRADING_DAYS}-day staleness threshold) -- treat as "
            "informational, not current"
        )
    if n_scored < _VOL_RANK_REFERENCE_PANEL_SIZE * _VOL_RANK_COVERAGE_MIN_RATIO:
        out["coverage_note"] = (
            f"only {n_scored} of {_VOL_RANK_REFERENCE_PANEL_SIZE} reference-panel names "
            f"have a horizon={horizon} score -- thin cross-section, ranks may be less reliable"
        )

    ranked = sorted((r for r in rows if r[pctile_idx] is not None),
                     key=lambda r: r[pctile_idx], reverse=True)  # highest vol first
    unranked = [r for r in rows if r[pctile_idx] is None]
    n_ranked = len(ranked)

    # Non-overlapping top/bottom allocation: `top` claims its full request
    # first (up to what's available), `bottom` gets whatever's left, capped
    # by its own request -- so the same symbol never appears in both lists.
    top_actual = min(top_n, n_ranked) if top_n else 0
    bottom_actual = min(bottom_n, n_ranked - top_actual) if bottom_n else 0
    if top_n:
        out["top"] = [_vol_rank_row(r, now) for r in ranked[:top_actual]]
    if bottom_n:
        bottom_slice = ranked[n_ranked - bottom_actual:] if bottom_actual else []
        out["bottom"] = [_vol_rank_row(r, now) for r in reversed(bottom_slice)]
    if (top_n and top_actual < top_n) or (bottom_n and bottom_actual < bottom_n):
        out["extremes_note"] = (
            f"only {n_ranked} ranked name(s) available at horizon={horizon} -- fewer than the "
            f"requested top={top_n or 0}/bottom={bottom_n or 0}; returned each available name "
            "once (no overlap between top and bottom) instead of duplicating"
        )
    if unranked and (top_n or bottom_n):
        out["unranked_note"] = (
            f"{len(unranked)} symbol(s) in this cross-section have no percentile "
            "(insufficient reference-panel distribution) and are excluded from top/bottom"
        )
    return out


def _tool_macro_series(duck: DuckStore, _sqlite: SqliteStore, _ctx: dict, args: dict,
                        _broker_state: Any = None) -> dict:
    series_id = str(args.get("series_id") or args.get("id") or "").strip()
    if not series_id:
        raise ValueError("macro_series: 'series_id' is required")
    points = args.get("points", 30)
    try:
        points = max(1, min(int(points), 60))
    except (TypeError, ValueError):
        points = 30
    rows = duck.fetchall(
        "SELECT ts, value FROM ts_macro WHERE series_id = ? AND value IS NOT NULL "
        "ORDER BY ts DESC LIMIT ?",
        [series_id, points],
    )
    if not rows:
        sample = duck.fetchall("SELECT DISTINCT series_id FROM ts_macro ORDER BY series_id LIMIT 15")
        ids = ", ".join(r[0] for r in sample)
        return {"error": f"unknown or empty series_id {series_id!r}. Available (sample): {ids}"}
    rows = rows[::-1]
    return {
        "series_id": series_id,
        "points": len(rows),
        "dates": [str(ts)[:10] for ts, _ in rows],
        "values": [round(float(v), 4) for _, v in rows],
    }


def _tool_news(duck: DuckStore, _sqlite: SqliteStore, _ctx: dict, args: dict,
                _broker_state: Any = None) -> dict:
    symbol = str(args.get("symbol") or "").strip().upper()
    keyword = str(args.get("keyword") or "").strip()
    limit = args.get("limit", 5)
    try:
        limit = max(1, min(int(limit), 10))
    except (TypeError, ValueError):
        limit = 5
    if symbol:
        rows = duck.fetchall(
            "SELECT title, published, score, confidence, source FROM news_items "
            "WHERE upper(symbol) = ? ORDER BY published DESC LIMIT ?",
            [symbol, limit],
        )
    elif keyword:
        rows = duck.fetchall(
            "SELECT title, published, score, confidence, source FROM news_items "
            "WHERE title ILIKE ? OR summary ILIKE ? ORDER BY published DESC LIMIT ?",
            [f"%{keyword}%", f"%{keyword}%", limit],
        )
    else:
        raise ValueError("news: 'symbol' or 'keyword' is required")
    if not rows:
        return {
            "symbol": symbol or None, "keyword": keyword or None,
            "headlines": [], "note": "no matching headlines",
        }
    return {
        "symbol": symbol or None,
        "keyword": keyword or None,
        "headlines": [
            {
                "title": title,
                "published": str(published)[:16],
                "score": round(float(score), 2) if score is not None else None,
                "confidence": round(float(conf), 2) if conf is not None else None,
                "source": src,
            }
            for title, published, score, conf, src in rows
        ],
    }


async def _tool_portfolio(
    _duck: DuckStore, _sqlite: SqliteStore, _ctx: dict, _args: dict,
    broker_state: Any = None,
) -> dict:
    """Read-only paper-bot portfolio summary (broker ground truth: account +
    positions), read through the shared TTL-cached ``BrokerState``
    (app.trading.broker_cache — the same rate-limit-safe view the bot
    endpoints and both sleeves read). ``broker_state`` is threaded in from
    ``StrategistService(..., broker_state=...)``; it is optional (None in
    tests / builds without the trading module wired up) and any broker
    failure degrades to an error payload — this must never take the
    strategist run down with it, and never touches an order endpoint."""
    if broker_state is None:
        return {"error": "portfolio unavailable — broker not wired into the strategist tool loop"}
    try:
        if not broker_state.enabled:
            return {"error": "portfolio unavailable — broker not configured (no trading keys)"}
        account = await broker_state.account()
        positions = await broker_state.positions()
    except Exception as exc:
        return {"error": f"portfolio lookup failed: {exc}"}
    try:
        equity = float(account.get("equity") or account.get("portfolio_value") or 0.0)
        cash = float(account.get("cash") or 0.0)
        holdings = [
            {
                "symbol": p.get("symbol"),
                "qty": float(p.get("qty") or 0.0),
                "market_value": round(float(p.get("market_value") or 0.0), 2),
                "unrealized_pl": round(float(p.get("unrealized_pl") or 0.0), 2),
            }
            for p in positions
        ]
        return {
            "is_paper": broker_state.is_paper,
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "n_positions": len(holdings),
            "positions": holdings[:25],
        }
    except Exception as exc:
        return {"error": f"portfolio summary shape unexpected: {exc}"}


def _tool_signal_detail(_duck: DuckStore, _sqlite: SqliteStore, ctx: dict, args: dict,
                         _broker_state: Any = None) -> dict:
    key = str(args.get("key") or "").strip()
    if not key:
        raise ValueError("signal_detail: 'key' is required")
    signals = (ctx or {}).get("signals") or []
    sig = next((s for s in signals if s.get("key") == key), None)
    if sig is None:
        keys = ", ".join(s.get("key", "") for s in signals[:25])
        return {"error": f"unknown signal key {key!r}. Available: {keys}"}
    return sig


@dataclass(frozen=True)
class Tool:
    name: str
    description: str  # shown verbatim to the LLM: one-liner + args schema
    # (duck, sqlite, ctx, args, broker_state) -> result | awaitable[result].
    # broker_state is the shared app.trading.broker_cache.BrokerState (or
    # None) — only the portfolio tool uses it; every other tool ignores it.
    executor: Callable[[DuckStore, SqliteStore, dict, dict, Any], Any]


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "calc",
            'Evaluate a safe math expression. args: {"expression": "<expr>"}. '
            "Allowed: + - * / // % ** unary ops, comparisons, list literals, and "
            "abs/round/min/max/sum/len/sqrt/log/exp/mean/median/std/pct_change. "
            "No names/strings/attributes.",
            _tool_calc,
        ),
        Tool(
            "price_history",
            'Daily close history. args: {"symbol": "AAPL", "bars": 60} (bars optional, '
            "max 260). Returns closes + a summary (return, max drawdown, realized vol).",
            _tool_price_history,
        ),
        Tool(
            "quote",
            'Latest close + 1d/5d/20d/60d percent returns. args: {"symbol": "AAPL"}.',
            _tool_quote,
        ),
        Tool(
            "vol_rank",
            'Per-name volatility rank/level vs the 147-name reference panel. args: '
            '{"symbol": "AAPL"} for one name, OR {"top": 5, "bottom": 5} for the '
            'jumpiest/calmest names (both default to 5 if neither given); optional '
            '"horizon" (5 or 21, default 21). h=5 is RANK-ONLY -- its level is not '
            "admissible for sizing (PLAN §13.3); only h=21 gives a usable pred_vol "
            "level. Flags symbols outside the reference panel and stale "
            "(>5 trading-day-old) scores.",
            _tool_vol_rank,
        ),
        Tool(
            "macro_series",
            'Recent points of a macro/indicator series. args: {"series_id": '
            '"NET_LIQUIDITY", "points": 30} (points optional, max 60). Unknown id '
            "returns a sample of available ids.",
            _tool_macro_series,
        ),
        Tool(
            "news",
            'Recent scored headlines. args: {"symbol": "AAPL"} OR {"keyword": "fed"}, '
            'optional "limit" (max 10).',
            _tool_news,
        ),
        Tool(
            "portfolio",
            "Read-only paper-bot portfolio summary (positions + cash). args: {} "
            "(no arguments). Returns {\"error\": ...} if the trading module/broker "
            "isn't available.",
            _tool_portfolio,
        ),
        Tool(
            "signal_detail",
            'Full detail dict for one signal already computed this run. args: '
            '{"key": "gex"}.',
            _tool_signal_detail,
        ),
    )
}


# ------------------------------------------------------------- tool loop

_SYSTEM_MSG = """You are the strategist for a personal market terminal. Below is a \
machine-generated allocation suggestion (JSON) built from stored signals. Your job \
is to write 3-8 short strategy notes in markdown (one "- " bullet each, <=25 words \
per note) connecting the signals to positioning, e.g. "gamma short + risk-off: \
size down, expect range expansion". Name specific holdings (sector ETFs, single \
names, GLD/SLV or BTC/ETH splits) where the buckets' holdings suggest them. Only \
use facts from the JSON and tool results below, no invented numbers, no \
disclaimers, no preamble.

Before writing, you may call read-only tools to check numbers (at most {max_calls} \
calls total). To call a tool, reply with ONLY a fenced block:
```json
{{"tool": "<name>", "args": {{...}}}}
```
Nothing else in that reply — no prose, no explanation. When you are done (or have \
nothing more to check), reply with ONLY the final notes as markdown bullet lines \
("- ...") — no fenced block, no preamble, no closing remarks.

TOOLS:
{tool_list}

DATA:
{data}
"""

_FINAL_NUDGE = (
    "No more tool calls available — write the final notes now. Reply with ONLY "
    "3-8 markdown bullet lines (\"- ...\"), nothing else."
)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_tool_call(text: str) -> dict | None:
    """Pull a {"tool": ..., "args": {...}} object out of a reply, tolerating
    surrounding prose (fenced ```json block, first match wins) or a bare
    JSON object as the entire reply. Returns None if nothing tool-shaped
    is found — the caller then treats the reply as the final answer."""
    text = (text or "").strip()
    if not text:
        return None
    m = _JSON_FENCE_RE.search(text)
    if m:
        candidate = m.group(1)
    elif text.startswith("{") and text.endswith("}"):
        candidate = text
    else:
        return None
    try:
        obj = json.loads(candidate)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("tool"), str):
        return None
    return obj


def _parse_notes(text: str) -> list[str]:
    return [
        ln.lstrip("-• ").strip()
        for ln in (text or "").splitlines()
        if ln.strip().startswith(("-", "•"))
    ]


async def _run_tool(name: str, duck: DuckStore, sqlite: SqliteStore, ctx: dict, args: dict,
                     broker_state: Any = None) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"unknown tool {name!r}. Available: {', '.join(sorted(TOOLS))}"}
    try:
        out = tool.executor(duck, sqlite, ctx, args, broker_state)
        if inspect.isawaitable(out):
            out = await out
    except Exception as exc:  # tool executors shouldn't raise, but never trust it
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(out, dict):
        out = {"result": out}
    return out


async def run_tool_loop(
    llm: LlmClient,
    duck: DuckStore,
    sqlite: SqliteStore,
    result: dict,
    *,
    max_calls: int = 6,
    budget_seconds: float = 120.0,
    broker_state: Any = None,
) -> tuple[list[str], list[dict]]:
    """Bounded tool-use loop for the strategist's notes. ``broker_state`` (the
    shared app.trading.broker_cache.BrokerState, or None) is passed straight
    through to the ``portfolio`` tool only — every other tool ignores it.
    Returns (notes_lines, tool_trace); raises ValueError if the final reply
    never parses into 3-8 bullet notes (callers own the fallback chain)."""
    started = time.monotonic()
    tool_list = "\n".join(f"- {t.name}: {t.description}" for t in TOOLS.values())
    data_json = json.dumps(
        {k: result[k] for k in ("regime", "score", "buckets", "equity_tilt", "signals") if k in result},
        indent=1, default=str,
    )
    messages: list[dict] = [{
        "role": "user",
        "content": _SYSTEM_MSG.format(max_calls=max_calls, tool_list=tool_list, data=data_json),
    }]
    trace: list[dict] = []
    calls_used = 0
    final_text = ""

    while True:
        elapsed = time.monotonic() - started
        if calls_used >= max_calls or elapsed >= budget_seconds:
            messages.append({"role": "user", "content": _FINAL_NUDGE})
            final_text = await llm.generate_messages(messages, temperature=0.3)
            break

        reply = await llm.generate_messages(messages, temperature=0.3)
        call = _extract_tool_call(reply)
        if call is None:
            final_text = reply
            break

        messages.append({"role": "assistant", "content": reply})
        name = call.get("tool", "")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        calls_used += 1

        payload = await _run_tool(name, duck, sqlite, result, args, broker_state)
        ok = "error" not in payload
        serialized = _truncate_json(payload)
        messages.append({"role": "user", "content": f"TOOL RESULT ({name}): {serialized}"})
        trace.append({"tool": name, "args": args, "ok": ok, "summary": serialized[:200]})

        if not ok:
            log.info("strategist tool loop: %s failed — %s", name, payload.get("error"))

    notes = _parse_notes(final_text)
    if not 3 <= len(notes) <= 8:
        raise ValueError(
            f"tool-loop LLM returned {len(notes)} final notes (need 3-8): {final_text[:200]!r}"
        )
    return notes, trace
