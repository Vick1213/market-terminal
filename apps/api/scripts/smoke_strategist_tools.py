"""Smoke test for the strategist tool-use loop (edge/strategist_tools.py) +
its wiring into StrategistService._notes (edge/strategist.py).

Fully offline/deterministic — no network, no real LLM. A scripted FakeLlm
drives three scenarios:

  1. Happy-path tool loop: calc -> price_history -> an unknown tool (must
     feed the error back and keep going, not crash the loop) -> final
     bullets. Asserts the notes parse and the trace has 3 entries with the
     right ok/error flags.
  2. Total LLM failure: FakeLlm raises on every call. Asserts the fallback
     chain (tool loop -> single-shot -> template) reaches the deterministic
     template notes without raising.
  3. safe_calc security: a battery of injection attempts that must all be
     rejected, plus one legitimate whitelisted-function expression that must
     evaluate correctly.

Exit code 0 = every check passed.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CHECKS: list[tuple[str, bool]] = []


def check(name: str, passed: bool) -> None:
    CHECKS.append((name, passed))
    print(f"  {'PASS' if passed else 'FAIL'}  {name}")


def seed_db(duck) -> None:
    """Synthetic-but-real-shaped fixtures: SPY daily bars (deterministic
    random walk) + a couple of macro points + one news item, enough for
    price_history/quote/macro_series/news/signal_detail to have something
    real to return without any network access."""
    import random

    rng = random.Random(20260813)
    rows = []
    price = 450.0
    start = datetime(2025, 1, 2)
    ts = start
    n = 0
    while n < 300:
        if ts.weekday() < 5:  # weekdays only
            price *= 1 + rng.uniform(-0.015, 0.017)
            o, hi, lo, c = price * 0.998, price * 1.01, price * 0.99, price
            rows.append(("yahoo", "SPY", "equity", ts, o, hi, lo, c, 5.0e7))
            n += 1
        ts += timedelta(days=1)
    duck.executemany(
        "INSERT OR REPLACE INTO ts_price "
        "(source, symbol, asset_class, ts, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    duck.executemany(
        "INSERT OR REPLACE INTO ts_macro (series_id, ts, value, source) VALUES (?, ?, ?, ?)",
        [
            ("NET_LIQUIDITY", start + timedelta(days=i), 6000.0 + i * 2, "fred")
            for i in range(0, 40, 2)
        ],
    )
    duck.execute(
        "INSERT OR REPLACE INTO news_items "
        "(id, source, symbol, title, summary, url, published, ingested, score, confidence, label, model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ["smoke-1", "test", "SPY", "Markets rally on soft CPI", "summary",
         "https://example.com/1", start + timedelta(days=10), start + timedelta(days=10),
         0.4, 0.8, "positive", "finbert"],
    )


class FakeLlm:
    """Scripts a fixed sequence of replies; raises IndexError if asked for
    more than provided (a real bug, not a silently-passing test)."""

    def __init__(self, replies: list[str] | None = None, raises: bool = False) -> None:
        self._replies = list(replies or [])
        self._raises = raises
        self.calls = 0
        self.label = "fake:scripted"

    async def generate_messages(self, messages: list[dict], *, temperature: float = 0.3) -> str:
        self.calls += 1
        if self._raises:
            raise ConnectionError("simulated total LLM failure")
        if not self._replies:
            raise IndexError("FakeLlm ran out of scripted replies")
        return self._replies.pop(0)

    async def generate(self, prompt: str, *, temperature: float = 0.3) -> str:
        # single-shot fallback path — same script/raise behavior.
        return await self.generate_messages([{"role": "user", "content": prompt}], temperature=temperature)


async def scenario_tool_loop_happy_path(duck, sqlite) -> None:
    print("\n[1] tool loop: calc -> price_history -> unknown tool -> final bullets")
    from app.edge.strategist_tools import run_tool_loop

    llm = FakeLlm(replies=[
        '```json\n{"tool": "calc", "args": {"expression": "mean([1, 2, 3]) * 2"}}\n```',
        'sure, checking the tape\n```json\n{"tool": "price_history", '
        '"args": {"symbol": "SPY", "bars": 30}}\n```',
        '{"tool": "not_a_real_tool", "args": {}}',
        "- Regime neutral, base mix holds — no strong tilts this run.\n"
        "- SPY 30d tape reviewed, nothing extreme.\n"
        "- Calc sanity-checked (mean * 2), consistent with the composite read.",
    ])
    result = {
        "regime": "neutral", "score": 0.0, "buckets": [], "equity_tilt": None,
        "signals": [{"key": "composite", "label": "Macro composite", "value": 0.0,
                     "detail": "regime neutral", "asof": "2025-01-01", "stale": False}],
    }
    notes, trace = await run_tool_loop(llm, duck, sqlite, result, max_calls=6, budget_seconds=60)

    check("llm called exactly 4 times (3 tool calls + final)", llm.calls == 4)
    check("3-8 notes parsed", 3 <= len(notes) <= 8)
    check("notes are non-empty strings", all(isinstance(n, str) and n for n in notes))
    check("trace has 3 entries", len(trace) == 3)
    if len(trace) == 3:
        check("trace[0] is calc, ok", trace[0]["tool"] == "calc" and trace[0]["ok"] is True)
        check("trace[1] is price_history, ok", trace[1]["tool"] == "price_history" and trace[1]["ok"] is True)
        check("trace[2] is unknown tool, NOT ok (error fed back, loop continued)",
              trace[2]["tool"] == "not_a_real_tool" and trace[2]["ok"] is False)
        check("unknown-tool trace summary mentions the error",
              "unknown tool" in trace[2]["summary"].lower())
    else:
        check("trace[0] is calc, ok", False)
        check("trace[1] is price_history, ok", False)
        check("trace[2] is unknown tool, NOT ok (error fed back, loop continued)", False)
        check("unknown-tool trace summary mentions the error", False)


async def scenario_fallback_chain(duck, sqlite) -> None:
    print("\n[2] total LLM failure -> fallback chain reaches template notes")
    from app.ws.hub import ConnectionManager
    from app.edge.strategist import StrategistService, compute_strategist

    llm = FakeLlm(raises=True)
    hub = ConnectionManager()
    svc = StrategistService(
        duck, sqlite, hub, llm,
        sectors=["XLK", "XLF"], benchmark="SPY",
        tools_enabled=True, tool_calls=6,
    )
    result = compute_strategist(duck, sqlite, ["XLK", "XLF"], "SPY")
    notes, model, trace = await svc._notes(result)  # noqa: SLF001 — smoke test, exercising private fallback chain

    check("model falls back to 'template'", model == "template")
    check("template notes non-empty", len(notes) >= 1)
    check("tool_trace empty on total failure (no successful calls)", trace == [])
    check("llm.generate_messages was actually invoked (raised, wasn't skipped)", llm.calls >= 1)

    # tools_enabled=False must reproduce exactly today's (pre-tool) behavior:
    # single-shot generate() is tried, then template — never run_tool_loop.
    llm2 = FakeLlm(raises=True)
    svc2 = StrategistService(
        duck, sqlite, hub, llm2,
        sectors=["XLK", "XLF"], benchmark="SPY",
        tools_enabled=False, tool_calls=6,
    )
    notes2, model2, trace2 = await svc2._notes(result)  # noqa: SLF001
    check("strategist_tools=False also falls back to template", model2 == "template")
    check("strategist_tools=False calls llm.generate exactly once (no tool loop)", llm2.calls == 1)


def scenario_safe_calc_security() -> None:
    print("\n[3] safe_calc security + correctness")
    from app.edge.strategist_tools import safe_calc

    injections = [
        "__import__('os').system('true')",
        "().__class__",
        "().__class__.__bases__",
        "open('x')",
        "[c for c in ().__class__.__mro__]",
        "x",
        "x + 1",
        "'a string'",
        "'a' + 'b'",
        "__builtins__",
        "eval('1')",
        "exec('1')",
        "getattr(1, '__class__')",
        "(lambda: 1)()",
        "1; 2",
        "import os",
    ]
    for expr in injections:
        try:
            safe_calc(expr)
            check(f"rejected: {expr!r}", False)
        except ValueError:
            check(f"rejected: {expr!r}", True)
        except Exception as exc:
            # safe_calc's contract is "raise ValueError or return a value" —
            # anything else (e.g. a bare SyntaxError escaping) is a bug.
            check(f"rejected: {expr!r} (wrong exception type {type(exc).__name__}: {exc})", False)

    try:
        result = safe_calc("mean([1, 2, 3]) * 2")
        check("mean([1,2,3]) * 2 == 4", result == 4)
    except Exception as exc:
        check(f"mean([1,2,3]) * 2 == 4 (raised {exc!r})", False)

    try:
        result = safe_calc("sqrt(16) + std([2, 4, 4, 4, 5, 5, 7, 9])")
        check("sqrt/std composite expression evaluates", isinstance(result, float))
    except Exception as exc:
        check(f"sqrt/std composite expression evaluates (raised {exc!r})", False)

    try:
        result = safe_calc("pct_change([100, 110, 99])")
        check("pct_change returns a list", isinstance(result, list) and len(result) == 2)
    except Exception as exc:
        check(f"pct_change returns a list (raised {exc!r})", False)


async def main() -> int:
    from app.db.duck import DuckStore
    from app.db.sqlite import SqliteStore
    from app.db.schema import init_all

    scenario_safe_calc_security()  # no DB needed

    with tempfile.TemporaryDirectory() as tmp:
        duck = DuckStore(Path(tmp) / "smoke.duckdb")
        sqlite = SqliteStore(Path(tmp) / "smoke.db")
        init_all(duck, sqlite)
        seed_db(duck)
        print("seeded synthetic SPY bars + macro points + one news item")

        await scenario_tool_loop_happy_path(duck, sqlite)
        await scenario_fallback_chain(duck, sqlite)

        duck.close()
        sqlite.close()

    print()
    ok = all(passed for _, passed in CHECKS)
    n_pass = sum(1 for _, p in CHECKS if p)
    print(f"{n_pass}/{len(CHECKS)} checks passed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
