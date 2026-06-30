"""Phase 12 REST surface: /api/bot/* — the paper trading bot.

  GET  /api/bot/status            account + config + open proposals + orders
  GET  /api/bot/account           broker account + positions (ground truth)
  POST /api/bot/propose           generate proposals now (read-only, no orders)
  POST /api/bot/run               propose + auto-execute IFF mode=auto & enabled
  POST /api/bot/execute/{id}      the human gate: submit one proposed order
  POST /api/bot/reconcile         pull broker orders -> local state
  POST /api/bot/enable            kill switch ON
  POST /api/bot/disable           kill switch OFF (also cancels open orders)
  POST /api/bot/mode              {"mode": "proposal" | "auto"}
  GET  /api/bot/proposals         proposal history (optionally ?run_id=)
  GET  /api/bot/orders            order history

Reads/writes are intentionally manual (no scheduler job): nothing trades on a
timer. The handlers are thin — all logic lives in TradingBotService.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, Query, Request

from app.trading.broker import BrokerError

router = APIRouter(prefix="/api/bot", tags=["bot"])


def _bot(request: Request):
    return request.app.state.trading_bot


def _day(request: Request):
    return request.app.state.day_trader


def _optimizer(request: Request):
    return request.app.state.optimizer


@router.get("/status")
async def status(request: Request) -> dict:
    return await _bot(request).status()


@router.get("/account")
async def account(request: Request) -> dict:
    bot = _bot(request)
    if not request.app.state.broker.enabled:
        return {"ok": False, "detail": "Alpaca paper keys not configured"}
    try:
        acct = await request.app.state.broker.get_account()
        positions = await request.app.state.broker.get_positions()
    except BrokerError as exc:
        return {"ok": False, "detail": exc.reason, "status": exc.status}
    return {
        "ok": True,
        "account": bot._account_summary(acct),
        "positions": [
            {"symbol": p.get("symbol"), "qty": p.get("qty"),
             "market_value": p.get("market_value"),
             "avg_entry_price": p.get("avg_entry_price"),
             "unrealized_pl": p.get("unrealized_pl")}
            for p in positions
        ],
    }


@router.get("/portfolio")
async def portfolio(request: Request) -> dict:
    """Aggregated portfolio overview: total value + per-position bot attribution,
    winners, and exit plans. Powers the big Portfolio panel."""
    return await _bot(request).portfolio()


@router.post("/propose")
async def propose(request: Request) -> dict:
    return await _bot(request).propose()


@router.post("/run")
async def run(request: Request) -> dict:
    return await _bot(request).run()


@router.post("/execute/{proposal_id}")
async def execute(request: Request, proposal_id: int) -> dict:
    return await _bot(request).execute(proposal_id)


@router.post("/reconcile")
async def reconcile(request: Request) -> dict:
    return await _bot(request).reconcile()


@router.post("/enable")
async def enable(request: Request) -> dict:
    return await _bot(request).set_enabled(True)


@router.post("/disable")
async def disable(request: Request) -> dict:
    return await _bot(request).set_enabled(False)


@router.post("/mode")
async def set_mode(request: Request, mode: str = Body(..., embed=True)) -> dict:
    try:
        return await _bot(request).set_mode(mode)
    except ValueError as exc:
        return {"ok": False, "detail": str(exc)}


# --- SWING execution overhaul toggles (swing sleeve -> _bot) ---
@router.post("/managed-exits/enable")
async def managed_exits_enable(request: Request) -> dict:
    return await _bot(request).set_managed_exits(True)


@router.post("/managed-exits/disable")
async def managed_exits_disable(request: Request) -> dict:
    return await _bot(request).set_managed_exits(False)


@router.post("/posture-sizing/enable")
async def posture_sizing_enable(request: Request) -> dict:
    return await _bot(request).set_posture_sizing(True)


@router.post("/posture-sizing/disable")
async def posture_sizing_disable(request: Request) -> dict:
    return await _bot(request).set_posture_sizing(False)


@router.get("/proposals")
async def proposals(request: Request, run_id: str | None = Query(None),
                    limit: int = Query(100, le=500)) -> dict:
    sqlite = request.app.state.sqlite
    from app.trading.bot import TradingBotService, proposals_for_run

    if run_id:
        return {"run_id": run_id, "proposals": proposals_for_run(sqlite, run_id)}
    rows = sqlite.fetchall(
        "SELECT * FROM bot_proposals ORDER BY id DESC LIMIT ?", [limit]
    )
    return {"proposals": [TradingBotService._proposal_row(r) for r in rows]}


# --- Phase 13: optimizer + day sleeve ---
@router.get("/optimizer")
async def optimizer(request: Request) -> dict:
    return _optimizer(request).latest()


@router.post("/optimizer/run")
async def optimizer_run(request: Request) -> dict:
    return await _optimizer(request).run()


@router.get("/day/status")
async def day_status(request: Request) -> dict:
    return await _day(request).status()


@router.get("/day/plan")
async def day_plan(request: Request) -> dict:
    """The deterministic intraday plan the fast loop trades off (regime + vol →
    stop/take-profit %, size scale, hedge requirement). No LLM."""
    return await _day(request).intraday_plan()


@router.post("/day/run")
async def day_run(request: Request) -> dict:
    return await _day(request).run()


@router.post("/day/enable")
async def day_enable(request: Request) -> dict:
    return await _day(request).set_enabled(True)


@router.post("/day/disable")
async def day_disable(request: Request) -> dict:
    return await _day(request).set_enabled(False)


@router.post("/day/hedge/enable")
async def day_hedge_enable(request: Request) -> dict:
    return await _day(request).set_hedge_enabled(True)


@router.post("/day/hedge/disable")
async def day_hedge_disable(request: Request) -> dict:
    return await _day(request).set_hedge_enabled(False)


@router.post("/day/soft-stop/enable")
async def day_soft_stop_enable(request: Request) -> dict:
    return await _day(request).set_soft_stop(True)


@router.post("/day/soft-stop/disable")
async def day_soft_stop_disable(request: Request) -> dict:
    return await _day(request).set_soft_stop(False)


@router.post("/day/shorts/enable")
async def day_shorts_enable(request: Request) -> dict:
    return await _day(request).set_short_enabled(True)


@router.post("/day/shorts/disable")
async def day_shorts_disable(request: Request) -> dict:
    return await _day(request).set_short_enabled(False)


@router.post("/day/pairs/enable")
async def day_pairs_enable(request: Request) -> dict:
    return await _day(request).set_pairs_enabled(True)


@router.post("/day/pairs/disable")
async def day_pairs_disable(request: Request) -> dict:
    return await _day(request).set_pairs_enabled(False)


@router.get("/day/review")
async def day_review_get(request: Request, date: str | None = Query(None)) -> dict:
    """Latest end-of-day review (or ?date=YYYY-MM-DD). {} if none persisted."""
    from app.trading.day_review import latest_review

    return latest_review(request.app.state.sqlite, date)


@router.post("/day/review/run")
async def day_review_run(request: Request, date: str | None = Query(None)) -> dict:
    """Run the learning-loop review now (backfills P&L + writes day_review)."""
    import functools

    from app.trading.day_review import review_day

    state = request.app.state
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        functools.partial(
            review_day, state.sqlite, state.duck, state.settings,
            trade_date=date, llm=getattr(state, "llm", None),
        ),
    )


@router.get("/trades")
async def trades(request: Request, sleeve: str | None = Query(None),
                 status: str | None = Query(None)) -> dict:
    """Trade-level (paired entry/exit) view over bot_orders. FIFO-pairs filled
    buys with subsequent filled sells per symbol+sleeve; open lots are marked to
    the latest ts_price close. ?sleeve=swing|day  ?status=open|closed."""
    from app.trading.tradebook import list_trades
    from app.trading.guardrails import norm_symbol

    state = request.app.state
    # Live broker prices for the open-lot mark (accurate intraday; ts_price is a
    # stale daily close). Best-effort — fall back to ts_price if the broker is
    # off or the call fails.
    marks: dict[str, float] = {}
    broker_state = getattr(state, "broker_state", None)
    if broker_state is not None:
        try:
            for p in await broker_state.positions():
                px = p.get("current_price")
                if px is not None:
                    marks[norm_symbol(p.get("symbol") or "")] = float(px)
        except Exception:
            marks = {}
    return {"trades": list_trades(state.sqlite, sleeve=sleeve, status=status,
                                  duck=getattr(state, "duck", None), marks=marks)}


@router.get("/orders")
async def orders(request: Request, limit: int = Query(100, le=500)) -> dict:
    sqlite = request.app.state.sqlite
    rows = sqlite.fetchall(
        "SELECT id, proposal_id, client_order_id, broker_order_id, symbol, side, "
        "order_type, qty, notional, status, filled_qty, filled_avg_price, "
        "submitted_at, reconciled_at, error FROM bot_orders ORDER BY id DESC LIMIT ?",
        [limit],
    )
    return {"orders": [dict(r) for r in rows]}
