"""Standalone validation for the paper trading bot (no pytest in this project).

Run:  cd apps/api && uv run python _validate_bot.py
Exits non-zero on the first failed assertion. Temporary — safe to delete.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.db.duck import DuckStore
from app.db.schema import init_all
from app.db.sqlite import SqliteStore
from app.trading.broker import AlpacaPaperBroker, BrokerError
from app.trading.broker_cache import BrokerState
from app.trading.bot import TradingBotService, build_proposals, sleeve_holdings
from app.trading.optimizer import compute_split
from app.trading.signals import intraday_signal, portfolio_conflict
from app.trading.guardrails import (
    GuardrailConfig,
    buys_halted,
    daily_pnl_pct,
    evaluate_order,
    is_dust,
    norm_symbol,
)


class FakeOptimizer:
    def __init__(self, swing_pct=100.0):
        self._s = swing_pct
    def latest(self):
        return {"swing_pct": self._s, "day_pct": round(100 - self._s, 1), "regime": "neutral"}

CFG = GuardrailConfig(
    max_position_pct=15.0, max_position_notional=5000.0, min_order_notional=100.0,
    daily_loss_limit_pct=3.0, rebalance_band_pp=2.0, allow_live=False,
)
STOP = {"equities": 8.0, "metals": 10.0, "crypto": 20.0, "cash": 1.0}

PASS = 0


def ok(cond, msg):
    global PASS
    assert cond, "FAIL: " + msg
    PASS += 1
    print("  ok:", msg)


SNAPSHOT = {
    "as_of": "2026-06-24T10:00",
    "regime": "neutral",
    "buckets": [
        {"key": "equities", "weight_pct": 60, "holdings": [
            {"symbol": "AAPL", "kind": "stock", "weight_pct": 10, "score": 3.5,
             "evidence": ["2 insider buys in 14d", "Senate PTR buy"]},
            {"symbol": "XLK", "kind": "sector", "weight_pct": 30, "evidence": ["RRG leading"]},
        ]},
        {"key": "crypto", "weight_pct": 10, "holdings": [
            {"symbol": "BTC", "kind": "asset", "weight_pct": 10, "evidence": ["net-liq tailwind"]},
        ]},
        {"key": "cash", "weight_pct": 30, "holdings": [
            {"symbol": "SGOV", "kind": "asset", "weight_pct": 1, "evidence": ["park cash"]},
        ]},
    ],
}

ACCOUNT = {"equity": "10000", "last_equity": "10000", "buying_power": "8000",
           "cash": "5000", "status": "ACTIVE"}
POSITIONS = [
    {"symbol": "AAPL", "qty": "15", "market_value": "3000", "current_price": "200",
     "avg_entry_price": "180"},
    {"symbol": "TSLA", "qty": "5", "market_value": "1500", "current_price": "300"},
]


def test_norm():
    print("\n[norm_symbol]")
    ok(norm_symbol("BTC/USD") == "BTCUSD", "slash stripped")
    ok(norm_symbol("BTC-USD") == "BTCUSD", "dash stripped")
    ok(norm_symbol("aapl") == "AAPL", "upper-cased")


def test_guardrails():
    print("\n[evaluate_order]")
    allow = {"AAPL", "BTC/USD"}
    # allowlist
    b = evaluate_order(symbol="NVDA", side="buy", order_value=500,
                       current_position_value=0, equity=10000, buying_power=10000,
                       allowlist=allow, cfg=CFG)
    ok(any("allowlist" in x for x in b), "non-allowlisted symbol blocked")
    # position % cap
    b = evaluate_order(symbol="AAPL", side="buy", order_value=2000,
                       current_position_value=0, equity=10000, buying_power=10000,
                       allowlist=allow, cfg=CFG)
    ok(any("per-position cap" in x for x in b), "20% of equity blocked by % cap")
    # notional cap (raise % cap so only notional bites)
    cfg2 = GuardrailConfig(max_position_pct=100, max_position_notional=5000,
                           min_order_notional=100, daily_loss_limit_pct=3,
                           rebalance_band_pp=2)
    b = evaluate_order(symbol="AAPL", side="buy", order_value=6000,
                       current_position_value=0, equity=100000, buying_power=100000,
                       allowlist=allow, cfg=cfg2)
    ok(any("notional cap" in x for x in b), "$6000 blocked by notional cap")
    # buying power
    b = evaluate_order(symbol="AAPL", side="buy", order_value=2000,
                       current_position_value=0, equity=10000, buying_power=1000,
                       allowlist=allow, cfg=CFG)
    ok(any("available cash" in x for x in b), "order over available cash blocked (no margin)")
    # sells bypass caps + buying power
    b = evaluate_order(symbol="AAPL", side="sell", order_value=99999,
                       current_position_value=0, equity=10000, buying_power=0,
                       allowlist=allow, cfg=CFG)
    ok(b == [], "huge sell of allowlisted name passes (de-risking allowed)")

    print("\n[daily loss circuit breaker]")
    down = {"equity": "9600", "last_equity": "10000"}
    ok(abs(daily_pnl_pct(down) + 4.0) < 1e-9, "daily_pnl_pct = -4%")
    halt = buys_halted(down, CFG)
    ok(halt is not None, "buys halted when down 4% (limit 3%)")
    b = evaluate_order(symbol="AAPL", side="buy", order_value=100,
                       current_position_value=0, equity=9600, buying_power=9600,
                       allowlist=allow, cfg=CFG, buys_halted_reason=halt)
    ok(any("circuit breaker" in x for x in b), "buy blocked during halt")
    b = evaluate_order(symbol="AAPL", side="sell", order_value=100,
                       current_position_value=500, equity=9600, buying_power=0,
                       allowlist=allow, cfg=CFG, buys_halted_reason=halt)
    ok(b == [], "sell still allowed during halt")
    ok(buys_halted({"equity": "10000", "last_equity": "10000", "trading_blocked": True}, CFG),
       "broker trading_blocked halts buys")

    print("\n[is_dust]")
    ok(is_dust(50, CFG) and not is_dust(150, CFG), "dust floor at min_order_notional")


def test_build_proposals():
    print("\n[build_proposals]")
    watch = {"AAPL", "BTC/USD"}
    plan = build_proposals(SNAPSHOT, ACCOUNT, POSITIONS, watch, CFG, STOP)
    by = {p["symbol"]: p for p in plan["proposals"]}
    ok(plan["equity"] == 10000, "equity parsed")
    ok("AAPL" in by and by["AAPL"]["side"] == "sell", "AAPL is a sell (held 3000 > target 1000)")
    ok(abs(by["AAPL"]["qty"] - 10.0) < 1e-6, "AAPL sell qty = 2000/200 = 10")
    ok(by["AAPL"]["status"] == "proposed" and by["AAPL"]["blocks"] == [], "AAPL sell not blocked")
    ok(by["XLK"]["side"] == "buy" and by["XLK"]["status"] == "blocked", "XLK buy blocked")
    ok(any("cap" in x for x in by["XLK"]["blocks"]), "XLK blocked by position cap")
    ok(by["BTC/USD"]["status"] == "proposed" and by["BTC/USD"]["notional"] == 1000.0,
       "BTC mapped to BTC/USD, buy 1000, proposed")
    ok(by["SGOV"]["status"] == "skipped", "SGOV within rebalance band -> skipped")
    ok(by["AAPL"]["rationale"]["bear_case"], "AAPL proposal carries a bear case")
    ok(by["AAPL"]["rationale"]["invalidation"], "AAPL proposal carries an invalidation")
    ok(by["BTC/USD"]["max_loss_est"] == 200.0, "BTC max-loss est = 1000 * 20% = 200")
    untracked = {u["symbol"] for u in plan["untracked_positions"]}
    ok("TSLA" in untracked, "TSLA surfaced as untracked (never auto-sold)")
    n_actionable = sum(1 for p in plan["proposals"] if p["status"] == "proposed")
    ok(n_actionable == 2, "exactly 2 actionable (AAPL sell, BTC buy)")


# ---- fake http + broker -------------------------------------------------------
class FakeResp:
    def __init__(self, body):
        self._b = body
    def json(self):
        return self._b


class FakeHttp:
    def __init__(self, raise_transport=False):
        self.posts = []
        self.deletes = []
        self.raise_transport = raise_transport
    async def post(self, url, *, json_body=None, headers=None, follow_redirects=None):
        if self.raise_transport:
            raise httpx.ConnectError("connection refused")
        self.posts.append((url, json_body))
        return FakeResp({"id": "o1", "status": "filled", **(json_body or {})})
    async def delete(self, url, *, headers=None, params=None, follow_redirects=None):
        self.deletes.append(url)
        return FakeResp([])
    async def get_json(self, url, **kw):
        return {}


async def test_broker_gate():
    print("\n[broker hard live-key gate]")
    http = FakeHttp()
    live = AlpacaPaperBroker(http, "k", "s",
                             base_url="https://api.alpaca.markets", allow_live=False)
    ok(not live.is_paper, "live URL detected as non-paper")
    raised = False
    try:
        await live.submit_order("AAPL", "buy", notional=100)
    except BrokerError:
        raised = True
    ok(raised, "submit_order REFUSED on non-paper URL")
    ok(http.posts == [], "no HTTP call was made on the refused order")

    paper = AlpacaPaperBroker(http, "k", "s",
                              base_url="https://paper-api.alpaca.markets", allow_live=False)
    ok(paper.is_paper, "paper URL detected as paper")
    order = await paper.submit_order("AAPL", "buy", notional=100, client_order_id="bot-1")
    ok(order["id"] == "o1" and len(http.posts) == 1, "paper order submitted (1 HTTP call)")

    override = AlpacaPaperBroker(http, "k", "s",
                                 base_url="https://api.alpaca.markets", allow_live=True)
    await override.submit_order("AAPL", "buy", notional=100)
    ok(len(http.posts) == 2, "explicit allow_live override permits live URL")

    nokeys = AlpacaPaperBroker(http, "", "", base_url="https://paper-api.alpaca.markets")
    ok(not nokeys.enabled, "no keys -> broker dormant")
    raised = False
    try:
        await nokeys.get_account()
    except BrokerError:
        raised = True
    ok(raised, "get_account errors cleanly without keys")

    print("\n[is_paper host-bypass hardening]")
    bypasses = [
        "https://api.alpaca.markets/?ref=paper-api.alpaca.markets",
        "https://api.alpaca.markets/v2/paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets.evil.example",
        "https://paper-api.alpaca.markets@evil.example",
    ]
    for url in bypasses:
        b = AlpacaPaperBroker(http, "k", "s", base_url=url, allow_live=False)
        ok(not b.is_paper, f"host-bypass rejected: {url[:50]}")
        r = False
        try:
            await b.submit_order("AAPL", "buy", notional=100)
        except BrokerError:
            r = True
        ok(r, f"submit refused for bypass URL: {url[:40]}")
    # non-https paper URL still refused (credentials in headers)
    nohttps = AlpacaPaperBroker(http, "k", "s", base_url="http://paper-api.alpaca.markets")
    ok(nohttps.is_paper, "http paper host still parses as paper host")
    r = False
    try:
        await nohttps.submit_order("AAPL", "buy", notional=100)
    except BrokerError:
        r = True
    ok(r, "submit refused on non-https endpoint")

    print("\n[transport error -> BrokerError(status=None)]")
    bad = AlpacaPaperBroker(FakeHttp(raise_transport=True), "k", "s",
                            base_url="https://paper-api.alpaca.markets")
    err = None
    try:
        await bad.submit_order("AAPL", "buy", notional=100)
    except BrokerError as exc:
        err = exc
    ok(err is not None and err.status is None,
       "transport error wrapped as BrokerError with status=None (ambiguous)")


class FakeBroker:
    def __init__(self):
        self.enabled = True
        self.is_paper = True
        self.base_url = "https://paper-api.alpaca.markets"
        self.submitted = []
        self.canceled = 0
    async def get_account(self):
        return dict(ACCOUNT)
    async def get_positions(self):
        return [dict(p) for p in POSITIONS]
    async def get_clock(self):
        return {"is_open": True}
    async def submit_order(self, symbol, side, *, qty=None, notional=None,
                           order_type="market", time_in_force="day", client_order_id=None):
        o = {"id": f"brk-{len(self.submitted)+1}", "client_order_id": client_order_id,
             "status": "filled", "symbol": symbol, "side": side,
             "filled_qty": str(qty or 1), "filled_avg_price": "200"}
        self.submitted.append(o)
        return o
    async def list_orders(self, status="all", limit=100):
        if status == "open":
            return [o for o in self.submitted
                    if o.get("status") not in ("filled", "canceled", "rejected", "expired")]
        return list(self.submitted)
    async def cancel_all(self):
        self.canceled += 1
        return 0


class FakeHub:
    async def broadcast(self, topic, payload):
        pass


async def test_service():
    print("\n[TradingBotService integration]")
    with tempfile.TemporaryDirectory() as d:
        duck = DuckStore(Path(d) / "market.duckdb")
        sqlite = SqliteStore(Path(d) / "app.db")
        init_all(duck, sqlite)
        duck.execute(
            "INSERT INTO strategist_snapshots (ts, regime, model, detail) VALUES (?,?,?,?)",
            [datetime.now(timezone.utc).replace(tzinfo=None), "neutral", "template",
             json.dumps(SNAPSHOT)],
        )
        broker = FakeBroker()
        state = BrokerState(broker, ttl=0.0)
        bot = TradingBotService(duck, sqlite, FakeHub(), state, CFG, FakeOptimizer(100.0),
                                stop_pct=STOP, default_mode="proposal")

        res = await bot.propose()
        ok(res["ok"] and res["n_actionable"] == 2, "propose() -> 2 actionable")
        rows = sqlite.fetchall("SELECT symbol, status FROM bot_proposals")
        ok(len(rows) == 4, "4 proposals persisted to bot_proposals")

        aapl = sqlite.fetchone(
            "SELECT id FROM bot_proposals WHERE symbol='AAPL' AND status='proposed'")
        xlk = sqlite.fetchone(
            "SELECT id FROM bot_proposals WHERE symbol='XLK'")

        # execute refused while kill switch off
        r = await bot.execute(int(aapl["id"]))
        ok(not r["ok"] and "disabled" in r["detail"], "execute refused while disabled")
        ok(broker.submitted == [], "no order submitted while disabled")

        await bot.set_enabled(True)
        r = await bot.execute(int(aapl["id"]))
        ok(r["ok"], "execute AAPL after enabling")
        ok(len(broker.submitted) == 1, "exactly one broker order submitted")
        ok(broker.submitted[0]["client_order_id"] == f"bot-{aapl['id']}",
           "deterministic client_order_id (no double-submit)")
        st = sqlite.fetchone("SELECT status FROM bot_proposals WHERE id=?", [aapl["id"]])
        ok(st["status"] in ("submitted", "filled"), "AAPL proposal marked submitted/filled")

        # executing a blocked proposal is refused
        r = await bot.execute(int(xlk["id"]))
        ok(not r["ok"], "execute refused for blocked XLK proposal")

        # reconcile flips submitted->filled from broker truth
        await bot.reconcile()
        st = sqlite.fetchone("SELECT status FROM bot_proposals WHERE id=?", [aapl["id"]])
        ok(st["status"] == "filled", "reconcile() marks AAPL filled from broker")

        # disable cancels open orders (kill switch)
        await bot.set_enabled(False)
        ok(broker.canceled == 1, "disable triggers cancel_all (kill switch)")

        # auto mode: re-propose + auto-execute
        await bot.set_mode("auto")
        await bot.set_enabled(True)
        res = await bot.run()
        ok(res.get("auto_executed", 0) >= 1, "auto mode executes actionable proposals")

        status = await bot.status()
        ok(status["broker"]["is_paper"] is True, "status reports paper broker")
        ok(status["config"]["enabled"] is True, "status reports kill switch on")
        duck.close()
        sqlite.close()


def test_pending_sleeve():
    print("\n[stale / pending-aware + sleeve sizing]")
    snap = {"buckets": [{"key": "equities",
            "holdings": [{"symbol": "AAPL", "kind": "stock", "weight_pct": 10, "evidence": []}]}]}
    acct = {"equity": "10000", "last_equity": "10000", "buying_power": "10000"}
    watch = {"AAPL"}
    plan = build_proposals(snap, acct, [], watch, CFG, STOP,
                           open_orders=[{"symbol": "AAPL", "side": "buy", "notional": "1000"}])
    a = {p["symbol"]: p for p in plan["proposals"]}["AAPL"]
    ok(a["status"] == "skipped" and any("working" in b for b in a["blocks"]),
       "in-flight buy makes AAPL skipped (the stale-info fix)")
    a = {p["symbol"]: p for p in build_proposals(snap, acct, [], watch, CFG, STOP)["proposals"]}["AAPL"]
    ok(a["status"] == "proposed" and a["notional"] == 1000.0, "no pending -> buy 1000")
    a = {p["symbol"]: p for p in build_proposals(snap, acct, [], watch, CFG, STOP, sleeve_pct=50.0)["proposals"]}["AAPL"]
    ok(a["notional"] == 500.0, "sleeve_pct=50 scales target to the swing budget (500)")
    pos = [{"symbol": "AAPL", "qty": "15", "market_value": "3000", "current_price": "200"}]
    a = {p["symbol"]: p for p in build_proposals(snap, acct, pos, watch, CFG, STOP, exclude_qty={"AAPL": 15.0})["proposals"]}["AAPL"]
    ok(a["side"] == "buy", "exclude_qty carves the other sleeve's shares (bots don't fight)")


def test_sleeve_holdings():
    print("\n[sleeve_holdings attribution]")
    with tempfile.TemporaryDirectory() as d:
        duck = DuckStore(Path(d) / "m.duckdb")
        sq = SqliteStore(Path(d) / "a.db")
        init_all(duck, sq)
        for sym, side, q, sleeve in [("AAPL", "buy", 5, "swing"), ("AAPL", "buy", 3, "swing"),
                                     ("AAPL", "sell", 2, "swing"), ("BTC/USD", "buy", 1, "day")]:
            sq.execute("INSERT INTO bot_orders (symbol, side, status, filled_qty, sleeve) "
                       "VALUES (?,?,?,?,?)", [sym, side, "filled", q, sleeve])
        sw = sleeve_holdings(sq, "swing")
        dy = sleeve_holdings(sq, "day")
        ok(abs(sw.get("AAPL", 0) - 6) < 1e-9, "swing AAPL net = 5+3-2 = 6")
        ok(abs(dy.get("BTCUSD", 0) - 1) < 1e-9, "day BTC/USD net = 1 (normalized key)")
        ok("AAPL" not in dy, "sleeves are isolated")
        duck.close()
        sq.close()


def test_optimizer():
    print("\n[portfolio optimizer split]")
    with tempfile.TemporaryDirectory() as d:
        duck = DuckStore(Path(d) / "m.duckdb")
        sq = SqliteStore(Path(d) / "a.db")
        init_all(duck, sq)
        duck.execute("INSERT INTO macro_composite (ts, score, regime, detail) VALUES (?,?,?,?)",
                     [datetime(2026, 6, 24), 1.0, "risk-on", "{}"])
        s = compute_split(duck, 5.0, 10.0)
        ok(5.0 <= s["day_pct"] <= 10.0, "day_pct clamped to [5,10]")
        ok(abs(s["swing_pct"] + s["day_pct"] - 100) < 1e-6, "swing + day = 100")
        ok(s["day_pct"] > 7.5, "risk-on tilts the day sleeve above midpoint")
        duck.execute("INSERT INTO macro_composite (ts, score, regime, detail) VALUES (?,?,?,?)",
                     [datetime(2026, 6, 25), -2.0, "stress", "{}"])
        ok(compute_split(duck, 5.0, 10.0)["day_pct"] == 5.0, "stress regime -> day floor")
        duck.close()
        sq.close()


def test_day_signals():
    print("\n[day-trade signals + portfolio conflict]")
    up = [{"o": 100 + i * 0.1, "h": 100 + i * 0.1, "l": 99.9 + i * 0.1,
           "c": 100 + i * 0.1, "v": 1000, "vw": 100 + i * 0.1} for i in range(11)]
    s = intraday_signal(up, breakout_buffer_pct=0.05, momentum_min_pct=0.3, reversion_z=2.0)
    ok(s["kind"] == "momentum" and s["direction"] == "buy", "uptrend into the high -> momentum buy")
    rev = [{"o": 101, "h": 101, "l": 101, "c": 101, "v": 1000, "vw": 101} for _ in range(9)] + \
          [{"o": 99, "h": 99, "l": 99, "c": 99, "v": 1000, "vw": 99}]
    s = intraday_signal(rev, breakout_buffer_pct=0.05, momentum_min_pct=0.3, reversion_z=2.0)
    ok(s["direction"] == "buy" and s["kind"] == "reversion", "stretched below VWAP -> reversion buy")
    flat = [{"o": 100, "h": 100, "l": 100, "c": 100, "v": 1000, "vw": 100} for _ in range(10)]
    ok(intraday_signal(flat, breakout_buffer_pct=0.05, momentum_min_pct=0.3,
                       reversion_z=2.0)["direction"] == "none", "flat tape -> no setup")
    ok(portfolio_conflict(symbol="AAPL", side="buy", swing_value=1000, equity=10000) is not None,
       "day buy blocked when swing already holds >=8%")
    ok(portfolio_conflict(symbol="AAPL", side="buy", swing_value=500, equity=10000) is None,
       "day buy ok when swing holds <8%")
    ok(portfolio_conflict(symbol="AAPL", side="sell", swing_value=9999, equity=10000) is None,
       "sells never conflict (de-risking)")


async def main():
    test_norm()
    test_guardrails()
    test_build_proposals()
    test_pending_sleeve()
    test_sleeve_holdings()
    test_optimizer()
    test_day_signals()
    await test_broker_gate()
    await test_service()
    print(f"\nALL {PASS} CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
