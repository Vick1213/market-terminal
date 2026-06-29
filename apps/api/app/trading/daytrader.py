"""DayTraderService — the fast, auto-executing sleeve.

Trades a small liquid universe (SPY/QQQ/NVDA/TSLA + BTC/ETH) on intraday
momentum-breakout and mean-reversion, with a major-news override and a
portfolio-conflict check so it never piles into what the swing book already
holds. Sized inside the DAY budget the optimizer hands it; auto-executes on the
paper account when armed (paper-only, guardrailed). Equity legs run only while
the market is open; crypto runs 24/7.

Data footprint per tick is ~2 batched calls (one intraday-bars request for the
equity set, one for crypto) plus the shared broker-state cache — deliberately
tiny so the fast loop can never get IP rate-limited.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timezone

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ingest.alpaca import alpaca_symbol, fetch_intraday_bars
from app.trading.bot import sleeve_holdings
from app.trading.broker import BrokerError
from app.trading.broker_cache import BrokerState
from app.trading.context import (
    DAY_HEDGE_SYMBOL, beta_for, build_market_context, build_portfolio_state, sector_for,
)
from app.trading.guardrails import GuardrailConfig, buys_halted, norm_symbol
from app.trading.intraday import UNHEDGEABLE_INDEX, build_plan, compute_bracket, hedge_for
from app.trading.optimizer import PortfolioOptimizer
from app.trading.signals import intraday_signal, major_news, portfolio_conflict
from app.ws.hub import ConnectionManager

log = logging.getLogger("market.trading.daytrader")

BOT_TOPIC = "bot"
# Assumed intraday adverse move per asset, for the illustrative max-loss only.
_DAY_STOP_PCT = {"equity": 2.0, "crypto": 5.0}

DISCLAIMER = (
    "Day sleeve: fast, auto-executed PAPER trades on intraday momentum / "
    "mean-reversion, sized inside the optimizer's day budget and hard caps. "
    "NOT financial advice; paper only."
)


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _last_close(bars: dict, sym: str) -> float | None:
    b = bars.get(sym) or bars.get(sym.replace("/", "")) or []
    return _to_float(b[-1].get("c")) if b else None


def _session_date() -> str:
    """US-market session date (ET, DST approximated as UTC-4) — groups a day's
    journal/review rows. Good enough for end-of-day batching."""
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")


def _sellable_qty(held_qty: float, broker_qty: float | None) -> float:
    """Largest qty we can actually sell. Capped at the broker's reported
    available balance (crypto fees are taken in-kind, so the fillable qty is a
    hair below what we bought) and floored to 6dp so the request never rounds
    *up* past what's available — which is what got ETH/USD sells rejected."""
    qty = held_qty if broker_qty is None else min(held_qty, broker_qty)
    return math.floor(max(0.0, qty) * 1_000_000) / 1_000_000


class DayTraderService:
    def __init__(
        self,
        duck: DuckStore,
        sqlite: SqliteStore,
        hub: ConnectionManager,
        broker: BrokerState,
        optimizer: PortfolioOptimizer,
        *,
        http,
        data_key_id: str,
        data_secret: str,
        settings,
    ) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._hub = hub
        self._broker = broker
        self._optimizer = optimizer
        self._http = http
        self._data_key = data_key_id
        self._data_secret = data_secret
        self._s = settings
        self._guard = GuardrailConfig(
            max_position_pct=settings.day_max_position_pct,
            max_position_notional=0.0,  # set per-run vs the day budget
            min_order_notional=settings.day_min_order_notional,
            daily_loss_limit_pct=settings.day_daily_loss_limit_pct,
            rebalance_band_pp=0.0,
            allow_live=settings.bot_allow_live_trading,
        )

    # ---- config / kill switch ---------------------------------------------
    def _config(self) -> dict:
        row = self._sqlite.fetchone("SELECT day_enabled FROM bot_config WHERE id = 1")
        return {"enabled": bool(row["day_enabled"]) if row else False}

    async def set_enabled(self, enabled: bool) -> dict:
        self._sqlite.execute(
            "UPDATE bot_config SET day_enabled = ?, updated_at = datetime('now') WHERE id = 1",
            [1 if enabled else 0],
        )
        log.info("day bot kill switch -> %s", "ENABLED" if enabled else "DISABLED")
        await self._broadcast("day_config")
        return self._config()

    def _universe(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for sym in self._s.day_universe:
            asset_class = "crypto" if "/" in sym else "equity"
            out.append((sym, asset_class))
        return out

    # ---- main loop ---------------------------------------------------------
    async def run(self) -> dict:
        if not self._broker.enabled:
            return {"ok": False, "detail": "Alpaca paper keys not configured", "config": self._config()}
        if not self._data_key:
            return {"ok": False, "detail": "Alpaca data keys not configured (MARKET_ALPACA_KEY_ID)"}
        cfg = self._config()
        if not cfg["enabled"]:
            # Halted — make NO data/broker calls (rate-limit hygiene).
            return {"ok": True, "config": cfg, "note": "day bot halted — enable to trade",
                    "actions": []}
        market_open = await self._broker.is_market_open()

        items = self._universe()
        crypto = [(s, a) for s, a in items if a == "crypto"]
        equities = [(s, a) for s, a in items if a == "equity"]
        active = crypto + (equities if market_open else [])
        if not active:
            return {"ok": True, "config": cfg, "market_open": market_open,
                    "note": "market closed and no crypto in universe", "actions": []}

        # Pull the hedge instruments' bars too (price only — not iterated for
        # signals) so hedged-bracket sizing knows their last price.
        active_syms = {s for s, _ in active}
        hedge_syms: set[str] = set()
        if market_open and self._s.day_hedged_enabled:
            for s, _a in equities:
                h = hedge_for(s)
                if h:
                    hedge_syms.add(h[0])
            hedge_syms -= active_syms
        fetch_list = active + [(h, "equity") for h in sorted(hedge_syms)]
        # The net-beta hedge instrument (SH) is NOT in the universe or the per-name
        # inverse-ETF set, so its price was never fetched — _net_beta_hedge always
        # saw sh_price=None and silently placed ZERO hedges. Pull its bars too.
        if (market_open and self._s.day_net_beta_enabled
                and DAY_HEDGE_SYMBOL not in active_syms
                and DAY_HEDGE_SYMBOL not in hedge_syms):
            fetch_list.append((DAY_HEDGE_SYMBOL, "equity"))

        try:
            bars = await fetch_intraday_bars(
                self._http, self._data_key, self._data_secret, fetch_list,
                minutes=self._s.day_intraday_lookback_min,
            )
            account = await self._broker.account()
            positions = await self._broker.positions()
        except BrokerError as exc:
            return {"ok": False, "detail": exc.reason, "config": cfg}

        # Deterministic intraday plan from regime + the vol-overlay forecast —
        # sets stop/take-profit %, size scale, and whether a hedge is required.
        # NO LLM in the fast loop (the user's ask).
        regime = self._latest_regime()
        vol_signal = await self._vol_signal()
        plan = build_plan(regime, vol_signal, base_stop_pct=self._s.day_stop_pct,
                          base_tp_pct=self._s.day_tp_pct, base_hedge_ratio=self._s.day_hedge_ratio,
                          stop_sigma=self._s.day_stop_sigma, tp_sigma=self._s.day_tp_sigma,
                          trail_sigma=self._s.day_trail_sigma,
                          reversion_stop_sigma=self._s.day_reversion_stop_sigma,
                          min_rr=self._s.day_min_rr)
        hedge_price = {h: _last_close(bars, h) for h in hedge_syms}

        equity = _to_float(account.get("equity")) or 0.0
        split = self._optimizer.latest()
        day_pct = float(split.get("day_pct") or 0.0)
        day_budget = equity * day_pct / 100.0
        day_hold = sleeve_holdings(self._sqlite, "day")
        swing_hold = sleeve_holdings(self._sqlite, "swing")
        pos_price = {norm_symbol(p.get("symbol", "")): _to_float(p.get("current_price"))
                     for p in positions}
        # Actually-sellable balance per name. Crypto fees are taken in-kind, so the
        # fillable balance sits a hair below the qty we bought — selling the
        # recorded fill qty gets rejected for "insufficient balance". qty_available
        # also nets out shares tied up in open orders.
        pos_avail = {norm_symbol(p.get("symbol", "")):
                     _to_float(p.get("qty_available") or p.get("qty"))
                     for p in positions}
        halted = buys_halted(account, self._guard)

        # --- coherent shared context (what the day trader now "knows") ----------
        strategist = None
        try:
            from app.edge.strategist import latest_snapshot
            strategist = latest_snapshot(self._duck)
        except Exception:
            strategist = None
        market_ctx = build_market_context(self._duck, vol_signal=vol_signal, strategist=strategist)
        portfolio = build_portfolio_state(account, positions, self._sqlite, day_budget=day_budget)
        # Day-sleeve capital in use = the sleeve's value bounded by ACTUAL broker
        # positions (incl. inverse-ETF hedges). Using portfolio.day_value rather
        # than the raw ledger means a desynced/phantom ledger entry (bracket exit
        # that filled but wasn't recorded) can NEVER again silently fill the budget
        # and freeze the day trader.
        deployed = portfolio.day_value
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        trade_date = _session_date()

        # --- 1) evaluate EVERY candidate (no orders yet) ------------------------
        candidates: list[dict] = []
        for sym, asset_class in active:
            mapped = alpaca_symbol(sym, asset_class)
            if mapped is None:
                continue
            asym = mapped[0]
            n = norm_symbol(asym)
            sig = intraday_signal(
                bars.get(asym) or bars.get(sym) or [],
                breakout_buffer_pct=self._s.day_breakout_buffer_pct,
                momentum_min_pct=self._s.day_momentum_min_pct,
                reversion_z=self._s.day_reversion_z,
            )
            price = sig.get("last") or pos_price.get(n)
            held_qty = day_hold.get(n, 0.0)
            swing_val = swing_hold.get(n, 0.0) * (price or 0.0)
            news = (major_news(self._duck, sym, max_age_min=self._s.day_news_max_age_min,
                               min_abs_score=self._s.day_news_min_abs_score,
                               min_outlets=self._s.day_news_min_outlets)
                    if asset_class == "equity" else None)
            decision = self._decide(
                sym=sym, asym=asym, asset_class=asset_class, sig=sig, price=price,
                held_qty=held_qty, broker_qty=pos_avail.get(n), day_budget=day_budget,
                deployed=deployed, swing_val=swing_val, equity=equity, halted=halted,
                news=news,
            )
            decision["price"] = price
            decision["sector"] = sector_for(asym)
            decision["conviction"] = self._conviction(decision, sig, market_ctx)
            candidates.append(decision)

        # --- 2) segmentation: rank buys by conviction, carve the day budget,
        #        cap per-tick / per-sector, respect the strategist's avoid list ---
        self._segment(candidates, portfolio)

        # --- 3) execute: exits/sells always go; selected longs go BRACKETED
        #        (no per-trade hedge — the net-beta hedge below carries the risk) -
        actions: list[dict] = []
        for d in candidates:
            out = d
            if d.get("act") and d.get("side") == "sell":
                if cfg["enabled"]:
                    d["submitted"] = await self._submit(d, d["asset_class"])
            elif d.get("selected"):
                out = self._build_long(d, plan, d.get("price"), d["asset_class"])
                # Only submit if _build_long actually produced a tradeable order
                # (it returns act=False when an equity can't be sized to a whole
                # bracketable share — never open a naked, stop-less day position).
                if out.get("act") and cfg["enabled"]:
                    out["submitted"] = await self._submit(out, d["asset_class"])
                    if out.get("submitted"):
                        deployed += out.get("notional") or 0.0
            actions.append(out)
            self._journal(run_id, trade_date, out, market_ctx)

        # --- 4) net-beta hedge ONCE (single SH position to a target, not a pile) -
        if cfg["enabled"] and self._s.day_net_beta_enabled and market_open:
            hedge = await self._net_beta_hedge(account, positions, bars)
            if hedge:
                actions.append(hedge)
                self._journal(run_id, trade_date, hedge, market_ctx)

        await self.reconcile()
        return {
            "ok": True, "config": cfg, "market_open": market_open,
            "day_pct": day_pct, "day_budget": round(day_budget, 2),
            "deployed": round(deployed, 2), "actions": actions,
            "market_context": market_ctx.to_dict(),
            "portfolio_state": portfolio.to_dict(),
            "disclaimer": DISCLAIMER,
        }

    def _decide(self, *, sym, asym, asset_class, sig, price, held_qty, broker_qty,
                day_budget, deployed, swing_val, equity, halted, news) -> dict:
        """Pure-ish decision for one symbol: hold / buy / sell, with reasons."""
        base = {"symbol": asym, "strategist_sym": sym, "asset_class": asset_class,
                "signal": sig, "news": news, "act": False, "side": None,
                "notional": None, "qty": None, "reason": ""}
        direction = sig.get("direction")
        strength = float(sig.get("strength") or 0.0)
        sell_qty = _sellable_qty(held_qty, broker_qty)

        # Major adverse news while holding -> exit regardless of the setup.
        if news and news["score"] < 0 and held_qty > 0:
            if sell_qty <= 0:
                return {**base, "reason": "adverse news but no sellable day balance"}
            return {**base, "act": True, "side": "sell", "qty": sell_qty,
                    "reason": f"major adverse news (score {news['score']:+.2f}) — exit day position"}

        if direction == "none" or strength < self._s.day_min_signal:
            return {**base, "reason": sig.get("detail", "no actionable setup")}

        if direction == "sell":
            if sell_qty <= 0:
                return {**base, "reason": "fade signal but no day position to trim"}
            return {**base, "act": True, "side": "sell", "qty": sell_qty,
                    "reason": f"{sig['detail']} — close day position"}

        # direction == buy
        if news and news["score"] < 0:
            return {**base, "reason": f"buy setup vetoed by adverse news ({news['score']:+.2f})"}
        conflict = portfolio_conflict(symbol=asym, side="buy", swing_value=swing_val, equity=equity)
        if conflict:
            return {**base, "reason": conflict}
        if held_qty > 0:
            return {**base, "reason": "already long this name in the day sleeve — no pyramiding"}
        if halted:
            return {**base, "reason": halted}
        if day_budget <= 0:
            return {**base, "reason": "day budget is zero under current conditions"}

        target = day_budget * (self._s.day_max_position_pct / 100.0) * min(1.0, strength / 3.0)
        remaining = max(0.0, day_budget - deployed)
        notional = min(target, remaining)
        if notional < self._s.day_min_order_notional:
            return {**base, "reason": (f"sized ${notional:,.0f} below ${self._s.day_min_order_notional:,.0f} "
                                       "min (or day budget full)")}
        stop = _DAY_STOP_PCT.get(asset_class, 3.0)
        return {**base, "act": True, "side": "buy", "notional": round(notional, 2),
                "max_loss_est": round(notional * stop / 100.0, 2),
                "reason": f"{sig['kind']} buy ({sig['detail']})"}

    # ---- Phase 2: conviction / segmentation / net-beta hedge ---------------
    def _conviction(self, decision: dict, sig: dict, mc) -> float:
        """Rank score for a BUY candidate, shaped by the shared context: the
        strategist's avoid list is a hard veto (0), its favored sectors get a
        boost, and an unfriendly tape down-weights (stress kills) momentum chasing.
        Non-actionable / non-buy candidates score 0."""
        if not (decision.get("act") and decision.get("side") == "buy"):
            return 0.0
        score = float(sig.get("strength") or 0.0)
        sec = (decision.get("sector") or "other").lower()
        sym = (decision.get("strategist_sym") or decision.get("symbol") or "").lower()

        def _hit(words: list[str]) -> bool:
            for w in words:
                w = (w or "").lower()
                if not w:
                    continue
                if w == sym or w in sec or sec in w:
                    return True
            return False

        if self._s.day_respect_strategist and _hit(mc.strategist_avoid):
            decision["reason"] += " | vetoed: strategist avoids this sleeve"
            return 0.0
        if _hit(mc.strategist_favor):
            score *= 1.25
        kind = sig.get("kind")
        if mc.stress_on and kind == "momentum":
            decision["reason"] += " | vetoed: no momentum-chasing in stress"
            return 0.0
        if not mc.risk_on and kind == "momentum":
            score *= 0.4
        return round(score, 3)

    def _segment(self, candidates: list[dict], portfolio) -> None:
        """Pick which BUY candidates actually run this tick. Marks each with
        ``selected`` and annotates skips. Ranks by conviction, then enforces:
        per-tick count cap, per-sector cap, and the remaining day budget — so the
        sleeve carves its small risk pool deliberately instead of acting on every
        name that twitches."""
        for c in candidates:
            c.setdefault("selected", False)
        buys = sorted((c for c in candidates if c.get("act") and c.get("side") == "buy"),
                      key=lambda c: c.get("conviction", 0.0), reverse=True)
        remaining = portfolio.day_remaining
        per_sector: dict[str, int] = {}
        picked = 0
        for c in buys:
            if c.get("conviction", 0.0) <= 0:
                continue  # reason already annotated by _conviction
            if picked >= self._s.day_max_new_positions:
                c["reason"] += " | skipped: hit max new positions this tick"
                continue
            sec = c.get("sector", "other")
            if per_sector.get(sec, 0) >= self._s.day_max_per_sector:
                c["reason"] += f" | skipped: {sec} sector cap this tick"
                continue
            notional = c.get("notional") or 0.0
            if notional > remaining + 1e-6:
                c["reason"] += f" | skipped: ${notional:,.0f} over ${remaining:,.0f} day budget left"
                continue
            c["selected"] = True
            picked += 1
            per_sector[sec] = per_sector.get(sec, 0) + 1
            remaining -= notional

    def _build_long(self, decision: dict, plan: dict, price, asset_class: str) -> dict:
        """A selected long as a single vol-normalized, structure-aware leg, sized
        and shaped by the setup (no per-trade inverse-ETF hedge — the net-beta
        hedge carries book risk):

          * momentum  → whole-share market BUY + a TRAILING-STOP sell (winners run),
          * reversion → a normal OCO bracket: SL just past the extreme, TP at VWAP.

        Distances are σ-multiples via ``compute_bracket``, which also enforces the
        ``min_rr`` reward:risk gate — a trade that can't structure ≥ min_rr is
        SKIPPED (``act=False``). Crypto can't bracket/trail on Alpaca, so it keeps
        the synthetic signal exit but still gets a vol-normalized σ-stop LEVEL."""
        notional = (decision.get("notional") or 0.0) * plan["risk_scale"]
        if not price or price <= 0 or notional <= 0:
            return decision
        sig = decision.get("signal") or {}
        if asset_class == "equity":
            qty = int(notional // price)
            if qty < 1:
                # Can't afford a whole share — a fractional/notional equity buy
                # CAN'T be bracketed/trailed on Alpaca (need whole-share qty), so
                # it would be an unprotected position with no stop. Skip it rather
                # than open a naked day trade (the TSLA-$217 bug).
                return {**decision, "act": False, "mode": None,
                        "reason": decision["reason"] +
                        f" | skip: ${notional:,.0f} < 1 share (${price:,.2f}) — can't bracket, no naked day buys"}
            br = compute_bracket(sig, price, plan)
            if not br["ok"]:
                # Can't structure the required reward:risk — don't take the trade.
                return {**decision, "act": False, "mode": None,
                        "reason": decision["reason"] + f" | skip: {br['reason']}"}
            primary_notional = qty * price
            leg = {"symbol": decision["symbol"], "side": "buy", "qty": qty,
                   "asset_class": "equity", "role": "primary", "entry": round(price, 2),
                   "exit_type": br["exit_type"], "bracket": br["exit_type"] == "bracket",
                   "sl_price": round(br["sl"], 2), "rr": round(br["rr"], 2),
                   "have_sigma": br["have_sigma"]}
            if br["exit_type"] == "trailing":
                leg["trail_price"] = round(br["trail_dist"], 2)
                why = (decision["reason"] +
                       f", trailing stop ${leg['trail_price']} ({plan['trail_sigma']}σ), "
                       f"SL≈{leg['sl_price']}, R:R proxy {leg['rr']}")
            else:
                leg["tp_price"] = round(br["tp"], 2)
                why = (decision["reason"] +
                       f", bracket SL {leg['sl_price']} / TP {leg['tp_price']}"
                       f"{' (VWAP)' if sig.get('kind') == 'reversion' else ''}, R:R {leg['rr']}")
            max_loss = br["stop_dist"] * qty
        else:  # crypto: synthetic exit, but compute a vol-normalized σ-stop LEVEL
            sigma = sig.get("sigma")
            if sigma and sigma > 0:
                if sig.get("kind") == "reversion":
                    extreme = sig.get("win_low") or price
                    sl_price = round(extreme - plan["reversion_stop_sigma"] * sigma, 2)
                else:
                    sl_price = round(price - plan["stop_sigma"] * sigma, 2)
            else:
                sl_price = round(price * (1 - plan["stop_pct"] / 100.0), 2)
            sl_price = max(0.0, sl_price)
            primary_notional = notional
            leg = {"symbol": decision["symbol"], "side": "buy", "notional": round(notional, 2),
                   "asset_class": "crypto", "role": "primary", "entry": round(price, 2),
                   "exit_type": "none", "bracket": False, "sl_price": sl_price,
                   "have_sigma": bool(sigma and sigma > 0)}
            max_loss = (price - sl_price) * (notional / price) if price else 0.0
            why = decision["reason"] + f", synthetic σ-stop {sl_price} (crypto: no Alpaca bracket)"
        return {**decision, "mode": "hedged", "legs": [leg], "plan_bias": plan["bias"],
                "notional": round(primary_notional, 2),
                "max_loss_est": round(max(0.0, max_loss), 2), "reason": why}

    async def _net_beta_hedge(self, account: dict, positions: list[dict], bars: dict) -> dict | None:
        """Keep the DAY sleeve near market-neutral with ONE inverse-ETF position
        (SH, -1x S&P) sized to the sleeve's net beta. Computes the day book's net
        beta in dollars (hedge instrument excluded), targets a whole-share SH
        position that offsets net-LONG beta, and only trades the delta when it
        drifts beyond the band — so the hedge can never accumulate into a pile."""
        day_hold = sleeve_holdings(self._sqlite, "day")
        pos_price = {norm_symbol(p.get("symbol", "")): _to_float(p.get("current_price"))
                     for p in positions}
        hsym_n = norm_symbol(DAY_HEDGE_SYMBOL)
        net_beta_usd = 0.0
        for n, q in day_hold.items():
            if n == hsym_n:
                continue
            px = pos_price.get(n) or 0.0
            net_beta_usd += q * px * beta_for(n)
        equity = _to_float(account.get("equity")) or 0.0
        band = max(self._s.day_net_beta_band_pct / 100.0 * equity, self._s.day_min_order_notional)
        sh_price = pos_price.get(hsym_n) or _last_close(bars, DAY_HEDGE_SYMBOL) or 0.0
        if sh_price <= 0:
            return None
        cur_sh = day_hold.get(hsym_n, 0.0)
        target_sh = int(max(0.0, net_beta_usd) / sh_price)  # only hedge net-long beta
        delta = target_sh - cur_sh
        if abs(delta * sh_price) < band:
            return None  # within band — no churn
        side = "buy" if delta > 0 else "sell"
        qty = round(abs(delta), 6)
        reason = (f"net-beta hedge: day net β ${net_beta_usd:,.0f} -> target {target_sh} "
                  f"{DAY_HEDGE_SYMBOL} (have {cur_sh:g}) -> {side} {qty}")
        ok = await self._submit_hedge_order(DAY_HEDGE_SYMBOL, side, qty, reason)
        return {"symbol": DAY_HEDGE_SYMBOL, "asset_class": "equity", "act": True, "side": side,
                "qty": qty, "notional": round(qty * sh_price, 2), "mode": "net_beta_hedge",
                "sector": "hedge", "conviction": 0.0, "signal": {"kind": "hedge"},
                "reason": reason, "submitted": ok}

    async def _submit_hedge_order(self, symbol: str, side: str, qty: float, reason: str) -> bool:
        """Plain market order for the net-beta hedge, recorded in the day ledger
        (no bracket — the hedge is managed by rebalancing, not a stop)."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        pid = self._sqlite.execute_returning_id(
            "INSERT INTO bot_proposals (run_id, created_at, symbol, strategist_sym, bucket, "
            "side, order_type, qty, notional, conviction, max_loss_est, rationale, status, "
            "blocks, sleeve) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'day')",
            [now[:10].replace("-", ""), now, symbol, None, "day", side, "market", qty, None,
             None, None, json.dumps({"kind": "net_beta_hedge", "reason": reason}), "proposed",
             json.dumps([])],
        )
        cid = f"day-hedge-{pid}"
        self._sqlite.execute(
            "INSERT OR IGNORE INTO bot_orders (proposal_id, client_order_id, symbol, side, "
            "order_type, qty, status, submitted_at, sleeve) "
            "VALUES (?,?,?,?,?,?, 'submitting', datetime('now'), 'day')",
            [pid, cid, symbol, side, "market", qty],
        )
        try:
            order = await self._broker.submit_order(
                symbol, side, qty=qty, order_type="market", time_in_force="day",
                client_order_id=cid)
        except BrokerError as exc:
            definite = exc.status is not None and 400 <= exc.status < 500
            self._sqlite.execute(
                "UPDATE bot_orders SET status = ?, error = ? WHERE client_order_id = ?",
                ["rejected" if definite else "unknown", exc.reason, cid])
            self._sqlite.execute(
                "UPDATE bot_proposals SET status = 'rejected', blocks = ?, updated_at = datetime('now') "
                "WHERE id = ?", [json.dumps([f"hedge {exc.reason}"]), pid])
            log.warning("net-beta hedge %s %s rejected: %s", side, symbol, exc.reason)
            return False
        self._sqlite.execute(
            "UPDATE bot_orders SET broker_order_id = ?, status = ?, filled_qty = ?, "
            "filled_avg_price = ?, raw = ? WHERE client_order_id = ?",
            [order.get("id"), order.get("status"), _to_float(order.get("filled_qty")),
             _to_float(order.get("filled_avg_price")), json.dumps(order), cid])
        self._sqlite.execute(
            "UPDATE bot_proposals SET status = 'submitted', updated_at = datetime('now') WHERE id = ?",
            [pid])
        return True

    def _journal(self, run_id: str, trade_date: str, d: dict, mc) -> None:
        """Persist EVERY signal the day trader saw this tick — acted, skipped, or
        blocked — with the market context it decided against. The transparency
        record AND the substrate the end-of-day learning loop mines for mistakes."""
        sig = d.get("signal") or {}
        if d.get("mode") == "net_beta_hedge":
            decision = "hedge"
        elif d.get("submitted") or (d.get("selected") and d.get("act")):
            decision = "acted"
        elif d.get("reason", "").find("block") >= 0 or "guardrail" in d.get("reason", ""):
            decision = "blocked"
        else:
            decision = "skipped"
        # Stash σ (and the chosen exit) in the JSON context column — no schema
        # migration — so day_review can replay σ-based exits / grid-search stops.
        legs = d.get("legs") or []
        primary = next((lg for lg in legs if lg.get("role") == "primary"), None)
        ctx = {"market": mc.to_dict(), "conviction": d.get("conviction"),
               "selected": d.get("selected"), "plan_bias": d.get("plan_bias"),
               "sigma": sig.get("sigma"),
               "exit_type": (primary or {}).get("exit_type"),
               "rr": (primary or {}).get("rr")}
        try:
            self._sqlite.execute(
                "INSERT INTO day_signal_journal (ts, run_id, trade_date, symbol, asset_class, "
                "sector, signal_kind, direction, strength, z, last_price, vwap, regime, vol_pctile, "
                "market_bias, decision, side, notional, qty, conviction, reason, context, "
                "proposal_id, outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')",
                [datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id, trade_date,
                 d.get("symbol"), d.get("asset_class"), d.get("sector"), sig.get("kind"),
                 sig.get("direction"), sig.get("strength"), sig.get("z"), sig.get("last"),
                 sig.get("vwap"), mc.regime, mc.vol_pctile, d.get("plan_bias"), decision,
                 d.get("side"), d.get("notional"), d.get("qty"), d.get("conviction"),
                 (d.get("reason") or "")[:500], json.dumps(ctx), d.get("proposal_id")],
            )
        except Exception:
            log.debug("journal write failed for %s", d.get("symbol"), exc_info=True)

    async def _submit(self, decision: dict, asset_class: str) -> bool:
        """Persist a day proposal + auto-submit the order (paper). Returns True
        on a submitted order. Mirrors the swing insert-before-submit pattern."""
        if decision.get("mode") == "hedged":
            return await self._submit_hedged(decision)
        asym = decision["symbol"]
        side = decision["side"]
        notional = decision.get("notional")
        qty = decision.get("qty")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rationale = {"kind": decision["signal"].get("kind"), "signal": decision["signal"],
                     "news": decision.get("news"), "reason": decision["reason"]}
        pid = self._sqlite.execute_returning_id(
            "INSERT INTO bot_proposals (run_id, created_at, symbol, strategist_sym, bucket, "
            "side, order_type, qty, notional, conviction, max_loss_est, rationale, status, "
            "blocks, sleeve) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'day')",
            [now[:10].replace("-", ""), now, asym, decision.get("strategist_sym"), "day",
             side, "market", qty, notional, decision["signal"].get("strength"),
             decision.get("max_loss_est"), json.dumps(rationale), "proposed", json.dumps([])],
        )
        decision["proposal_id"] = pid  # link the journal row to this proposal
        client_order_id = f"day-{pid}"
        is_crypto = asset_class == "crypto"
        tif = "gtc" if is_crypto else "day"
        self._sqlite.execute(
            "INSERT OR IGNORE INTO bot_orders (proposal_id, client_order_id, symbol, side, "
            "order_type, qty, notional, status, submitted_at, sleeve) "
            "VALUES (?,?,?,?,?,?,?, 'submitting', datetime('now'), 'day')",
            [pid, client_order_id, asym, side, "market", qty, notional],
        )
        try:
            order = await self._broker.submit_order(
                asym, side, qty=qty if side == "sell" else None,
                notional=notional if side == "buy" else None,
                order_type="market", time_in_force=tif, client_order_id=client_order_id,
            )
        except BrokerError as exc:
            definite = exc.status is not None and 400 <= exc.status < 500
            self._sqlite.execute(
                "UPDATE bot_orders SET status = ?, error = ? WHERE client_order_id = ?",
                ["rejected" if definite else "unknown", exc.reason, client_order_id],
            )
            self._sqlite.execute(
                "UPDATE bot_proposals SET status = ?, blocks = ?, updated_at = datetime('now') WHERE id = ?",
                ["rejected" if definite else "submitted",
                 json.dumps([f"broker {'rejected' if definite else 'submit ambiguous'}: {exc.reason}"]), pid],
            )
            log.warning("day order %s %s: %s", side, asym, exc.reason)
            await self._broadcast("day")
            return False
        self._sqlite.execute(
            "UPDATE bot_orders SET broker_order_id = ?, status = ?, filled_qty = ?, "
            "filled_avg_price = ?, raw = ? WHERE client_order_id = ?",
            [order.get("id"), order.get("status"), _to_float(order.get("filled_qty")),
             _to_float(order.get("filled_avg_price")), json.dumps(order), client_order_id],
        )
        self._sqlite.execute(
            "UPDATE bot_proposals SET status = 'submitted', updated_at = datetime('now') WHERE id = ?",
            [pid],
        )
        log.info("day submitted %s %s (proposal %s)", side, asym, pid)
        await self._broadcast("day")
        return True

    # ---- hedged-bracket execution ------------------------------------------
    def _build_hedged(self, decision: dict, plan: dict, price, hedge_price: dict,
                      asset_class: str) -> dict:
        """Turn a plain LONG into a hedged trade: the signal leg + a BUY of a
        beta-sized inverse ETF, each equity leg carrying a bracket (stop-loss +
        take-profit). Crypto's long leg can't be bracketed on Alpaca (synthetic
        stop via the signal/news exit) but its BITI hedge — an equity — is. Falls
        back to the plain decision when it can't size whole shares or a hedge."""
        sym = decision["strategist_sym"]
        notional = (decision.get("notional") or 0.0) * plan["risk_scale"]
        if not price or price <= 0 or notional <= 0:
            return decision
        is_equity = asset_class == "equity"
        stop_pct, tp_pct = plan["stop_pct"], plan["tp_pct"]
        legs: list[dict] = []
        if is_equity:
            qty = int(notional // price)
            if qty < 1:
                return decision  # too small for a whole-share bracket
            legs.append({"symbol": decision["symbol"], "side": "buy", "qty": qty,
                         "asset_class": "equity", "bracket": True, "role": "primary",
                         "entry": round(price, 2),
                         "tp_price": round(price * (1 + tp_pct / 100), 2),
                         "sl_price": round(price * (1 - stop_pct / 100), 2)})
            primary_notional = qty * price
        else:  # crypto: plain notional buy (no bracket possible)
            legs.append({"symbol": decision["symbol"], "side": "buy",
                         "notional": round(notional, 2), "asset_class": "crypto",
                         "bracket": False, "role": "primary", "entry": round(price, 2)})
            primary_notional = notional

        hedge = None
        h = hedge_for(sym)
        if h:
            hsym, beta, lev = h
            hprice = hedge_price.get(hsym)
            if hprice and hprice > 0:
                hnotional = primary_notional * plan["hedge_ratio"] * beta / lev
                hqty = int(hnotional // hprice)
                if hqty >= 1 and hqty * hprice >= self._s.day_min_hedge_notional:
                    hedge = {"symbol": hsym, "side": "buy", "qty": hqty,
                             "asset_class": "equity", "bracket": True, "role": "hedge",
                             "beta": beta, "lev": lev, "entry": round(hprice, 2),
                             "tp_price": round(hprice * (1 + tp_pct / 100), 2),
                             "sl_price": round(hprice * (1 - stop_pct / 100), 2)}
                    legs.append(hedge)
        if hedge is None and plan["require_hedge"] and sym not in UNHEDGEABLE_INDEX:
            reason = "market closed" if not hedge_price else "no sizable hedge"
            return {**decision, "act": False,
                    "reason": f"{plan['bias']} tape requires a hedge for {sym} but {reason} — skip"}

        max_loss = primary_notional * stop_pct / 100.0
        if hedge:
            max_loss += hedge["qty"] * hedge["entry"] * stop_pct / 100.0
        why = decision["reason"]
        if hedge:
            why += (f" | hedge BUY {hedge['qty']}×{hedge['symbol']} "
                    f"(β{hedge['beta']}/{int(hedge['lev'])}x inv)")
        why += (f", bracket -{stop_pct:.2f}/+{tp_pct:.2f}%" if is_equity
                else f", synthetic stop {stop_pct:.2f}%")
        return {**decision, "mode": "hedged", "legs": legs, "plan_bias": plan["bias"],
                "notional": round(primary_notional, 2),
                "max_loss_est": round(max_loss, 2), "reason": why}

    async def _submit_hedged(self, decision: dict) -> bool:
        """Persist one hedged proposal + submit each leg (bracket where the asset
        allows). Returns True if the PRIMARY leg submitted."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        legs = decision.get("legs") or []
        rationale = {"kind": decision["signal"].get("kind"), "signal": decision["signal"],
                     "plan_bias": decision.get("plan_bias"), "legs": legs,
                     "news": decision.get("news"), "reason": decision["reason"]}
        pid = self._sqlite.execute_returning_id(
            "INSERT INTO bot_proposals (run_id, created_at, symbol, strategist_sym, bucket, "
            "side, order_type, qty, notional, conviction, max_loss_est, rationale, status, "
            "blocks, sleeve) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'day')",
            [now[:10].replace("-", ""), now, decision["symbol"], decision.get("strategist_sym"),
             "day", "buy", "bracket", None, decision.get("notional"),
             decision["signal"].get("strength"), decision.get("max_loss_est"),
             json.dumps(rationale), "proposed", json.dumps([])],
        )
        decision["proposal_id"] = pid  # link the journal row to this proposal
        ok_primary = False
        for i, leg in enumerate(legs):
            cid = f"day-{pid}-{leg['role'][0]}{i}"
            is_crypto = leg.get("asset_class") == "crypto"
            exit_type = leg.get("exit_type") or ("bracket" if leg.get("bracket") else "none")
            is_bracket = exit_type == "bracket"
            is_trailing = exit_type == "trailing"
            # Momentum trailing legs ENTER with a plain market buy; the trailing
            # stop is a SEPARATE order submitted after the entry (Alpaca can't
            # attach a trailing stop as a bracket leg). Brackets enter via
            # order_class so Alpaca attaches the OCO take-profit + stop-loss.
            otype = "bracket" if is_bracket else "market"
            self._sqlite.execute(
                "INSERT OR IGNORE INTO bot_orders (proposal_id, client_order_id, symbol, side, "
                "order_type, qty, notional, status, submitted_at, sleeve) "
                "VALUES (?,?,?,?,?,?,?, 'submitting', datetime('now'), 'day')",
                [pid, cid, leg["symbol"], leg["side"], otype, leg.get("qty"), leg.get("notional")],
            )
            kwargs: dict = {"order_type": "market",
                            "time_in_force": "gtc" if is_crypto else "day",
                            "client_order_id": cid}
            if leg.get("qty") is not None:
                kwargs["qty"] = leg["qty"]
            else:
                kwargs["notional"] = leg["notional"]
            if is_bracket:
                kwargs["order_class"] = "bracket"
                kwargs["take_profit_price"] = leg["tp_price"]
                kwargs["stop_loss_price"] = leg["sl_price"]
            try:
                order = await self._broker.submit_order(leg["symbol"], leg["side"], **kwargs)
            except BrokerError as exc:
                definite = exc.status is not None and 400 <= exc.status < 500
                self._sqlite.execute(
                    "UPDATE bot_orders SET status = ?, error = ? WHERE client_order_id = ?",
                    ["rejected" if definite else "unknown", exc.reason, cid],
                )
                log.warning("day hedged leg %s %s rejected: %s", leg["side"], leg["symbol"], exc.reason)
                continue
            self._sqlite.execute(
                "UPDATE bot_orders SET broker_order_id = ?, status = ?, filled_qty = ?, "
                "filled_avg_price = ?, raw = ? WHERE client_order_id = ?",
                [order.get("id"), order.get("status"), _to_float(order.get("filled_qty")),
                 _to_float(order.get("filled_avg_price")), json.dumps(order), cid],
            )
            if is_bracket:
                self._record_child_legs(order)
            if is_trailing and leg["side"] == "buy" and leg.get("trail_price"):
                # Entry filled (or accepted) — attach the trailing-stop exit. Like
                # bracket child legs it carries NO proposal link, so reconcile()
                # tracks its fill and sleeve_holdings nets the exit (no phantom).
                await self._submit_trailing_exit(
                    cid, leg["symbol"], leg["qty"], leg["trail_price"], is_crypto)
            if leg["role"] == "primary":
                ok_primary = True
        self._sqlite.execute(
            "UPDATE bot_proposals SET status = ?, updated_at = datetime('now') WHERE id = ?",
            ["submitted" if ok_primary else "rejected", pid],
        )
        log.info("day hedged %s submitted (proposal %s, %d legs)",
                 decision["symbol"], pid, len(legs))
        await self._broadcast("day")
        return ok_primary

    def _record_child_legs(self, order: dict) -> None:
        """A bracket order spawns OCO child legs (take-profit + stop-loss) with
        their OWN broker ids that reconcile() would otherwise never see — so when
        a child fills, the broker position drops but the sleeve ledger never
        decrements (the bug that left phantom NFLX/SOXS/NVDA 'holdings'). Record
        each child as a day order keyed by its client_order_id, with NO proposal
        link (it only adjusts holdings, not proposal status), so reconcile()
        tracks its fill and sleeve_holdings nets the exit."""
        for leg in (order.get("legs") or []):
            cid = leg.get("client_order_id")
            if not cid:
                continue
            self._sqlite.execute(
                "INSERT OR IGNORE INTO bot_orders (proposal_id, client_order_id, symbol, "
                "side, order_type, qty, notional, status, submitted_at, broker_order_id, "
                "filled_qty, filled_avg_price, raw, sleeve) "
                "VALUES (NULL,?,?,?,?,?,?,?, datetime('now'), ?,?,?,?, 'day')",
                [cid, leg.get("symbol"), leg.get("side"), leg.get("type") or "limit",
                 _to_float(leg.get("qty")), None, leg.get("status") or "new",
                 leg.get("id"), _to_float(leg.get("filled_qty")),
                 _to_float(leg.get("filled_avg_price")), json.dumps(leg)],
            )

    async def _submit_trailing_exit(self, parent_cid: str, symbol: str, qty: float,
                                    trail_price: float, is_crypto: bool) -> bool:
        """Attach a TRAILING-STOP sell to a momentum entry (equities only) — the
        winners-run exit. Recorded as an unlinked day order (proposal_id NULL) so
        reconcile() tracks its fill and sleeve_holdings nets the position out when
        it triggers (mirrors how bracket OCO child legs are tracked)."""
        cid = f"{parent_cid}-trail"
        self._sqlite.execute(
            "INSERT OR IGNORE INTO bot_orders (proposal_id, client_order_id, symbol, side, "
            "order_type, qty, status, submitted_at, sleeve) "
            "VALUES (NULL,?,?,?,?,?, 'submitting', datetime('now'), 'day')",
            [cid, symbol, "sell", "trailing_stop", qty],
        )
        try:
            order = await self._broker.submit_order(
                symbol, "sell", qty=qty, order_type="trailing_stop",
                trail_price=trail_price, time_in_force="gtc" if is_crypto else "day",
                client_order_id=cid)
        except BrokerError as exc:
            definite = exc.status is not None and 400 <= exc.status < 500
            self._sqlite.execute(
                "UPDATE bot_orders SET status = ?, error = ? WHERE client_order_id = ?",
                ["rejected" if definite else "unknown", exc.reason, cid])
            log.warning("day trailing exit %s rejected: %s", symbol, exc.reason)
            return False
        self._sqlite.execute(
            "UPDATE bot_orders SET broker_order_id = ?, status = ?, filled_qty = ?, "
            "filled_avg_price = ?, raw = ? WHERE client_order_id = ?",
            [order.get("id"), order.get("status"), _to_float(order.get("filled_qty")),
             _to_float(order.get("filled_avg_price")), json.dumps(order), cid])
        return True

    # ---- reconcile / status ------------------------------------------------
    async def reconcile(self) -> dict:
        if not self._broker.enabled:
            return {"ok": False}
        orders = await self._broker.list_orders("all", 200)
        by_cid = {o.get("client_order_id"): o for o in orders if o.get("client_order_id")}
        rows = self._sqlite.fetchall(
            "SELECT id, proposal_id, client_order_id FROM bot_orders WHERE sleeve = 'day'"
        )
        for r in rows:
            o = by_cid.get(r["client_order_id"])
            if not o:
                continue
            status = o.get("status")
            self._sqlite.execute(
                "UPDATE bot_orders SET broker_order_id = ?, status = ?, filled_qty = ?, "
                "filled_avg_price = ?, reconciled_at = datetime('now'), raw = ? WHERE id = ?",
                [o.get("id"), status, _to_float(o.get("filled_qty")),
                 _to_float(o.get("filled_avg_price")), json.dumps(o), r["id"]],
            )
            if r["proposal_id"] is not None:
                filled = _to_float(o.get("filled_qty")) or 0.0
                pstatus = ("filled" if (status == "filled" or
                           (status in ("canceled", "expired") and filled > 0))
                           else {"rejected": "rejected", "canceled": "canceled",
                                 "expired": "canceled"}.get(status, "submitted"))
                self._sqlite.execute(
                    "UPDATE bot_proposals SET status = ? WHERE id = ? AND "
                    "status IN ('submitted','rejected','canceled','filled')",
                    [pstatus, r["proposal_id"]],
                )
        return {"ok": True}

    async def status(self) -> dict:
        cfg = self._config()
        out = {
            "config": cfg,
            "universe": self._s.day_universe,
            "optimizer": self._optimizer.latest(),
            "guardrails": {
                "max_position_pct": self._s.day_max_position_pct,
                "min_order_notional": self._s.day_min_order_notional,
                "daily_loss_limit_pct": self._s.day_daily_loss_limit_pct,
            },
            "recent_orders": self._recent_orders(),
            "recent_proposals": self._recent_proposals(),
            "disclaimer": DISCLAIMER,
        }
        try:
            out["intraday_plan"] = await self.intraday_plan()
        except Exception:
            out["intraday_plan"] = None
        # Market context (no broker call) — "what the day trader sees", always shown.
        try:
            strategist = None
            try:
                from app.edge.strategist import latest_snapshot
                strategist = latest_snapshot(self._duck)
            except Exception:
                strategist = None
            mc = build_market_context(self._duck, vol_signal=await self._vol_signal(),
                                      strategist=strategist)
            out["market_context"] = mc.to_dict()
        except Exception:
            out["market_context"] = None
        if self._broker.enabled:
            try:
                if self._has_inflight():
                    await self.reconcile()
                out["market_open"] = await self._broker.is_market_open()
                holds = sleeve_holdings(self._sqlite, "day")
                out["holdings"] = holds
                # Portfolio state (equity, net beta incl. hedges, sectors, budget).
                account = await self._broker.account()
                positions = await self._broker.positions()
                day_budget = (_to_float(account.get("equity")) or 0.0) * \
                    float(self._optimizer.latest().get("day_pct") or 0.0) / 100.0
                out["portfolio_state"] = build_portfolio_state(
                    account, positions, self._sqlite, day_budget=day_budget).to_dict()
            except BrokerError as exc:
                out["account_error"] = exc.reason
        return out

    def _has_inflight(self) -> bool:
        row = self._sqlite.fetchone(
            "SELECT 1 FROM bot_orders WHERE sleeve = 'day' AND status IN "
            "('submitting','new','accepted','pending_new','partially_filled') LIMIT 1"
        )
        return row is not None

    def _recent_orders(self) -> list[dict]:
        rows = self._sqlite.fetchall(
            "SELECT id, symbol, side, qty, notional, status, filled_qty, filled_avg_price, "
            "submitted_at, error FROM bot_orders WHERE sleeve = 'day' ORDER BY id DESC LIMIT 30"
        )
        return [dict(r) for r in rows]

    def _recent_proposals(self) -> list[dict]:
        rows = self._sqlite.fetchall(
            "SELECT id, symbol, side, qty, notional, conviction, max_loss_est, rationale, "
            "status, created_at FROM bot_proposals WHERE sleeve = 'day' ORDER BY id DESC LIMIT 20"
        )
        out = []
        for r in rows:
            d = dict(r)
            if d.get("rationale"):
                try:
                    d["rationale"] = json.loads(d["rationale"])
                except (ValueError, TypeError):
                    pass
            out.append(d)
        return out

    def _latest_regime(self) -> str | None:
        row = self._duck.fetchone("SELECT regime FROM macro_composite ORDER BY ts DESC LIMIT 1")
        return row[0] if row else None

    async def _vol_signal(self) -> dict | None:
        """The vol-overlay forecast, cached 15min (it barely moves intraday and
        loads SPY history — don't recompute it every 1-min tick)."""
        import time as _t
        cache = getattr(self, "_vol_cache", None)
        if cache and (_t.monotonic() - cache[1]) < 900:
            return cache[0]
        sig = None
        try:
            from app.ml.vol_overlay import current_signal as _vol_sig
            loop = asyncio.get_running_loop()
            sig = await loop.run_in_executor(None, lambda: _vol_sig(duck=self._duck))
        except Exception:
            sig = None
        self._vol_cache = (sig, _t.monotonic())
        return sig

    async def intraday_plan(self) -> dict:
        """The deterministic intraday plan (regime + vol-overlay → risk envelope +
        hedge requirement). No LLM, no broker call — safe to poll for the UI."""
        vol_signal = await self._vol_signal()
        plan = build_plan(self._latest_regime(), vol_signal, base_stop_pct=self._s.day_stop_pct,
                          base_tp_pct=self._s.day_tp_pct, base_hedge_ratio=self._s.day_hedge_ratio,
                          stop_sigma=self._s.day_stop_sigma, tp_sigma=self._s.day_tp_sigma,
                          trail_sigma=self._s.day_trail_sigma,
                          reversion_stop_sigma=self._s.day_reversion_stop_sigma,
                          min_rr=self._s.day_min_rr)
        plan["hedged_enabled"] = self._s.day_hedged_enabled
        plan["vol_signal"] = vol_signal
        return plan

    async def _broadcast(self, event: str) -> None:
        try:
            await self._hub.broadcast(BOT_TOPIC, {"type": "bot", "event": event})
        except Exception:
            log.debug("day broadcast failed", exc_info=True)
