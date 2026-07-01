"""Interactive Brokers broker adapter — a REAL funded account, read-only for now.

This mirrors ``AlpacaPaperBroker``'s method surface so ``BrokerState`` and every
downstream reader (both bots, the optimizer, /api/bot/portfolio) work unchanged.
The difference is the wire protocol and the stakes.

CONNECTION — there are no IBKR "API keys". This talks to the **Client Portal Web
API**, a REST service exposed by IBKR's *Client Portal Gateway* that you run
locally and log into with your normal IBKR username/password in a browser. Once
that session is live, the gateway answers unauthenticated localhost REST calls on
``https://localhost:5000/v1/api`` — so the only config is the base URL and (optionally)
which account to read. Setup:

    1. Download the Client Portal Gateway (clientportal.gw) from IBKR.
    2. Run ``bin/run.sh root/conf.yaml`` (mac/linux) — it listens on :5000.
    3. Open https://localhost:5000 in a browser and log in (accept the self-signed
       cert warning). Keep that session alive; the gateway proxies to IBKR.
    4. Set MARKET_BROKER_BACKEND=ibkr and (optionally) MARKET_IBKR_ACCOUNT_ID.

The gateway's TLS cert is self-signed for localhost, so this adapter uses its OWN
httpx client with verification disabled — scoped to localhost only, never the
shared HttpClient that talks to the public internet.

SAFETY — this is a live margin account, not Alpaca paper. Reads always work.
Order WRITES are implemented but gated by two independent switches:

  * an IBKR **paper** account (id prefixed "DU") may trade freely — test here first;
  * an IBKR **live** account (id prefixed "U") refuses every write UNLESS
    ``ibkr_allow_live`` (env MARKET_IBKR_ALLOW_LIVE) is explicitly True.

Even armed, the bot's own per-sleeve enable toggle still gates whether it submits
anything. So a live order needs BOTH env arming AND the runtime toggle — the
"never wire an autonomous loop straight to a live key" rule, enforced in code.

WRITE-PATH CAVEAT — order placement was authored against IBKR's documented
Client Portal order schema but could NOT be tested against a live gateway here.
The stop/bracket field mappings (STP price vs auxPrice, bracket parentId linking)
in particular MUST be verified on a DU paper account before pointing at real money.
``scripts/ibkr_check.py`` prints raw-vs-translated output for that verification.

IBKR's JSON shapes differ from Alpaca's; every read is translated into the same
Alpaca-shaped dicts the rest of the code already consumes (``equity``,
``buying_power``, ``qty``, ``market_value``, ``filled_avg_price``, ...). Where a
concept has no clean IBKR equivalent (e.g. Alpaca's prior-close ``last_equity``)
the field is filled conservatively and flagged in a comment.

IBKR's JSON shapes differ from Alpaca's; every read is translated into the same
Alpaca-shaped dicts the rest of the code already consumes (``equity``,
``buying_power``, ``qty``, ``market_value``, ``filled_avg_price``, ...). Where a
concept has no clean IBKR equivalent (e.g. Alpaca's prior-close ``last_equity``)
the field is filled conservatively and flagged in a comment.
"""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.trading.broker import BrokerError

log = logging.getLogger("market.trading.ibkr")

_ET = ZoneInfo("America/New_York")

# IBKR assetClass -> Alpaca asset_class, best-effort.
_ASSET_CLASS = {
    "STK": "us_equity",
    "ETF": "us_equity",
    "FUND": "us_equity",
    "CRYPTO": "crypto",
    "OPT": "us_option",
    "FUT": "future",
    "CASH": "forex",
    "BOND": "bond",
}

# Alpaca order_type -> IBKR Client Portal orderType code.
_OTYPE = {
    "market": "MKT",
    "limit": "LMT",
    "stop": "STP",
    "stop_limit": "STP_LIMIT",
    "trailing_stop": "TRAIL",
}

# Alpaca time_in_force -> IBKR tif code.
_TIF = {"day": "DAY", "gtc": "GTC", "ioc": "IOC", "opg": "OPG"}

# IBKR order status -> Alpaca-ish status the bot's reconciler understands.
_ORDER_STATUS = {
    "PreSubmitted": "accepted",
    "Submitted": "new",
    "PendingSubmit": "pending_new",
    "PendingCancel": "pending_cancel",
    "Filled": "filled",
    "Cancelled": "canceled",
    "Inactive": "canceled",
    "Rejected": "rejected",
}


def _num(d: dict | None, *keys: str) -> float | None:
    """First present numeric value across the given keys. Tolerates IBKR's two
    shapes — a bare number, or a nested ``{"amount": <n>}`` (used by summary
    endpoints). Returns None if none of the keys carry a usable number."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if isinstance(v, dict):
            v = v.get("amount")
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v.replace(",", ""))
            except ValueError:
                continue
    return None


class IbkrBroker:
    """Read-only Interactive Brokers adapter over the Client Portal Web API.

    Implements the same surface as :class:`AlpacaPaperBroker` so it drops into
    ``BrokerState`` unchanged. Writes are gated by ``allow_live`` for live accounts.
    """

    def __init__(
        self,
        base_url: str = "https://localhost:5000/v1/api",
        account_id: str = "",
        *,
        allow_live: bool = False,
        timeout: float = 20.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._account_id = (account_id or "").strip()
        self._allow_live = allow_live
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._conids: dict[str, int] = {}  # symbol -> conid, resolved once per process

    # ---- properties (mirror AlpacaPaperBroker) -----------------------------
    @property
    def enabled(self) -> bool:
        """True whenever the IBKR backend is selected — there is nothing to
        configure beyond the running gateway, so the adapter is always 'on'.
        A gateway that isn't running surfaces later as a read-time BrokerError,
        not as a silent dormant bot."""
        return True

    @property
    def is_paper(self) -> bool:
        """IBKR paper accounts are prefixed 'DU'; live accounts 'U'. When the
        account id is unknown we assume LIVE (the safe assumption for a funded
        margin account — the UI then honestly shows a live badge)."""
        return self._account_id.upper().startswith("DU")

    @property
    def base_url(self) -> str:
        return self._base

    @property
    def _c(self) -> httpx.AsyncClient:
        """Dedicated client. verify=False is safe here and ONLY here: every call
        goes to the localhost gateway whose cert is self-signed. Lazily built so
        construction needs no running event loop."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base,
                timeout=self._timeout,
                verify=False,
                follow_redirects=False,
                headers={"Accept": "application/json", "User-Agent": "market-bot"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- HTTP helpers ------------------------------------------------------
    async def _request(
        self, method: str, path: str, *, json_body: dict | None = None,
        params: dict | None = None,
    ) -> object:
        try:
            resp = await self._c.request(method, path, json=json_body, params=params)
        except httpx.HTTPError as exc:
            # Gateway not running / TLS / connect refused — the single most common
            # first-run failure. Give the operator the exact fix, not a stack trace.
            raise BrokerError(
                f"cannot reach IBKR Client Portal Gateway at {self._base} "
                f"({exc}). Is it running and logged in? Open the gateway URL in a "
                "browser and sign in, then retry.",
                None,
            ) from exc
        if resp.status_code == 401:
            raise BrokerError(
                "IBKR gateway session is not authenticated — open the gateway URL "
                "in a browser and log in (the session expires and must be renewed).",
                401,
            )
        if resp.status_code >= 400:
            raise BrokerError(_ibkr_error(resp), resp.status_code)
        try:
            return resp.json()
        except Exception:
            return {}

    async def _get(self, path: str, params: dict | None = None) -> object:
        return await self._request("GET", path, params=params)

    async def _ensure_account(self) -> str:
        """Resolve (and cache) the account id. The CP Web API also REQUIRES that
        ``/portfolio/accounts`` be hit once per session before any subaccount
        endpoint responds — so calling this first doubles as that priming call."""
        try:
            accounts = await self._get("/portfolio/accounts")
        except BrokerError:
            raise
        ids: list[str] = []
        if isinstance(accounts, list):
            ids = [str(a.get("accountId") or a.get("id") or "") for a in accounts if isinstance(a, dict)]
            ids = [i for i in ids if i]
        if self._account_id:
            if ids and self._account_id not in ids:
                raise BrokerError(
                    f"configured IBKR account {self._account_id!r} not in gateway "
                    f"accounts {ids}", None,
                )
            return self._account_id
        if not ids:
            raise BrokerError("IBKR gateway returned no accounts", None)
        self._account_id = ids[0]  # cache the first account for the process lifetime
        return self._account_id

    # ---- reads (translated to Alpaca shapes) -------------------------------
    async def get_account(self) -> dict:
        acct = await self._ensure_account()
        # BASE row of the ledger aggregates every currency into the account's base
        # currency — the cleanest single source for cash + net liquidation.
        ledger = await self._get(f"/portfolio/{acct}/ledger")
        base = {}
        if isinstance(ledger, dict):
            base = ledger.get("BASE") or ledger.get("USD") or next(
                (v for v in ledger.values() if isinstance(v, dict)), {}
            )
        # Summary carries the margin/buying-power figures the ledger lacks.
        try:
            summary = await self._get(f"/portfolio/{acct}/summary")
        except BrokerError:
            summary = {}
        summary = summary if isinstance(summary, dict) else {}

        equity = _num(base, "netliquidationvalue", "netliquidation") \
            or _num(summary, "netliquidation", "netliquidationvalue") or 0.0
        cash = _num(base, "cashbalance", "settledcash") \
            or _num(summary, "totalcashvalue", "availablefunds") or 0.0
        buying_power = _num(summary, "buyingpower", "availablefunds", "excessliquidity") or cash
        return {
            "id": acct,
            "account_number": acct,
            "currency": (base.get("currency") if isinstance(base, dict) else None) or "USD",
            "status": "ACTIVE",
            "equity": str(equity),
            # IBKR exposes no clean prior-close equity; report today's equity so the
            # day-change display reads ~0 rather than a bogus number. (Reads-only.)
            "last_equity": str(equity),
            "portfolio_value": str(equity),
            "cash": str(cash),
            "buying_power": str(buying_power),
            # Margin uses one buying-power pool; mirror it for the DT field.
            "daytrading_buying_power": str(buying_power),
            "trading_blocked": False,
            "account_blocked": False,
            "pattern_day_trader": False,
            "_source": "ibkr",
        }

    async def get_positions(self) -> list[dict]:
        acct = await self._ensure_account()
        out: list[dict] = []
        page = 0
        while True:  # positions are paginated; walk until a short/empty page
            data = await self._get(f"/portfolio/{acct}/positions/{page}")
            rows = data if isinstance(data, list) else []
            for p in rows:
                if not isinstance(p, dict):
                    continue
                pos = _num(p, "position") or 0.0
                if pos == 0.0:
                    continue  # closed lot still listed — skip
                out.append(self._translate_position(p, pos))
            if len(rows) < 100:
                break
            page += 1
            if page > 50:  # hard backstop against a misbehaving gateway
                break
        return out

    def _translate_position(self, p: dict, pos: float) -> dict:
        avg = _num(p, "avgPrice", "avgCost") or 0.0
        mkt_price = _num(p, "mktPrice") or 0.0
        mkt_value = _num(p, "mktValue")
        if mkt_value is None:
            mkt_value = pos * mkt_price
        unreal = _num(p, "unrealizedPnl") or 0.0
        cost_basis = abs(pos) * avg
        plpc = (unreal / cost_basis) if cost_basis else 0.0
        symbol = str(p.get("ticker") or p.get("contractDesc") or p.get("conid") or "").split()[0]
        return {
            "symbol": symbol,
            "qty": str(pos),                       # signed: negative = short (Alpaca semantics)
            "side": "long" if pos >= 0 else "short",
            "market_value": str(mkt_value),
            "cost_basis": str(cost_basis),
            "avg_entry_price": str(avg),
            "current_price": str(mkt_price),
            "unrealized_pl": str(unreal),
            "unrealized_plpc": str(plpc),
            "asset_class": _ASSET_CLASS.get(str(p.get("assetClass") or "").upper(), "us_equity"),
            "exchange": p.get("listingExchange") or p.get("exchange"),
            "conid": p.get("conid"),
        }

    async def get_clock(self) -> dict:
        """IBKR's CP Web API has no Alpaca-style clock. Approximate US equity
        regular trading hours (Mon–Fri 09:30–16:00 ET). Holidays are NOT modelled
        — a fine approximation for a read-only status badge; do not gate live
        orders on this once writes are armed."""
        now = datetime.now(_ET)
        is_open = (
            now.weekday() < 5
            and (now.hour, now.minute) >= (9, 30)
            and now.hour < 16
        )
        return {"timestamp": now.isoformat(), "is_open": is_open}

    async def list_orders(self, status: str = "all", limit: int = 100) -> list[dict]:
        data = await self._get("/iserver/account/orders")
        orders = []
        if isinstance(data, dict):
            orders = data.get("orders") or []
        elif isinstance(data, list):
            orders = data
        out = [self._translate_order(o) for o in orders if isinstance(o, dict)]
        if status and status not in ("all", ""):
            # Alpaca's 'open' == working orders. Map to non-terminal IBKR states.
            if status == "open":
                out = [o for o in out if o["status"] in ("new", "accepted", "pending_new", "partially_filled")]
            elif status == "closed":
                out = [o for o in out if o["status"] in ("filled", "canceled", "rejected")]
        return out[:limit]

    def _translate_order(self, o: dict) -> dict:
        total = _num(o, "totalSize", "sizeAndFills", "quantity") or 0.0
        filled = _num(o, "filledQuantity") or 0.0
        remaining = _num(o, "remainingQuantity")
        if total == 0.0 and remaining is not None:
            total = filled + remaining
        raw_status = str(o.get("status") or o.get("order_status") or "")
        status = _ORDER_STATUS.get(raw_status, raw_status.lower() or "unknown")
        # IBKR keeps a partial fill as "Submitted" (-> "new") with a partial
        # filledQuantity; Alpaca distinguishes "partially_filled", which the
        # reconciler checks by name. Upgrade so a working partial reads correctly.
        if status in ("new", "accepted") and 0.0 < filled < total:
            status = "partially_filled"
        return {
            "id": str(o.get("orderId") or o.get("order_id") or ""),
            "client_order_id": o.get("order_ref") or o.get("orderRef"),
            "symbol": str(o.get("ticker") or o.get("symbol") or ""),
            "side": str(o.get("side") or "").lower(),
            "qty": str(total),
            "filled_qty": str(filled),
            "filled_avg_price": str(_num(o, "avgPrice", "average_price") or 0.0),
            "limit_price": _num(o, "price", "limit_price"),
            "type": str(o.get("orderType") or o.get("order_type") or "").lower().replace(" ", "_"),
            "status": status,
            "_raw_status": raw_status,
        }

    async def get_order(self, order_id: str) -> dict:
        data = await self._get(f"/iserver/account/order/status/{order_id}")
        if isinstance(data, dict) and data:
            merged = dict(data)
            merged.setdefault("orderId", order_id)
            return self._translate_order(merged)
        # Fall back to scanning the live order list.
        for o in await self.list_orders("all", 500):
            if o["id"] == str(order_id):
                return o
        raise BrokerError(f"IBKR order {order_id} not found", 404)

    async def get_asset(self, symbol: str) -> dict:
        """Asset metadata (fractionable / shortable) is not exposed cleanly by the
        CP Web API, so this stays unimplemented and raises. That makes BrokerState
        fall back to its SAFE defaults: fractionable -> True (notional routes via
        IBKR ``cashQty``), shortability -> (False, False) i.e. FAIL-CLOSED — the
        bot will not short on IBKR until shortability is wired. A deliberate, safe
        limitation, not a bug."""
        raise BrokerError("IBKR asset metadata (fractionable/shortable) not wired", None)

    # ---- conid resolution --------------------------------------------------
    async def _resolve_conid(self, symbol: str) -> int:
        """Symbol -> IBKR contract id. Every order needs a numeric conid, not a
        ticker. Resolved once per symbol via secdef search and cached. Prefers an
        exact-symbol US stock match."""
        key = (symbol or "").upper().strip()
        if not key:
            raise BrokerError("cannot place order for empty symbol", None)
        if key in self._conids:
            return self._conids[key]
        data = await self._get("/iserver/secdef/search", params={"symbol": key, "secType": "STK"})
        matches = data if isinstance(data, list) else []
        chosen: int | None = None
        for m in matches:
            if not isinstance(m, dict):
                continue
            sym = str(m.get("symbol") or "").upper()
            has_stk = any(
                str(s.get("secType")).upper() == "STK"
                for s in (m.get("sections") or []) if isinstance(s, dict)
            )
            cid = m.get("conid")
            if cid is None:
                continue
            if sym == key and has_stk:      # exact ticker + is a stock -> best
                chosen = int(cid)
                break
            if chosen is None:              # fallback: first match with a conid
                chosen = int(cid)
        if chosen is None:
            raise BrokerError(f"no IBKR contract found for symbol {symbol!r}", None)
        self._conids[key] = chosen
        return chosen

    # ---- writes (GATED — live account requires explicit arming) ------------
    def _gate(self, acct: str) -> None:
        """The hard write gate. A DU (paper) account trades freely; a live account
        refuses unless ``allow_live`` was explicitly set. This is the one thing
        that must not fail open."""
        if urlsplit(self._base).scheme != "https":
            raise BrokerError(f"refusing to trade against non-https endpoint {self._base!r}", None)
        if not self.is_paper and not self._allow_live:
            raise BrokerError(
                f"refusing to place a LIVE order on real IBKR account {acct} "
                "(set MARKET_IBKR_ALLOW_LIVE=true only if you truly mean it)",
                None,
            )

    async def submit_order(
        self,
        symbol: str,
        side: str,
        *,
        qty: float | None = None,
        notional: float | None = None,
        order_type: str = "market",
        time_in_force: str = "day",
        client_order_id: str | None = None,
        limit_price: float | None = None,
        order_class: str | None = None,
        take_profit_price: float | None = None,
        stop_loss_price: float | None = None,
        stop_loss_limit_price: float | None = None,
        trail_percent: float | None = None,
        trail_price: float | None = None,
    ) -> dict:
        """Place one order on the IBKR account. Same signature/semantics as
        ``AlpacaPaperBroker.submit_order`` so callers are unchanged.

        Notional -> IBKR ``cashQty`` (needs fractional trading enabled on the
        account; else pass qty). Bracket -> a parent MKT plus TP (LMT) and SL (STP)
        children linked by ``parentId``, which IBKR groups OCA. Trailing -> a TRAIL
        order with trailingAmt. Raises BrokerError on rejection carrying IBKR's
        message; the caller records it and does not retry."""
        acct = await self._ensure_account()
        self._gate(acct)
        if (qty is None) == (notional is None):
            raise BrokerError("submit_order needs exactly one of qty / notional", None)

        conid = await self._resolve_conid(symbol)
        tif = _TIF.get(time_in_force.lower(), "DAY")
        coid = client_order_id or None
        side_u = side.upper()

        parent: dict = {"conid": conid, "orderType": _OTYPE.get(order_type, "MKT"),
                        "side": side_u, "tif": tif}
        if coid:
            parent["cOID"] = coid
        if qty is not None:
            parent["quantity"] = qty
        else:
            # cashQty is IBKR's dollar-notional (fractional) order — the faithful
            # analogue of Alpaca's notional. No market-data snapshot needed.
            parent["cashQty"] = round(float(notional), 2)
        if order_type == "limit" and limit_price is not None:
            parent["price"] = round(float(limit_price), 2)
        if order_type == "trailing_stop":
            if (trail_percent is None) == (trail_price is None):
                raise BrokerError("trailing_stop needs exactly one of trail_percent / trail_price", None)
            if qty is None:
                raise BrokerError("trailing_stop orders require a whole-share qty (no notional)", None)
            if trail_price is not None:
                parent["trailingType"] = "amt"
                parent["trailingAmt"] = round(float(trail_price), 2)
            else:
                parent["trailingType"] = "%"
                parent["trailingAmt"] = round(float(trail_percent), 4)

        orders = [parent]
        legs_meta: list[dict] = []
        if order_class in ("bracket", "oco", "oto") and (take_profit_price is not None or stop_loss_price is not None):
            if qty is None:
                raise BrokerError("bracket/oco/oto orders require a whole-share qty (no notional)", None)
            if not coid:
                raise BrokerError("bracket orders require a client_order_id to link child legs", None)
            exit_side = "SELL" if side_u == "BUY" else "BUY"
            if take_profit_price is not None:
                tp_coid = f"{coid}-tp"
                orders.append({"conid": conid, "orderType": "LMT", "side": exit_side, "tif": tif,
                               "quantity": qty, "price": round(float(take_profit_price), 2),
                               "cOID": tp_coid, "parentId": coid})
                legs_meta.append({"client_order_id": tp_coid, "symbol": symbol,
                                  "side": exit_side.lower(), "qty": str(qty), "type": "limit"})
            if stop_loss_price is not None:
                sl_coid = f"{coid}-sl"
                sl: dict = {"conid": conid, "side": exit_side, "tif": tif, "quantity": qty,
                            "cOID": sl_coid, "parentId": coid}
                if stop_loss_limit_price is not None:
                    # STP_LIMIT: price=limit, auxPrice=stop trigger.
                    sl["orderType"] = "STP_LIMIT"
                    sl["price"] = round(float(stop_loss_limit_price), 2)
                    sl["auxPrice"] = round(float(stop_loss_price), 2)
                else:
                    # STP (stop-market): CP carries the trigger in `price`.
                    sl["orderType"] = "STP"
                    sl["price"] = round(float(stop_loss_price), 2)
                orders.append(sl)
                legs_meta.append({"client_order_id": sl_coid, "symbol": symbol,
                                  "side": exit_side.lower(), "qty": str(qty), "type": "stop"})

        final = await self._place_orders(acct, orders)
        out = self._translate_submit(final, symbol=symbol, side=side, qty=qty,
                                     notional=notional, coid=coid)
        if legs_meta:
            out["legs"] = legs_meta
        return out

    async def _place_orders(self, acct: str, orders: list[dict]) -> dict:
        """POST the order array, then answer IBKR's confirmation-reply chain until
        it returns a real order id (or an error). Without answering the replies the
        order silently never lands — this is the single most common IBKR footgun."""
        resp = await self._request("POST", f"/iserver/account/{acct}/orders",
                                   json_body={"orders": orders})
        return await self._resolve_replies(resp, depth=0)

    async def _resolve_replies(self, resp: object, depth: int) -> dict:
        if depth > 8:
            raise BrokerError("IBKR order confirmation looped too many times — aborting", None)
        if not isinstance(resp, list) or not resp:
            if isinstance(resp, dict) and resp.get("error"):
                raise BrokerError(str(resp["error"]), None)
            raise BrokerError(f"unexpected IBKR order response: {resp!r}", None)
        first = resp[0]
        if not isinstance(first, dict):
            raise BrokerError(f"unexpected IBKR order response: {resp!r}", None)
        if first.get("error"):
            raise BrokerError(str(first["error"]), None)
        # Final acknowledgement.
        if first.get("order_id") or first.get("order_status"):
            return first
        # Confirmation prompt: {"id": <replyId>, "message": [...]}. Auto-confirm —
        # required for automation — but LOG every warning so a surprising message
        # (e.g. size vs net-liq) is visible in the record, not silently accepted.
        reply_id = first.get("id")
        if reply_id and "message" in first:
            for m in (first.get("message") or []):
                log.warning("IBKR order confirmation auto-accepted: %s", m)
            r2 = await self._request("POST", f"/iserver/reply/{reply_id}",
                                     json_body={"confirmed": True})
            return await self._resolve_replies(r2, depth + 1)
        raise BrokerError(f"unexpected IBKR order response: {resp!r}", None)

    def _translate_submit(self, final: dict, *, symbol: str, side: str,
                          qty: float | None, notional: float | None, coid: str | None) -> dict:
        """IBKR's submit ack -> the Alpaca-shaped order dict callers persist. A
        fresh market order has no fills yet; the bot reconciles fills later via
        get_order/list_orders, so filled_* start at 0."""
        raw_status = str(final.get("order_status") or "")
        status = _ORDER_STATUS.get(raw_status, raw_status.lower() or "accepted")
        return {
            "id": str(final.get("order_id") or ""),
            "client_order_id": coid,
            "symbol": symbol,
            "side": side.lower(),
            "qty": str(qty) if qty is not None else None,
            "notional": str(notional) if notional is not None else None,
            "filled_qty": "0",
            "filled_avg_price": "0",
            "status": status,
            "_raw": final,
        }

    async def cancel_all(self) -> int:
        """Cancel every working order (kill switch). IBKR has no bulk cancel, so
        fetch open orders and delete each. Returns the count cancelled."""
        acct = await self._ensure_account()
        self._gate(acct)
        cancelled = 0
        for o in await self.list_orders("open", 500):
            oid = o.get("id")
            if not oid:
                continue
            try:
                await self._request("DELETE", f"/iserver/account/{acct}/order/{oid}")
                cancelled += 1
            except BrokerError as exc:
                log.warning("IBKR cancel order %s failed: %s", oid, exc.reason)
        return cancelled


def _ibkr_error(resp: httpx.Response) -> str:
    """Best-effort extraction of the gateway's error message from a 4xx/5xx body."""
    try:
        data = resp.json()
        if isinstance(data, dict):
            return str(data.get("error") or data.get("message") or data)
    except Exception:
        pass
    return (resp.text or "IBKR gateway error")[:200]
