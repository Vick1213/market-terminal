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
from datetime import datetime, timedelta, timezone

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
from app.trading.intraday import (
    PAIRS, UNHEDGEABLE_INDEX, build_plan, compute_bracket, compute_bracket_short, hedge_for,
)
from app.trading.optimizer import PortfolioOptimizer
from app.trading.pairs import best_pair_trade
from app.trading.signals import (
    evaluate_setups, intraday_signal, major_news, opening_range, portfolio_conflict,
)
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
        # Opening-range cache for ORB: {symbol: (high, low)}, rebuilt each session.
        self._orb_cache: dict[str, tuple[float, float] | None] = {}
        self._orb_date: str | None = None
        # Live tunable-knob values (override ?? env default), refreshed once per
        # run/plan so an accepted learning-loop suggestion takes effect next tick.
        self._knobs: dict[str, object] = {}
        # Whether conviction-gated leverage may deploy (set each run()); dormant
        # until the env master + panel toggle + validated-edge gate all pass.
        self._lev_armed: bool = False
        self._lev_reason: str = "leverage off"

    def _refresh_knobs(self) -> None:
        from app.trading.knobs import effective_params
        try:
            self._knobs = effective_params(self._sqlite, self._s)
        except Exception:
            self._knobs = {}

    def _knob(self, name: str):
        """A tunable parameter's LIVE value — an accepted runtime override if one
        exists, else the env default. Falls back to settings for anything not in
        the knob cache (non-tunable params)."""
        v = self._knobs.get(name)
        return v if v is not None else getattr(self._s, name)

    def _orb_for(self, sym: str) -> tuple[float, float] | None:
        """Cached opening-range (high, low) for ORB — populated by ``_ensure_orb``."""
        return self._orb_cache.get(norm_symbol(sym))

    async def _ensure_orb(self, equities: list[tuple[str, str]], trade_date: str) -> None:
        """Populate the opening-range cache ONCE per session, from a dedicated
        from-the-open fetch — so ORB works even when the bot starts mid-day (not
        only when it ran from the open). Pulls the full session for the equity
        universe, then takes each name's first ``day_orb_minutes`` of bars by
        timestamp. Best-effort: any fetch error just leaves ORB dormant today."""
        if self._orb_date == trade_date and self._orb_cache:
            return
        self._orb_cache = {}
        self._orb_date = trade_date
        syms = [(s, "equity") for s, a in equities if a == "equity"]
        if not syms:
            return
        try:
            bars = await fetch_intraday_bars(
                self._http, self._data_key, self._data_secret, syms, minutes=390)
        except Exception:
            log.debug("ORB fetch failed", exc_info=True)
            return

        def _p(ts):
            try:
                return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except Exception:
                return None

        for s, _a in syms:
            sb = bars.get(s) or bars.get(alpaca_symbol(s, "equity")[0] if alpaca_symbol(s, "equity") else s) or []
            n = norm_symbol(s)
            if not sb:
                self._orb_cache[n] = None
                continue
            t0 = _p(sb[0].get("ts"))
            if t0 is None:
                self._orb_cache[n] = opening_range(sb[: self._s.day_orb_minutes])
                continue
            cutoff = t0 + timedelta(minutes=self._s.day_orb_minutes)
            orb_bars = [b for b in sb if (_p(b.get("ts")) or t0) <= cutoff]
            self._orb_cache[n] = opening_range(orb_bars)

    # ---- config / kill switch ---------------------------------------------
    def _config(self) -> dict:
        row = self._sqlite.fetchone(
            "SELECT day_enabled, day_hedge_enabled, day_soft_stop, day_short_enabled, "
            "day_pairs_enabled, day_leverage FROM bot_config WHERE id = 1"
        )
        return {
            "enabled": bool(row["day_enabled"]) if row else False,
            # Net-beta SH hedge toggle (defaults OFF). The env flag is a hard
            # master switch on top of this runtime, panel-driven one.
            "hedge_enabled": bool(row["day_hedge_enabled"]) if row else False,
            # Soft stop: pause NEW entries but keep managing open positions.
            "soft_stop": bool(row["day_soft_stop"]) if row else False,
            # Shorts: panel toggle AND the env master must both be on to open shorts.
            "short_enabled": bool(row["day_short_enabled"]) if row else False,
            # Pairs (market-neutral) mode — needs shorts armed too.
            "pairs_enabled": bool(row["day_pairs_enabled"]) if row else False,
            # Conviction-gated leverage: panel toggle. Env master + validated-edge
            # gate still apply on top (see leverage.leverage_armed).
            "leverage": bool(row["day_leverage"]) if row and "day_leverage" in row.keys() else False,
        }

    async def set_enabled(self, enabled: bool) -> dict:
        self._sqlite.execute(
            "UPDATE bot_config SET day_enabled = ?, updated_at = datetime('now') WHERE id = 1",
            [1 if enabled else 0],
        )
        log.info("day bot kill switch -> %s", "ENABLED" if enabled else "DISABLED")
        await self._broadcast("day_config")
        return self._config()

    async def set_short_enabled(self, enabled: bool) -> dict:
        """Arm/disarm SHORT entries (equities only, mandatory upside stop). The env
        ``day_short_enabled`` master must also be on for shorts to actually open."""
        self._sqlite.execute(
            "UPDATE bot_config SET day_short_enabled = ?, updated_at = datetime('now') WHERE id = 1",
            [1 if enabled else 0],
        )
        log.info("day bot SHORTS -> %s", "ENABLED" if enabled else "DISABLED")
        await self._broadcast("day_config")
        return self._config()

    async def set_pairs_enabled(self, enabled: bool) -> dict:
        """Arm/disarm relative-strength PAIRS mode (needs shorts armed too)."""
        self._sqlite.execute(
            "UPDATE bot_config SET day_pairs_enabled = ?, updated_at = datetime('now') WHERE id = 1",
            [1 if enabled else 0],
        )
        log.info("day bot PAIRS -> %s", "ENABLED" if enabled else "DISABLED")
        await self._broadcast("day_config")
        return self._config()

    async def set_soft_stop(self, enabled: bool) -> dict:
        """Soft stop: pause opening NEW day entries while still managing the ones
        already on (exits, fade-sells, reconcile, and the broker's own stop/target
        brackets all keep running). A pause, distinct from the hard kill switch."""
        self._sqlite.execute(
            "UPDATE bot_config SET day_soft_stop = ?, updated_at = datetime('now') WHERE id = 1",
            [1 if enabled else 0],
        )
        log.info("day bot SOFT STOP -> %s", "ON (no new entries)" if enabled else "OFF")
        await self._broadcast("day_config")
        return self._config()

    async def set_leverage_enabled(self, enabled: bool) -> dict:
        """Arm/disarm conviction-gated leverage (panel toggle). The env master
        ``day_leverage_enabled`` AND the validated-edge gate must ALSO pass before
        any position actually exceeds 1x — this toggle alone never levers blind."""
        self._sqlite.execute(
            "UPDATE bot_config SET day_leverage = ?, updated_at = datetime('now') WHERE id = 1",
            [1 if enabled else 0],
        )
        log.info("day CONVICTION LEVERAGE -> %s", "ENABLED" if enabled else "DISABLED")
        await self._broadcast("day_config")
        return self._config()

    async def set_hedge_enabled(self, enabled: bool) -> dict:
        """Arm/disarm the net-beta SH hedge. OFF leaves any existing SH position
        untouched but stops the rebalancer from opening or trimming it."""
        self._sqlite.execute(
            "UPDATE bot_config SET day_hedge_enabled = ?, updated_at = datetime('now') WHERE id = 1",
            [1 if enabled else 0],
        )
        log.info("day net-beta SH hedge -> %s", "ENABLED" if enabled else "DISABLED")
        await self._broadcast("day_config")
        return self._config()

    def _leverage_status(self, cfg: dict) -> dict:
        """Leverage readout for the UI: whether it's armed right now and why/why
        not (env master, panel toggle, and the validated-edge gate)."""
        from app.trading.leverage import leverage_armed, cost_adjusted_expectancy
        armed, reason = leverage_armed(self._sqlite, self._s, cfg)
        exp, n = cost_adjusted_expectancy(self._sqlite, self._s)
        return {
            "armed": armed,
            "reason": reason,
            "env_enabled": bool(getattr(self._s, "day_leverage_enabled", False)),
            "panel_enabled": bool(cfg.get("leverage")),
            "require_validated": bool(getattr(self._s, "day_leverage_require_validated", True)),
            "max_leverage": float(getattr(self._s, "day_max_leverage", 2.0)),
            "max_gross_leverage": float(getattr(self._s, "day_max_gross_leverage", 2.0)),
            "cost_adj_expectancy": exp,
            "validation_trades": n,
            "validation_trades_needed": int(getattr(self._s, "day_leverage_min_validation_trades", 100)),
        }

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
        self._refresh_knobs()  # pick up any accepted learning-loop overrides
        # Whether conviction-gated leverage may deploy this tick (env master +
        # panel toggle + validated-edge gate). Stays False — so every position is
        # 1x — until the edge is proven; see leverage.leverage_armed.
        from app.trading.leverage import leverage_armed
        self._lev_armed, self._lev_reason = leverage_armed(self._sqlite, self._s, cfg)
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
        # The net-beta hedge instrument (VOO, shorted) is NOT in the universe, so
        # its price was never fetched — pull its bars too so the hedge can size.
        hedge_sym = self._s.day_hedge_symbol
        if (market_open and cfg["hedge_enabled"] and self._s.day_net_beta_enabled
                and hedge_sym not in active_syms and hedge_sym not in hedge_syms):
            fetch_list.append((hedge_sym, "equity"))

        try:
            bars = await fetch_intraday_bars(
                self._http, self._data_key, self._data_secret, fetch_list,
                minutes=self._s.day_intraday_lookback_min,
            )
            account = await self._broker.account()
            # FRESH (uncached) positions: sell-sizing caps at the broker's current
            # long qty, so a sell can't be sized off a stale snapshot that still
            # shows shares a prior tick already sold — which oversold AAPL into an
            # unintended −2 short. The day sleeve is long-only; a "sell" only closes.
            positions = await self._broker.positions(fresh=True)
        except BrokerError as exc:
            return {"ok": False, "detail": exc.reason, "config": cfg}

        # Deterministic intraday plan from regime + the vol-overlay forecast —
        # sets stop/take-profit %, size scale, and whether a hedge is required.
        # NO LLM in the fast loop (the user's ask).
        regime = self._latest_regime()
        vol_signal = await self._vol_signal()
        plan = build_plan(regime, vol_signal, base_stop_pct=self._knob("day_stop_pct"),
                          base_tp_pct=self._s.day_tp_pct, base_hedge_ratio=self._s.day_hedge_ratio,
                          stop_sigma=self._knob("day_stop_sigma"), tp_sigma=self._knob("day_tp_sigma"),
                          trail_sigma=self._s.day_trail_sigma,
                          reversion_stop_sigma=self._s.day_reversion_stop_sigma,
                          min_rr=self._s.day_min_rr,
                          stop_floor_pct=self._knob("day_stop_floor_pct"))
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

        # Shorts open only when BOTH the env master and the panel toggle are on
        # (equities only — Alpaca crypto is long-only). The evaluator still emits a
        # bearish bias when shorts are off, but it's used only to EXIT longs.
        shorts_on = bool(self._s.day_short_enabled and cfg["short_enabled"] and market_open)
        strats = tuple(self._s.day_strategies)
        if "orb" in strats and market_open:
            await self._ensure_orb(equities, trade_date)

        # --- 1) evaluate EVERY candidate (no orders yet) ------------------------
        candidates: list[dict] = []
        for sym, asset_class in active:
            mapped = alpaca_symbol(sym, asset_class)
            if mapped is None:
                continue
            asym = mapped[0]
            n = norm_symbol(asym)
            sym_bars = bars.get(asym) or bars.get(sym) or []
            orb_levels = (self._orb_for(asym)
                          if (asset_class == "equity" and "orb" in strats) else None)
            sig = evaluate_setups(
                sym_bars,
                breakout_buffer_pct=self._s.day_breakout_buffer_pct,
                momentum_min_pct=self._knob("day_momentum_min_pct"),
                reversion_z=self._knob("day_reversion_z"),
                vwap_trend_min_pct=self._s.day_vwap_trend_min_pct,
                orb_levels=orb_levels,
                enabled=strats,
                # A bearish setup on a name we don't hold is only an OPEN-SHORT when
                # shorts are armed AND it's an equity; otherwise leave it as bias
                # (used to exit an existing long) by allowing the short direction
                # in the signal — _decide decides exit vs open.
                allow_short=True,
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
                news=news, shorts_on=shorts_on,
            )
            decision["price"] = price
            decision["sector"] = sector_for(asym)
            decision["conviction"] = self._conviction(decision, sig, market_ctx)
            candidates.append(decision)

        # --- 2) segmentation: rank longs+shorts by conviction, fill within the
        #        day budget ∩ AVAILABLE FUNDS (buying power), per-tick/sector caps -
        self._segment(candidates, portfolio, account, plan)

        # --- 3) execute: exits/sells always go; selected longs go BRACKETED
        #        (no per-trade hedge — the net-beta hedge below carries the risk) -
        actions: list[dict] = []
        for d in candidates:
            out = d
            if d.get("act") and d.get("exit"):
                # Exits (sell-to-close a long OR buy-to-cover a short) ALWAYS run —
                # soft stop manages open positions, it never traps them.
                if cfg["enabled"]:
                    d["submitted"] = await self._submit(d, d["asset_class"])
            elif d.get("selected") and cfg["soft_stop"]:
                # Soft stop: a qualifying NEW entry (long or short) is held back.
                out = {**d, "act": False, "mode": None,
                       "reason": d.get("reason", "") + " | soft-stop: new entries paused"}
            elif d.get("selected") and d.get("side") == "short":
                # Borrow check: never short a name we can't confirm is borrowable.
                shortable, etb = await self._broker.shortability(d["symbol"])
                if not shortable or (self._s.day_short_easy_to_borrow_only and not etb):
                    out = {**d, "act": False, "mode": None,
                           "reason": d.get("reason", "") +
                           f" | short skip: {d['symbol']} not "
                           f"{'easy-to-borrow' if shortable else 'shortable'}"}
                else:
                    out = self._build_short(d, plan, d.get("price"), d["asset_class"])
                    if out.get("act") and cfg["enabled"]:
                        out["submitted"] = await self._submit(out, d["asset_class"])
                        if out.get("submitted"):
                            deployed += out.get("notional") or 0.0
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

        # --- 3b) PAIRS: one relative-strength market-neutral pair (armed = off) --
        if market_open:
            pair = await self._maybe_pair_trade(bars, plan, cfg, portfolio, account, deployed,
                                                market_ctx, run_id, trade_date, day_hold)
            if pair:
                actions.append(pair)

        # --- 4) net-beta hedge ONCE (single SH position to a target, not a pile) -
        #     Gated by the panel toggle (cfg["hedge_enabled"], default OFF) on top
        #     of the env master switch — SH is a weak unlevered hedge, off by default.
        if (cfg["enabled"] and cfg["hedge_enabled"] and not cfg["soft_stop"]
                and self._s.day_net_beta_enabled and market_open):
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
                day_budget, deployed, swing_val, equity, halted, news, shorts_on=False) -> dict:
        """One symbol → hold / open-long(buy) / open-short(short) / exit(sell to
        close a long, buy to cover a short). ``sig['direction']`` is the BIAS
        (buy=bullish, short=bearish); this maps it to the right action given what
        the day sleeve already holds. Exits carry ``exit=True`` so they ALWAYS run
        (even under soft-stop). A long-only sleeve never reaches the open-short arm
        because ``shorts_on`` is False."""
        base = {"symbol": asym, "strategist_sym": sym, "asset_class": asset_class,
                "signal": sig, "news": news, "act": False, "side": None,
                "notional": None, "qty": None, "reason": ""}
        direction = sig.get("direction")
        strength = float(sig.get("strength") or 0.0)
        held_long = max(0.0, held_qty)
        held_short = max(0.0, -held_qty)
        bq_long = max(0.0, broker_qty) if broker_qty is not None else None
        bq_short = max(0.0, -broker_qty) if broker_qty is not None else None
        sell_qty = _sellable_qty(held_long, bq_long)    # to close a long
        cover_qty = _sellable_qty(held_short, bq_short)  # to close a short

        def _size() -> float:
            target = day_budget * (self._s.day_max_position_pct / 100.0) * min(1.0, strength / 3.0)
            return min(target, max(0.0, day_budget - deployed))

        # News overrides: adverse news exits a long; favorable news covers a short.
        if news and news["score"] < 0 and held_long > 0:
            if sell_qty <= 0:
                return {**base, "reason": "adverse news but no sellable day balance"}
            return {**base, "act": True, "side": "sell", "qty": sell_qty, "exit": True,
                    "reason": f"major adverse news ({news['score']:+.2f}) — exit long"}
        if news and news["score"] > 0 and held_short > 0:
            if cover_qty <= 0:
                return {**base, "reason": "favorable news but no short to cover"}
            return {**base, "act": True, "side": "buy", "qty": cover_qty, "exit": True,
                    "reason": f"favorable news ({news['score']:+.2f}) — cover short"}

        if direction == "none" or strength < self._knob("day_min_signal"):
            return {**base, "reason": sig.get("detail", "no actionable setup")}

        # ---- bearish bias -------------------------------------------------
        if direction == "short":
            if held_long > 0:  # fade/breakdown EXITS the long (was the old 'sell')
                if sell_qty <= 0:
                    return {**base, "reason": "bearish signal but no long to trim"}
                return {**base, "act": True, "side": "sell", "qty": sell_qty, "exit": True,
                        "reason": f"{sig['detail']} — close long"}
            if held_short > 0:
                return {**base, "reason": "already short this name — no pyramiding"}
            if not shorts_on:
                return {**base, "reason": f"{sig['detail']} — shorts off (skip)"}
            if asset_class != "equity":
                return {**base, "reason": "shorts are equities-only (Alpaca crypto is long-only)"}
            if news and news["score"] > 0:
                return {**base, "reason": f"short vetoed by favorable news ({news['score']:+.2f})"}
            if halted:
                return {**base, "reason": halted}
            if day_budget <= 0:
                return {**base, "reason": "day budget is zero under current conditions"}
            notional = _size()
            if notional < self._s.day_min_order_notional:
                return {**base, "reason": (f"short sized ${notional:,.0f} below "
                                           f"${self._s.day_min_order_notional:,.0f} min (or budget full)")}
            stop = _DAY_STOP_PCT.get(asset_class, 3.0)
            return {**base, "act": True, "side": "short", "notional": round(notional, 2),
                    "max_loss_est": round(notional * stop / 100.0, 2),
                    "reason": f"{sig['kind']} short ({sig['detail']})"}

        # ---- bullish bias (direction == buy) ------------------------------
        if held_short > 0:  # cover the short on a bullish signal
            if cover_qty <= 0:
                return {**base, "reason": "bullish signal but no short to cover"}
            return {**base, "act": True, "side": "buy", "qty": cover_qty, "exit": True,
                    "reason": f"{sig['detail']} — cover short"}
        if news and news["score"] < 0:
            return {**base, "reason": f"buy setup vetoed by adverse news ({news['score']:+.2f})"}
        conflict = portfolio_conflict(symbol=asym, side="buy", swing_value=swing_val, equity=equity)
        if conflict:
            return {**base, "reason": conflict}
        if held_long > 0:
            return {**base, "reason": "already long this name in the day sleeve — no pyramiding"}
        if halted:
            return {**base, "reason": halted}
        if day_budget <= 0:
            return {**base, "reason": "day budget is zero under current conditions"}
        notional = _size()
        if notional < self._s.day_min_order_notional:
            return {**base, "reason": (f"sized ${notional:,.0f} below ${self._s.day_min_order_notional:,.0f} "
                                       "min (or day budget full)")}
        stop = _DAY_STOP_PCT.get(asset_class, 3.0)
        return {**base, "act": True, "side": "buy", "notional": round(notional, 2),
                "max_loss_est": round(notional * stop / 100.0, 2),
                "reason": f"{sig['kind']} buy ({sig['detail']})"}

    # ---- Phase 2: conviction / segmentation / net-beta hedge ---------------
    def _conviction(self, decision: dict, sig: dict, mc) -> float:
        """Rank score for an OPENING candidate (long or short), shaped by the
        shared context. For a LONG the strategist's avoid-list is a hard veto and
        an unfriendly/stressed tape down-weights momentum; for a SHORT the signs
        FLIP — shorting a strategist-avoided name into a risk-off tape is exactly
        what you want, while shorting a favored name is vetoed. Exits and
        non-actionable candidates score 0 (they don't compete for budget)."""
        if not (decision.get("act") and decision.get("side") in ("buy", "short")
                and not decision.get("exit")):
            return 0.0
        is_short = decision.get("side") == "short"
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

        if self._knob("day_respect_strategist") and _hit(mc.strategist_avoid):
            if is_short:
                score *= 1.25  # strategist dislikes it → confirms the short
            else:
                decision["reason"] += " | vetoed: strategist avoids this sleeve"
                return 0.0
        if _hit(mc.strategist_favor):
            if is_short:
                decision["reason"] += " | vetoed: won't short a strategist-favored name"
                return 0.0
            score *= 1.25
        kind = sig.get("kind")
        if kind == "momentum":
            if is_short:  # a breakdown short — risk-off / stress FAVORS it
                if mc.stress_on:
                    score *= 1.25
                elif not mc.risk_on:
                    score *= 1.1
            else:
                if mc.stress_on:
                    decision["reason"] += " | vetoed: no momentum-chasing in stress"
                    return 0.0
                if not mc.risk_on:
                    score *= 0.4
        return round(score, 3)

    def _segment(self, candidates: list[dict], portfolio, account: dict, plan: dict) -> None:
        """Pick which OPENING candidates (longs AND shorts) actually run this tick.
        Ranks every opener by conviction and greedily fills the highest-ranked ones
        that fit the AVAILABLE FUNDS — the day-sleeve budget headroom ∩ the broker's
        buying power — so we never blow the budget on lower-ranked setups and never
        exceed what the account can actually fund. Per-tick count + per-sector caps
        still apply. Exits never pass through here (they always run). Pure + fast.

        CONVICTION-GATED LEVERAGE: when armed, a high-signal candidate's notional is
        scaled by ``leverage_for`` (≤ day_max_leverage), and the funds ceiling rises
        to day_max_gross_leverage × the sleeve budget (still ∩ buying power). When
        NOT armed, leverage_for returns 1.0 and the gross cap collapses to the plain
        day budget — i.e. behaviour is identical to the un-levered path."""
        from app.trading.leverage import leverage_for
        armed = getattr(self, "_lev_armed", False)
        for c in candidates:
            c.setdefault("selected", False)
        opens = sorted(
            (c for c in candidates
             if c.get("act") and c.get("side") in ("buy", "short") and not c.get("exit")),
            key=lambda c: c.get("conviction", 0.0), reverse=True)
        # Available funds = min(day-sleeve GROSS ceiling headroom, broker buying
        # power). Gross ceiling = day budget × max_gross_leverage when armed, else
        # just the day budget (unchanged). buying power still hard-caps everything —
        # we can never order more than the (margin) account can actually fund.
        bp = (_to_float(account.get("daytrading_buying_power"))
              or _to_float(account.get("buying_power")) or 0.0)
        gross_mult = float(getattr(self._s, "day_max_gross_leverage", 2.0)) if armed else 1.0
        gross_ceiling = portfolio.day_budget * gross_mult
        funds = max(0.0, min(gross_ceiling - portfolio.day_value, bp))
        per_sector: dict[str, int] = {}
        picked = 0
        for c in opens:
            if c.get("conviction", 0.0) <= 0:
                continue  # reason already annotated by _conviction
            if picked >= self._knob("day_max_new_positions"):
                c["reason"] += " | skipped: hit max new positions this tick"
                continue
            sec = c.get("sector", "other")
            if per_sector.get(sec, 0) >= self._s.day_max_per_sector:
                c["reason"] += f" | skipped: {sec} sector cap this tick"
                continue
            base_notional = c.get("notional") or 0.0
            # Leverage the highest-quality setups only (1.0 = no leverage / not armed).
            lev, lev_reason = leverage_for(c, plan, self._s, armed)
            notional = base_notional * lev
            if notional > funds + 1e-6:
                c["reason"] += (f" | skipped: ${notional:,.0f} over ${funds:,.0f} available "
                                "(gross ceiling ∩ buying power) — lower-ranked, deferred")
                continue
            if lev > 1.0:
                c["notional"] = round(notional, 2)
                c["leverage"] = lev
                c["reason"] += f" | {lev}x leverage ({lev_reason})"
            c["selected"] = True
            picked += 1
            per_sector[sec] = per_sector.get(sec, 0) + 1
            funds -= notional

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

    def _build_short(self, decision: dict, plan: dict, price, asset_class: str) -> dict:
        """A selected SHORT as a single sell-to-open leg with a MANDATORY upside
        stop — the mirror of ``_build_long``. Equities only (Alpaca crypto is
        long-only). Distances come from ``compute_bracket_short`` (SL above / TP
        below), which enforces the same ``min_rr`` gate — an un-structurable short
        is SKIPPED (act=False), never opened naked. Whole-share qty (brackets need
        it). The bracket's stop is submitted WITH the entry, so a short is never
        left unprotected."""
        notional = (decision.get("notional") or 0.0) * plan["risk_scale"]
        if not price or price <= 0 or notional <= 0:
            return {**decision, "act": False, "mode": None}
        if asset_class != "equity":
            return {**decision, "act": False, "mode": None,
                    "reason": decision["reason"] + " | short skip: equities only"}
        sig = decision.get("signal") or {}
        qty = int(notional // price)
        if qty < 1:
            return {**decision, "act": False, "mode": None,
                    "reason": decision["reason"] +
                    f" | skip: ${notional:,.0f} < 1 share (${price:,.2f}) — can't bracket a short"}
        br = compute_bracket_short(sig, price, plan)
        if not br["ok"]:
            return {**decision, "act": False, "mode": None,
                    "reason": decision["reason"] + f" | skip: {br['reason']}"}
        primary_notional = qty * price
        leg = {"symbol": decision["symbol"], "side": "sell", "qty": qty, "direction": "short",
               "asset_class": "equity", "role": "primary", "entry": round(price, 2),
               "exit_type": br["exit_type"], "bracket": br["exit_type"] == "bracket",
               "sl_price": round(br["sl"], 2), "rr": round(br["rr"], 2),
               "have_sigma": br["have_sigma"]}
        if br["exit_type"] == "trailing":
            leg["trail_price"] = round(br["trail_dist"], 2)
            why = (decision["reason"] +
                   f", trailing-stop cover ${leg['trail_price']} ({plan['trail_sigma']}σ), "
                   f"SL≈{leg['sl_price']}, R:R proxy {leg['rr']}")
        else:
            leg["tp_price"] = round(br["tp"], 2)
            why = (decision["reason"] +
                   f", short bracket SL {leg['sl_price']} / TP {leg['tp_price']}"
                   f"{' (VWAP)' if sig.get('kind') == 'reversion' else ''}, R:R {leg['rr']}")
        max_loss = br["stop_dist"] * qty
        return {**decision, "mode": "hedged", "legs": [leg], "plan_bias": plan["bias"],
                "notional": round(primary_notional, 2),
                "max_loss_est": round(max(0.0, max_loss), 2), "reason": why}

    def _leg_decision(self, sym: str, sym_bars: list[dict], side: str,
                      notional: float, reason: str) -> dict:
        """Build a day decision for ONE leg of a pair trade. Reuses the σ struct
        from the bars (for vol-normalized brackets) but forces the direction +
        a momentum/trailing exit so the relative-strength winner can run."""
        struct = evaluate_setups(
            sym_bars, breakout_buffer_pct=self._s.day_breakout_buffer_pct,
            momentum_min_pct=self._knob("day_momentum_min_pct"), reversion_z=self._knob("day_reversion_z"),
            enabled=("momentum",))
        sig = {**struct, "kind": "momentum",
               "direction": "buy" if side == "buy" else "short", "strength": 2.0}
        price = sig.get("last")
        return {"symbol": sym, "strategist_sym": sym, "asset_class": "equity", "signal": sig,
                "news": None, "act": True, "side": side, "notional": round(notional, 2),
                "qty": None, "price": price, "sector": sector_for(sym),
                "conviction": 2.0, "reason": reason}

    async def _maybe_pair_trade(self, bars, plan, cfg, portfolio, account, deployed,
                                market_ctx, run_id, trade_date, day_hold) -> dict | None:
        """Open the single best relative-strength PAIR this tick (long strong leg +
        short weak leg) when pairs mode is armed and both legs fit the remaining
        funds. Sector-neutral. Each leg carries its own bracket, so a one-leg fill
        is still independently stopped (basis risk noted). Returns a summary or None."""
        if not (cfg["enabled"] and self._s.day_pairs_enabled and cfg["pairs_enabled"]
                and not cfg["soft_stop"]
                and self._s.day_short_enabled and cfg["short_enabled"]):
            return None

        def _bars(sym):
            return bars.get(sym) or bars.get(alpaca_symbol(sym, "equity")[0]
                                             if alpaca_symbol(sym, "equity") else sym) or []
        bars_by = {s: _bars(s) for pair in PAIRS for s in pair}
        flat = lambda s: abs(day_hold.get(norm_symbol(s), 0.0)) < 1e-6
        best = best_pair_trade(bars_by, PAIRS, min_spread_pct=self._s.day_pairs_min_spread_pct,
                               is_flat=flat)
        if not best:
            return None
        long_sym, short_sym = best["long"], best["short"]
        # Per-leg size = standard day position, both legs must fit available funds.
        bp = (_to_float(account.get("daytrading_buying_power"))
              or _to_float(account.get("buying_power")) or 0.0)
        funds = max(0.0, min(portfolio.day_budget - deployed, bp))
        leg_notional = max(self._s.day_min_order_notional,
                           portfolio.day_budget * (self._s.day_max_position_pct / 100.0)
                           * min(1.0, best["strength"] / 2.0))
        if 2 * leg_notional > funds + 1e-6:
            log.info("pair %s/%s skipped: 2x $%.0f legs over $%.0f available",
                     long_sym, short_sym, leg_notional, funds)
            return None
        # Short leg must be borrowable.
        shortable, etb = await self._broker.shortability(short_sym)
        if not shortable or (self._s.day_short_easy_to_borrow_only and not etb):
            return None

        long_dec = self._leg_decision(long_sym, bars_by[long_sym], "buy", leg_notional,
                                      best["detail"] + " | LONG leg")
        short_dec = self._leg_decision(short_sym, bars_by[short_sym], "short", leg_notional,
                                       best["detail"] + " | SHORT leg")
        long_out = self._build_long(long_dec, plan, long_dec.get("price"), "equity")
        short_out = self._build_short(short_dec, plan, short_dec.get("price"), "equity")
        submitted = []
        for out in (long_out, short_out):
            if out.get("act"):
                out["submitted"] = await self._submit(out, "equity")
                self._journal(run_id, trade_date, out, market_ctx)
                if out.get("submitted"):
                    submitted.append(out["symbol"])
        return {"mode": "pair", "long": long_sym, "short": short_sym,
                "spread": best["spread"], "submitted": submitted, "reason": best["detail"]}

    async def _net_beta_hedge(self, account: dict, positions: list[dict], bars: dict) -> dict | None:
        """Keep the DAY sleeve near market-neutral with ONE SHORT in a dedicated
        broad S&P tracker (``day_hedge_symbol``, default VOO, β≈1) sized to the
        book's net LONG beta. This replaces buying the -1x inverse SH (decay +
        oscillation). Two key properties:

          * direct short, no ETF decay / leverage-divisor math; and
          * the CURRENT hedge size is read from the FRESH broker position, not the
            lagging filled-ledger — which is what made the old SH hedge double-apply
            its delta and ping-pong. So it converges to target and stays there.

        Targets a whole-share SHORT (negative position) offsetting net-long beta and
        only trades the delta beyond the band — it can never accumulate into a pile."""
        hedge_sym = self._s.day_hedge_symbol
        hn = norm_symbol(hedge_sym)
        day_hold = sleeve_holdings(self._sqlite, "day")
        pos_price = {norm_symbol(p.get("symbol", "")): _to_float(p.get("current_price"))
                     for p in positions}
        pos_qty = {norm_symbol(p.get("symbol", "")): _to_float(p.get("qty"))
                   for p in positions}
        net_beta_usd = 0.0
        for n, q in day_hold.items():
            if n == hn:
                continue
            px = pos_price.get(n) or 0.0
            net_beta_usd += q * px * beta_for(n)
        equity = _to_float(account.get("equity")) or 0.0
        band = max(self._knob("day_net_beta_band_pct") / 100.0 * equity, self._s.day_min_order_notional)
        px = pos_price.get(hn) or _last_close(bars, hedge_sym) or 0.0
        if px <= 0:
            return None
        # CURRENT hedge from FRESH broker truth (signed: negative = short). Falling
        # back to the ledger only if the broker doesn't report the position.
        cur = pos_qty.get(hn)
        if cur is None:
            cur = day_hold.get(hn, 0.0)
        target = -int(max(0.0, net_beta_usd) / px)  # SHORT to offset net-long beta
        delta = target - cur
        if abs(delta * px) < band:
            return None  # within band — no churn
        side = "sell" if delta < 0 else "buy"  # sell = open/add short; buy = cover
        qty = round(abs(delta), 0)
        if qty <= 0:
            return None
        reason = (f"net-beta hedge: day net β ${net_beta_usd:,.0f} -> short {abs(target)} "
                  f"{hedge_sym} (have {cur:g}) -> {side} {qty:g}")
        ok = await self._submit_hedge_order(hedge_sym, side, qty, reason)
        return {"symbol": hedge_sym, "asset_class": "equity", "act": True, "side": side,
                "qty": qty, "notional": round(qty * px, 2), "mode": "net_beta_hedge",
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
            # Key off whether a qty is set, NOT the side: a cover (buy-to-close a
            # short) carries a qty, an open-long buy carries a notional, a close
            # carries a qty — submit_order needs exactly one.
            order = await self._broker.submit_order(
                asym, side, qty=qty, notional=(notional if qty is None else None),
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
            if is_trailing and leg["side"] in ("buy", "sell") and leg.get("trail_price"):
                # Attach the trailing-stop exit — but ONLY after the entry has
                # actually filled. Alpaca refuses to open the opposite-side stop
                # while the entry is still working; submitting it immediately left
                # the position NAKED (no stop). Wait briefly for the fill (paper
                # market orders settle well under a second); if it doesn't, skip
                # rather than submit a doomed order. The COVER side mirrors the
                # entry: a long (buy) trails a SELL stop, a short (sell) trails a
                # BUY stop.
                cover_side = "buy" if leg["side"] == "sell" else "sell"
                filled = await self._await_fill(order.get("id"))
                if filled:
                    await self._submit_trailing_exit(
                        cid, leg["symbol"], leg["qty"], leg["trail_price"], is_crypto,
                        cover_side=cover_side)
                else:
                    log.warning("day %s entry not filled yet — deferring trailing stop", leg["symbol"])
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
                                    trail_price: float, is_crypto: bool,
                                    cover_side: str = "sell") -> bool:
        """Attach a TRAILING-STOP exit to a momentum entry (equities only) — the
        winners-run exit. ``cover_side`` is 'sell' for a long entry, 'buy' to cover
        a short. Recorded as an unlinked day order (proposal_id NULL) so reconcile()
        tracks its fill and sleeve_holdings nets the position out when it triggers
        (mirrors how bracket OCO child legs are tracked)."""
        cid = f"{parent_cid}-trail"
        self._sqlite.execute(
            "INSERT OR IGNORE INTO bot_orders (proposal_id, client_order_id, symbol, side, "
            "order_type, qty, status, submitted_at, sleeve) "
            "VALUES (NULL,?,?,?,?,?, 'submitting', datetime('now'), 'day')",
            [cid, symbol, cover_side, "trailing_stop", qty],
        )
        try:
            order = await self._broker.submit_order(
                symbol, cover_side, qty=qty, order_type="trailing_stop",
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

    async def _await_fill(self, order_id, *, tries: int = 6, delay: float = 0.4) -> bool:
        """Poll an order until it's FILLED (or terminal), so a follow-on exit can
        be attached. Paper market orders settle in well under a second; we give it
        up to ~tries·delay. Returns True only on a confirmed fill."""
        if not order_id:
            return False
        for i in range(tries):
            try:
                o = await self._broker.get_order(order_id)
            except BrokerError:
                return False
            status = (o.get("status") or "").lower()
            if status == "filled":
                return True
            if status in ("canceled", "cancelled", "expired", "rejected", "done_for_day"):
                return False
            if i < tries - 1:
                await asyncio.sleep(delay)
        return False

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
            "leverage": self._leverage_status(cfg),
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
        # Pull the broker's rejection/error reason (lives on the ORDER, not the
        # proposal) so a rejected proposal can explain WHY it failed in the panel.
        rows = self._sqlite.fetchall(
            "SELECT p.id, p.symbol, p.side, p.qty, p.notional, p.conviction, p.max_loss_est, "
            "p.rationale, p.status, p.created_at, "
            "(SELECT o.error FROM bot_orders o WHERE o.proposal_id = p.id "
            " AND o.error IS NOT NULL ORDER BY o.id DESC LIMIT 1) AS error "
            "FROM bot_proposals p WHERE p.sleeve = 'day' ORDER BY p.id DESC LIMIT 20"
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
        self._refresh_knobs()  # reflect accepted overrides in the previewed plan
        plan = build_plan(self._latest_regime(), vol_signal, base_stop_pct=self._knob("day_stop_pct"),
                          base_tp_pct=self._s.day_tp_pct, base_hedge_ratio=self._s.day_hedge_ratio,
                          stop_sigma=self._knob("day_stop_sigma"), tp_sigma=self._knob("day_tp_sigma"),
                          trail_sigma=self._s.day_trail_sigma,
                          reversion_stop_sigma=self._s.day_reversion_stop_sigma,
                          min_rr=self._s.day_min_rr,
                          stop_floor_pct=self._knob("day_stop_floor_pct"))
        plan["hedged_enabled"] = self._s.day_hedged_enabled
        plan["vol_signal"] = vol_signal
        return plan

    async def _broadcast(self, event: str) -> None:
        try:
            await self._hub.broadcast(BOT_TOPIC, {"type": "bot", "event": event})
        except Exception:
            log.debug("day broadcast failed", exc_info=True)
