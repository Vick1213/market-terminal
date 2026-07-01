"""Alpaca paper trading client — the bot's only door to placing orders.

Distinct from ``ingest/alpaca.py`` (market DATA, data.alpaca.markets): this
talks to the TRADING API (paper-api.alpaca.markets) to read the account, read
positions, and submit/cancel orders. It goes through the shared ``HttpClient``
so it inherits the same rate limiting, retries, and source-health watchdog.

THE hard safety guarantee: ``submit_order`` and ``cancel_all`` refuse to run
unless the configured base URL is the paper endpoint, UNLESS ``allow_live`` was
explicitly set (default False). A live key wired to an autonomous loop is the
single failure mode behind every blow-up story in the recon — this class makes
that the one thing you cannot do by accident.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("market.trading.broker")

_PAPER_HOST = "paper-api.alpaca.markets"


class BrokerError(RuntimeError):
    """A broker call failed in a way the caller must surface, not retry.

    ``status`` is the HTTP status (or None for client-side guardrail refusals);
    ``reason`` is Alpaca's human-readable message where available.
    """

    def __init__(self, reason: str, status: int | None = None) -> None:
        self.reason = reason
        self.status = status
        super().__init__(reason if status is None else f"[{status}] {reason}")


class AlpacaPaperBroker:
    def __init__(
        self,
        http,
        key_id: str,
        secret: str,
        base_url: str = "https://paper-api.alpaca.markets",
        *,
        allow_live: bool = False,
    ) -> None:
        self._http = http
        self._key_id = key_id
        self._secret = secret
        self._base = base_url.rstrip("/")
        self._allow_live = allow_live

    # ---- properties --------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """True when keys are configured. No keys -> the bot is dormant."""
        return bool(self._key_id and self._secret)

    @property
    def is_paper(self) -> bool:
        """True only when the base URL's HOST is exactly the Alpaca paper
        endpoint. Substring matching is unsafe — 'api.alpaca.markets/?x=paper-
        api.alpaca.markets' or 'paper-api.alpaca.markets.evil.example' would
        slip a live/attacker host past the gate — so compare the parsed host."""
        return (urlsplit(self._base).hostname or "").lower() == _PAPER_HOST

    @property
    def base_url(self) -> str:
        return self._base

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._key_id,
            "APCA-API-SECRET-KEY": self._secret,
            "Accept": "application/json",
        }

    def _assert_can_trade(self) -> None:
        """The hard gate. Any write (order submit / cancel) passes through here."""
        if not self.enabled:
            raise BrokerError("Alpaca paper keys not configured", None)
        if urlsplit(self._base).scheme != "https":
            # Credentials ride in headers; never send them in cleartext.
            raise BrokerError(f"refusing to trade against non-https endpoint {self._base!r}", None)
        if not self.is_paper and not self._allow_live:
            raise BrokerError(
                f"refusing to trade against non-paper endpoint {self._base!r} "
                "(set MARKET_BOT_ALLOW_LIVE_TRADING=true only if you truly mean it)",
                None,
            )

    # ---- reads (ground truth) ---------------------------------------------
    async def get_account(self) -> dict:
        if not self.enabled:
            raise BrokerError("Alpaca paper keys not configured", None)
        return await self._get_json("/v2/account")

    async def get_positions(self) -> list[dict]:
        if not self.enabled:
            raise BrokerError("Alpaca paper keys not configured", None)
        data = await self._get_json("/v2/positions")
        return data if isinstance(data, list) else []

    async def get_clock(self) -> dict:
        if not self.enabled:
            raise BrokerError("Alpaca paper keys not configured", None)
        return await self._get_json("/v2/clock")

    async def list_orders(self, status: str = "all", limit: int = 100) -> list[dict]:
        if not self.enabled:
            raise BrokerError("Alpaca paper keys not configured", None)
        data = await self._get_json(
            "/v2/orders", params={"status": status, "limit": str(limit), "nested": "false"}
        )
        return data if isinstance(data, list) else []

    async def get_order(self, order_id: str) -> dict:
        """One order by its broker id — used to confirm an entry has FILLED before
        attaching a follow-on exit (a trailing stop can't be opened while the long
        buy is still working)."""
        if not self.enabled:
            raise BrokerError("Alpaca paper keys not configured", None)
        return await self._get_json(f"/v2/orders/{order_id}")

    async def get_asset(self, symbol: str) -> dict:
        """Asset metadata (tradable, fractionable, ...). Crypto uses the slashed
        form ('BTC/USD'); equities the plain ticker."""
        if not self.enabled:
            raise BrokerError("Alpaca paper keys not configured", None)
        return await self._get_json(f"/v2/assets/{symbol}")

    # ---- writes (gated) ----------------------------------------------------
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
        """Submit one order to the PAPER account. Exactly one of qty/notional.

        Bracket / OCO / OTO exits: pass ``order_class='bracket'`` (or 'oto') with
        ``take_profit_price`` and/or ``stop_loss_price`` — Alpaca attaches the
        protective legs (OCO) so the position auto-exits at the stop or target,
        the user's "fast exit with a stop loss + take profit". Advanced order
        classes require a WHOLE-SHARE ``qty`` (no fractional / notional) and only
        work on EQUITIES — crypto is market/limit/long-only on Alpaca, so the
        caller must not pass a bracket for a crypto symbol.

        Trailing stops: pass ``order_type='trailing_stop'`` with exactly one of
        ``trail_percent`` (e.g. 1.5 → trails 1.5% below the high-water mark) or
        ``trail_price`` (a dollar give-back). Used for the momentum exit so winners
        run while a fixed stop distance below the peak protects the gain. Like
        brackets, trailing stops are EQUITIES-only and need a whole-share ``qty``.

        Raises BrokerError on any rejection (insufficient buying power, wash
        trade, PDT, bad symbol) carrying Alpaca's reason — callers record it,
        they do not retry.
        """
        self._assert_can_trade()
        if (qty is None) == (notional is None):
            raise BrokerError("submit_order needs exactly one of qty / notional", None)
        if order_class in ("bracket", "oco", "oto") and qty is None:
            raise BrokerError("bracket/oco/oto orders require a whole-share qty (no notional)", None)
        if order_type == "trailing_stop":
            if (trail_percent is None) == (trail_price is None):
                raise BrokerError("trailing_stop needs exactly one of trail_percent / trail_price", None)
            if qty is None:
                raise BrokerError("trailing_stop orders require a whole-share qty (no notional)", None)
        body: dict = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if qty is not None:
            body["qty"] = str(qty)
        else:
            body["notional"] = str(round(float(notional), 2))
        if limit_price is not None:
            body["limit_price"] = str(round(float(limit_price), 2))
        if client_order_id:
            body["client_order_id"] = client_order_id
        if order_class:
            body["order_class"] = order_class
        if take_profit_price is not None:
            body["take_profit"] = {"limit_price": str(round(float(take_profit_price), 2))}
        if stop_loss_price is not None:
            sl: dict = {"stop_price": str(round(float(stop_loss_price), 2))}
            if stop_loss_limit_price is not None:
                sl["limit_price"] = str(round(float(stop_loss_limit_price), 2))
            body["stop_loss"] = sl
        if trail_percent is not None:
            body["trail_percent"] = str(round(float(trail_percent), 4))
        if trail_price is not None:
            body["trail_price"] = str(round(float(trail_price), 2))
        try:
            resp = await self._http.post(
                f"{self._base}/v2/orders", json_body=body, headers=self._headers(),
                follow_redirects=False,  # a cross-host 3xx must NOT re-send the order
            )
        except httpx.HTTPStatusError as exc:
            raise BrokerError(_alpaca_error(exc.response), exc.response.status_code) from exc
        except httpx.HTTPError as exc:
            # Transport/timeout/connect error: the order MAY have landed. status
            # is None to flag "ambiguous" so the caller keeps a reconcilable row.
            raise BrokerError(f"broker transport error: {exc}", None) from exc
        return resp.json()

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel ONE resting order by its broker id (used when a manual trade
        close needs to clear the position's resting bracket / stop legs before
        flattening, so no orphan exit is left to fire against shares that are
        gone). Tolerant of the already-gone case (404/422 -> treated as done)."""
        self._assert_can_trade()
        if not order_id:
            return False
        try:
            await self._http.delete(
                f"{self._base}/v2/orders/{order_id}", headers=self._headers(),
                follow_redirects=False,
            )
            return True
        except httpx.HTTPStatusError as exc:
            # 404 (unknown) / 422 (not cancelable — already filled/canceled) mean
            # there's nothing left to cancel; that's success for our purposes.
            if exc.response.status_code in (404, 422):
                return True
            raise BrokerError(_alpaca_error(exc.response), exc.response.status_code) from exc
        except httpx.HTTPError as exc:
            raise BrokerError(f"broker transport error: {exc}", None) from exc

    async def cancel_all(self) -> int:
        """Cancel every open order (used by the kill switch). Returns the count
        Alpaca reported acting on; tolerant of the 'nothing to cancel' case."""
        self._assert_can_trade()
        try:
            resp = await self._http.delete(
                f"{self._base}/v2/orders", headers=self._headers(),
                follow_redirects=False,
            )
        except httpx.HTTPStatusError as exc:
            # 207 multi-status comes back as success; a real 4xx is surfaced.
            raise BrokerError(_alpaca_error(exc.response), exc.response.status_code) from exc
        except httpx.HTTPError as exc:
            raise BrokerError(f"broker transport error: {exc}", None) from exc
        try:
            body = resp.json()
            return len(body) if isinstance(body, list) else 0
        except Exception:
            return 0

    # ---- internals ---------------------------------------------------------
    async def _get_json(self, path: str, params: dict | None = None):
        try:
            return await self._http.get_json(
                f"{self._base}{path}",
                params=params,
                headers=self._headers(),
                conditional=False,
            )
        except httpx.HTTPStatusError as exc:
            raise BrokerError(_alpaca_error(exc.response), exc.response.status_code) from exc
        except httpx.HTTPError as exc:
            raise BrokerError(f"broker transport error: {exc}", None) from exc


def _alpaca_error(resp: httpx.Response) -> str:
    """Best-effort extraction of Alpaca's error message from a 4xx body."""
    try:
        data = resp.json()
        if isinstance(data, dict):
            return str(data.get("message") or data.get("error") or data)
    except Exception:
        pass
    return (resp.text or "broker error")[:200]
