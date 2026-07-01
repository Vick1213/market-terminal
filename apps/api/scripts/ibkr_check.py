"""IBKR Client Portal gateway check — verify the connection and field mappings.

READ-ONLY. Never places, modifies, or cancels an order. Run this once after
starting the Client Portal Gateway and logging in, to confirm:

  * the gateway is reachable and the session is authenticated,
  * which account(s) the session exposes,
  * that IbkrBroker's translation lands on the right fields — it prints the RAW
    IBKR JSON next to the Alpaca-shaped dict the bot will actually consume, so
    you can eyeball equity / buying_power / positions before trusting live data,
  * (optional) that a symbol resolves to a conid, so order routing will work.

Usage (from apps/api):

    .venv/bin/python scripts/ibkr_check.py
    .venv/bin/python scripts/ibkr_check.py --conid AAPL
    MARKET_IBKR_ACCOUNT_ID=U1234567 .venv/bin/python scripts/ibkr_check.py

It reads MARKET_IBKR_BASE_URL / MARKET_IBKR_ACCOUNT_ID from your env/config, so
it hits the same gateway the app would. Nothing here depends on
MARKET_BROKER_BACKEND — it always uses the IBKR adapter directly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.config import Settings
from app.trading.broker import BrokerError
from app.trading.ibkr import IbkrBroker


def _dump(label: str, obj: object) -> None:
    print(f"\n=== {label} ===")
    try:
        print(json.dumps(obj, indent=2, default=str)[:4000])
    except Exception:
        print(repr(obj)[:2000])


async def _main(symbol: str | None) -> int:
    settings = Settings()
    broker = IbkrBroker(
        base_url=settings.ibkr_base_url,
        account_id=settings.ibkr_account_id,
        allow_live=settings.ibkr_allow_live,
    )
    print(f"gateway base : {broker.base_url}")
    print(f"configured id: {settings.ibkr_account_id or '(auto-detect)'}")
    print(f"allow_live   : {settings.ibkr_allow_live}")

    # 1) auth + session (raw, tolerant — a 401 here means 'log in via the browser')
    try:
        auth = await broker._get("/iserver/auth/status")  # noqa: SLF001 (diagnostic)
        _dump("iserver/auth/status (raw)", auth)
    except BrokerError as exc:
        print(f"\n!! auth status failed: {exc.reason}")
        print("   -> Is the gateway running AND logged in? Open the base URL in a "
              "browser, sign in, then re-run.")
        await broker.aclose()
        return 1

    # 2) account resolution (also primes the CP session for subaccount endpoints)
    try:
        acct = await broker._ensure_account()  # noqa: SLF001
        print(f"\nresolved account: {acct}  (is_paper={broker.is_paper})")
    except BrokerError as exc:
        print(f"\n!! account resolution failed: {exc.reason}")
        await broker.aclose()
        return 1

    # 3) account: raw ledger + summary next to the translated Alpaca-shaped dict
    try:
        _dump("portfolio/{acct}/ledger (raw)", await broker._get(f"/portfolio/{acct}/ledger"))  # noqa: SLF001
    except BrokerError as exc:
        print(f"\n!! ledger failed: {exc.reason}")
    try:
        _dump("portfolio/{acct}/summary (raw)", await broker._get(f"/portfolio/{acct}/summary"))  # noqa: SLF001
    except BrokerError as exc:
        print(f"\n!! summary failed: {exc.reason}")
    try:
        _dump("get_account() -> Alpaca-shaped (what the bot reads)", await broker.get_account())
    except BrokerError as exc:
        print(f"\n!! get_account failed: {exc.reason}")

    # 4) positions: raw page 0 next to translated
    try:
        _dump("portfolio/{acct}/positions/0 (raw)", await broker._get(f"/portfolio/{acct}/positions/0"))  # noqa: SLF001
    except BrokerError as exc:
        print(f"\n!! raw positions failed: {exc.reason}")
    try:
        _dump("get_positions() -> Alpaca-shaped", await broker.get_positions())
    except BrokerError as exc:
        print(f"\n!! get_positions failed: {exc.reason}")

    # 5) live orders (translated)
    try:
        _dump("list_orders('all') -> Alpaca-shaped", await broker.list_orders("all", 50))
    except BrokerError as exc:
        print(f"\n!! list_orders failed: {exc.reason}")

    # 6) optional conid resolution (read-only — proves order routing would work)
    if symbol:
        try:
            cid = await broker._resolve_conid(symbol)  # noqa: SLF001
            print(f"\nconid({symbol}) = {cid}")
        except BrokerError as exc:
            print(f"\n!! conid resolution for {symbol} failed: {exc.reason}")

    await broker.aclose()
    print("\ndone — no orders were placed, modified, or cancelled.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Read-only IBKR gateway + mapping check")
    ap.add_argument("--conid", metavar="SYMBOL", default=None,
                    help="also resolve this ticker to an IBKR conid (read-only)")
    args = ap.parse_args()
    sys.exit(asyncio.run(_main(args.conid)))
