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


@router.post("/day/run")
async def day_run(request: Request) -> dict:
    return await _day(request).run()


@router.post("/day/enable")
async def day_enable(request: Request) -> dict:
    return await _day(request).set_enabled(True)


@router.post("/day/disable")
async def day_disable(request: Request) -> dict:
    return await _day(request).set_enabled(False)


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
