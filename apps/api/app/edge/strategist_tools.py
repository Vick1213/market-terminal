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
