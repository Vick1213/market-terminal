"""TradingBotService — strategist allocation -> guarded Alpaca PAPER orders.

Flow:
  propose()        read the latest strategist snapshot + the broker's account &
                   positions (GROUND TRUTH), diff current weights to the
                   strategist targets, and emit one proposal per drift. Each
                   proposal carries conviction, the strategist evidence, a
                   forced bear-case, an explicit invalidation, and an
                   illustrative max-loss. Guardrails run here; a blocked
                   proposal is recorded but can never be executed. NO ORDERS
                   ARE PLACED by propose().
  execute(id)      the human gate: re-validate one 'proposed' row against a
                   FRESH account snapshot, then submit it to the paper account
                   with a deterministic client_order_id (no double-submits).
  run()            propose, then — only if mode == 'auto' AND the kill switch is
                   on — execute every non-blocked proposal. Paper only, always.
  reconcile()      pull the broker's orders and overwrite local order/proposal
                   state from them. The broker is truth; the ledger follows it.
  set_enabled()    kill switch. Turning it OFF also cancels open broker orders.

The bot is NOT on the scheduler — nothing runs autonomously unless you call
run() with the kill switch on and mode 'auto'. Even then it is paper-only.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ingest.alpaca import alpaca_symbol
from app.trading.broker import BrokerError
from app.trading.broker_cache import BrokerState
from app.trading.optimizer import PortfolioOptimizer
from app.trading.guardrails import (
    GuardrailConfig,
    buys_halted,
    evaluate_order,
    is_dust,
    norm_symbol,
)
from app.ws.hub import ConnectionManager

log = logging.getLogger("market.trading.bot")

BOT_TOPIC = "bot"

# strategist bucket -> asset_class understood by ingest.alpaca.alpaca_symbol.
BUCKET_ASSET_CLASS = {
    "equities": "equity",
    "metals": "metal",
    "crypto": "crypto",
    "cash": "equity",  # SGOV / T-bill ETFs are ordinary equities to Alpaca
}

# Bridge from context.sector_for's coarse labels to the RRG sector ETF whose
# quadrant governs that group — so a held swing name can be mapped to an RRG
# read for the rotation-lifecycle exit. Groups with no clean sector ETF (crypto,
# metals, broad index, bonds) get no RRG read and fall back to the weekly trend.
_SECTOR_TO_ETF = {
    "tech": "XLK", "megacap-tech": "XLK", "software": "XLK", "semis": "XLK",
    "financials": "XLF", "fintech": "XLF",
    "energy": "XLE", "healthcare": "XLV", "industrials": "XLI",
    "staples": "XLP", "consumer": "XLY", "media": "XLY",
    "utilities": "XLU", "materials": "XLB", "real-estate": "XLRE",
}

DISCLAIMER = (
    "Paper trading only. Orders are sized from the strategist's mechanical "
    "signal synthesis (NOT financial advice) and gated by hard code limits. "
    "Proposals are not advice; the broker account is the source of truth."
)


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bear_case(kind: str, bucket: str) -> tuple[str, str]:
    """(bear_case, invalidation) — a forced counter-thesis per holding type, so
    every proposal ships with its own kill criteria (recon requirement)."""
    if kind == "stock":
        return (
            "Signal-overlap pick built on lagging inputs (13F ≤45d, Senate PTR, "
            "Form 4, week-old news). Crowding or a single headline can reverse it.",
            "Exit if regime flips to risk-off/stress, price lags the benchmark "
            ">5pp over 1m, or a Lazy-Prices risk-factor rewrite hits.",
        )
    if kind == "sector":
        return (
            "RRG leadership rotates; a momentum tilt decays as relative strength "
            "mean-reverts.",
            "Trim when the sector drops out of the leading/improving RRG quadrant.",
        )
    if bucket == "metals":
        return (
            "Rising real yields pressure gold; silver carries higher beta to a "
            "risk-off air-pocket.",
            "Cut on a decisive risk-on turn with real yields climbing.",
        )
    if bucket == "crypto":
        return (
            "High-beta and liquidity-sensitive; a net-liquidity drain or a "
            "crowded-COT unwind hits crypto hardest.",
            "Reduce when the net-liquidity 1m delta turns negative or COT prints "
            "a 3y max-bullish extreme.",
        )
    if bucket == "cash":
        return (
            "Opportunity cost in a sustained rally; near-zero drawdown otherwise.",
            "Redeploy when the regime turns decisively risk-on.",
        )
    return ("Mechanical allocation tilt; inputs lag and rules are blunt.",
            "Re-evaluate on the next strategist snapshot or a regime flip.")


def build_proposals(
    snapshot: dict,
    account: dict,
    positions: list[dict],
    watchlist_alpaca: set[str],
    cfg: GuardrailConfig,
    stop_pct: dict[str, float],
    open_orders: list[dict] | None = None,
    sleeve_pct: float = 100.0,
    exclude_qty: dict[str, float] | None = None,
    posture: dict | None = None,
    sector_max_pct: float | None = None,
) -> dict:
    """Pure planning step: snapshot + broker state -> proposal dicts.

    No I/O, no persistence — so the whole diff-and-gate logic is unit-testable
    with plain dicts. Returns equity/buying_power context, the proposal list,
    any halt reason, and positions the strategist doesn't track (surfaced, never
    auto-sold).
    """
    equity = _to_float(account.get("equity")) or 0.0
    buying_power = _to_float(account.get("buying_power")) or 0.0
    halted = buys_halted(account, cfg)

    # broker positions -> {norm symbol: {value, qty, price}}
    pos_map: dict[str, dict] = {}
    for p in positions:
        sym = p.get("symbol")
        if not sym:
            continue
        pos_map[norm_symbol(sym)] = {
            "value": _to_float(p.get("market_value")) or 0.0,
            "qty": _to_float(p.get("qty")) or 0.0,
            "price": _to_float(p.get("current_price")),
        }

    # In-flight orders already move toward the target — a pending BUY is
    # acquiring it, a pending SELL is shedding it. Net them into an "effective"
    # position so a re-run of propose() doesn't re-propose a trade still working
    # (the core stale-info fix: you placed orders, refresh shouldn't repeat them).
    pending: dict[str, float] = {}
    for o in open_orders or []:
        osym = o.get("symbol")
        if not osym:
            continue
        n = norm_symbol(osym)
        val = _to_float(o.get("notional"))
        if val is None:
            oqty = _to_float(o.get("qty"))
            price = (pos_map.get(n) or {}).get("price")
            val = oqty * price if (oqty and price) else None
        if val is None:
            continue
        pending[n] = pending.get(n, 0.0) + (val if (o.get("side") or "").lower() == "buy" else -val)

    # strategist holdings -> targets, aggregated by Alpaca symbol.
    targets: dict[str, dict] = {}
    for bucket in snapshot.get("buckets", []) or []:
        bkey = bucket.get("key")
        asset_class = BUCKET_ASSET_CLASS.get(bkey, "equity")
        for h in bucket.get("holdings", []) or []:
            strat_sym = h.get("symbol")
            if not strat_sym:
                continue
            mapped = alpaca_symbol(strat_sym, asset_class)
            if mapped is None:
                continue  # Alpaca can't trade this (spot codes etc.)
            asym = mapped[0]
            tp = _to_float(h.get("weight_pct")) or 0.0
            t = targets.setdefault(asym, {
                "symbol": asym, "strategist_sym": strat_sym, "bucket": bkey,
                "kind": h.get("kind"), "target_pct": 0.0, "score": h.get("score"),
                "evidence": list(h.get("evidence") or []),
            })
            t["target_pct"] += tp

    allowlist = {t["symbol"] for t in targets.values()} | set(watchlist_alpaca)

    # POSTURE sizing. The macro GROSS dial and the conviction tilt live in the
    # STRATEGIST (the single brain — its snapshot weights are ALREADY posture-scaled
    # by gross_factor and score-weighted), so the swing bot must NOT re-apply them or
    # it would double-count. The one execution-level control the swing bot adds when
    # posture-sizing is armed is a HARD per-SECTOR gross cap (which the strategist
    # does not enforce). Off by default: no cap, exact legacy sizing.
    scaled_tv: dict[str, float] = {
        asym: equity * t["target_pct"] / 100.0 * sleeve_pct / 100.0
        for asym, t in targets.items()
    }

    if posture and sector_max_pct and equity > 0:
        from app.trading.context import sector_for  # local: avoid import cycle
        cap = equity * sector_max_pct / 100.0
        by_sector: dict[str, float] = {}
        sec_of: dict[str, str] = {}
        for asym, tv in scaled_tv.items():
            sec = sector_for(asym)
            sec_of[asym] = sec
            by_sector[sec] = by_sector.get(sec, 0.0) + tv
        for asym in scaled_tv:
            total = by_sector[sec_of[asym]]
            if total > cap and total > 0:
                scaled_tv[asym] *= cap / total

    proposals: list[dict] = []
    for t in targets.values():
        asym = t["symbol"]
        nrm = norm_symbol(asym)
        bucket = t["bucket"]
        target_pct = t["target_pct"]
        # Scale the strategist target to THIS sleeve's slice of capital so the
        # swing book leaves room for the day sleeve (sleeve_pct < 100) — and, when
        # posture sizing is on, by the macro gross dial / conviction / sector cap.
        target_value = scaled_tv[asym]
        cur = pos_map.get(nrm, {})
        price = cur.get("price")
        # Carve out shares the OTHER sleeve owns so we never trade its position.
        ex = (exclude_qty or {}).get(nrm, 0.0)
        sleeve_qty = max(0.0, float(cur.get("qty") or 0.0) - ex)
        current_value = sleeve_qty * float(price) if price else float(cur.get("value") or 0.0)
        current_pct = (current_value / equity * 100.0) if equity else 0.0
        # effective = what we'll hold once in-flight orders fill.
        effective_value = current_value + pending.get(nrm, 0.0)
        delta = target_value - effective_value
        drift_pp = abs(delta) / equity * 100.0 if equity else 0.0
        order_value = abs(delta)

        bear, invalidation = _bear_case(t.get("kind") or "", bucket)
        stop = stop_pct.get(bucket, 10.0)
        proposal = {
            "symbol": asym,
            "strategist_sym": t["strategist_sym"],
            "bucket": bucket,
            "target_pct": round(target_pct, 2),
            "current_pct": round(current_pct, 2),
            "target_value": round(target_value, 2),
            "current_value": round(current_value, 2),
            "delta_value": round(delta, 2),
            "conviction": t.get("score"),
            "qty": None,
            "notional": None,
            "order_type": "market",
            "max_loss_est": round(target_value * stop / 100.0, 2),
            "rationale": {
                "kind": t.get("kind"),
                "score": t.get("score"),
                "evidence": t["evidence"],
                "bear_case": bear,
                "invalidation": invalidation,
            },
            "blocks": [],
            "status": "proposed",
            "side": "buy" if delta >= 0 else "sell",
            "sleeve": "swing",
            "pending_value": round(pending.get(norm_symbol(asym), 0.0), 2),
        }

        # within the rebalance band, or below the dust floor -> skip (no trade).
        if drift_pp < cfg.rebalance_band_pp:
            proposal["status"] = "skipped"
            in_flight = abs(pending.get(norm_symbol(asym), 0.0)) > 1e-6
            proposal["blocks"] = [
                (f"already working — ${abs(pending.get(norm_symbol(asym), 0.0)):,.0f} in-flight "
                 f"toward target" if in_flight else
                 f"within rebalance band ({drift_pp:.1f}pp < {cfg.rebalance_band_pp:.1f}pp)")
            ]
            proposals.append(proposal)
            continue
        if is_dust(order_value, cfg):
            proposal["status"] = "skipped"
            proposal["blocks"] = [
                f"order ${order_value:,.0f} below ${cfg.min_order_notional:,.0f} min"
            ]
            proposals.append(proposal)
            continue

        side = proposal["side"]
        extra_block: list[str] = []
        if side == "buy":
            proposal["notional"] = round(order_value, 2)
        else:
            # Only sell shares THIS sleeve owns (sleeve_qty already carved).
            if not price or sleeve_qty <= 0:
                extra_block.append("no current price/holding to size the sell")
            else:
                qty = min(sleeve_qty, order_value / float(price))
                proposal["qty"] = round(qty, 6)

        blocks = evaluate_order(
            symbol=asym,
            side=side,
            order_value=order_value,
            current_position_value=current_value,
            equity=equity,
            buying_power=buying_power,
            allowlist=allowlist,
            cfg=cfg,
            buys_halted_reason=halted,
        ) + extra_block
        proposal["blocks"] = blocks
        proposal["status"] = "blocked" if blocks else "proposed"
        proposals.append(proposal)

    target_norms = {norm_symbol(s) for s in targets}
    untracked = [
        {"symbol": p.get("symbol"),
         "value": _to_float(p.get("market_value")),
         "qty": _to_float(p.get("qty"))}
        for p in positions
        if p.get("symbol") and norm_symbol(p["symbol"]) not in target_norms
    ]

    # actionable first, then blocked, then skipped — and by drift size.
    order_rank = {"proposed": 0, "blocked": 1, "skipped": 2}
    proposals.sort(key=lambda p: (order_rank.get(p["status"], 3),
                                  -abs(p["delta_value"])))
    return {
        "equity": round(equity, 2),
        "buying_power": round(buying_power, 2),
        "halted": halted,
        "proposals": proposals,
        "untracked_positions": untracked,
    }


# Day sleeve stops, mirrored from daytrader._DAY_STOP_PCT (kept here to avoid a
# circular import — daytrader imports sleeve_holdings from this module). The day
# sleeve has no fixed profit target: it exits on signal reversal / adverse news.
_DAY_STOP_PCT = {"equity": 2.0, "crypto": 5.0}


def _is_crypto(symbol: str) -> bool:
    return "/" in (symbol or "")


def _exit_plan(
    symbol: str,
    sleeve: str,
    avg_entry: float | None,
    swing_prop: dict | None,
    stop_pct_by_bucket: dict[str, float],
) -> dict | None:
    """A concrete exit plan for a held position: the protective stop both sleeves
    imply, plus (swing only) the strategist target and the written invalidation."""
    if sleeve == "day":
        stop_pct = _DAY_STOP_PCT["crypto" if _is_crypto(symbol) else "equity"]
        plan = {
            "stop_pct": stop_pct,
            "stop_price": round(avg_entry * (1 - stop_pct / 100.0), 4) if avg_entry else None,
            "target_value": None,
            "target_pct": None,
            "invalidation": "Momentum sleeve — exits on signal reversal or major "
                            "adverse news; no fixed price target.",
        }
        return plan

    bucket = (swing_prop or {}).get("bucket")
    stop_pct = stop_pct_by_bucket.get(bucket or "", 10.0)
    invalidation = None
    rationale = (swing_prop or {}).get("rationale")
    if isinstance(rationale, str):
        try:
            rationale = json.loads(rationale)
        except (ValueError, TypeError):
            rationale = None
    if isinstance(rationale, dict):
        invalidation = rationale.get("invalidation")
    return {
        "stop_pct": stop_pct,
        "stop_price": round(avg_entry * (1 - stop_pct / 100.0), 4) if avg_entry else None,
        "target_value": _to_float((swing_prop or {}).get("target_value")),
        "target_pct": _to_float((swing_prop or {}).get("target_pct")),
        "invalidation": invalidation,
    }


class TradingBotService:
    def __init__(
        self,
        duck: DuckStore,
        sqlite: SqliteStore,
        hub: ConnectionManager,
        broker: BrokerState,
        cfg: GuardrailConfig,
        optimizer: PortfolioOptimizer,
        *,
        stop_pct: dict[str, float],
        default_mode: str = "proposal",
        settings=None,
    ) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._hub = hub
        self._broker = broker
        self._cfg = cfg
        self._optimizer = optimizer
        self._stop_pct = dict(stop_pct)
        self._s = settings
        # bot_config row is seeded by schema.init_sqlite; default_mode only seeds
        # a fresh row if somehow missing.
        self._sqlite.execute(
            "INSERT OR IGNORE INTO bot_config (id, enabled, mode) VALUES (1, 0, ?)",
            [default_mode],
        )

    # ---- config / kill switch ---------------------------------------------
    def _traded_by_sleeve(self) -> dict[tuple[str, str], float]:
        """(sleeve, norm_symbol) -> total real shares the sleeve has traded (sum of
        filled buy+sell magnitudes), EXCLUDING synthetic reconcile/heal/flatten
        rows. Lets portfolio() attribute a broker-vs-ledger residual to the bot
        that actually trades a name rather than calling it 'manual'."""
        out: dict[tuple[str, str], float] = {}
        for r in self._sqlite.fetchall(
            "SELECT sleeve, symbol, filled_qty, order_type, client_order_id FROM bot_orders "
            "WHERE status = 'filled' AND filled_qty IS NOT NULL"
        ):
            cid = (r["client_order_id"] or "").lower()
            if (r["order_type"] or "").lower() == "reconcile" or \
                    cid.startswith(("reconcile", "heal", "flat", "manual")):
                continue
            q = _to_float(r["filled_qty"]) or 0.0
            if not q:
                continue
            key = (r["sleeve"] or "swing", norm_symbol(r["symbol"] or ""))
            out[key] = out.get(key, 0.0) + abs(q)
        return out

    def _config(self) -> dict:
        row = self._sqlite.fetchone(
            "SELECT enabled, mode, updated_at, swing_managed_exits, swing_posture_sizing "
            "FROM bot_config WHERE id = 1"
        )
        if row is None:
            return {"enabled": False, "mode": "proposal", "updated_at": None,
                    "managed_exits": False, "posture_sizing": False}
        return {"enabled": bool(row["enabled"]), "mode": row["mode"],
                "updated_at": row["updated_at"],
                # Panel toggles for the swing execution overhaul (both default OFF
                # and additionally gated by their env master switch at run time).
                "managed_exits": bool(row["swing_managed_exits"]),
                "posture_sizing": bool(row["swing_posture_sizing"])}

    async def set_managed_exits(self, enabled: bool) -> dict:
        """Arm/disarm enforced protective exits (stop-loss / trailing / profit-take
        / RRG rotation) for the swing sleeve. The env master ``swing_managed_exits``
        must also be on for the exits to actually fire."""
        self._sqlite.execute(
            "UPDATE bot_config SET swing_managed_exits = ?, updated_at = datetime('now') WHERE id = 1",
            [1 if enabled else 0],
        )
        log.info("swing MANAGED EXITS -> %s", "ENABLED" if enabled else "DISABLED")
        await self._broadcast("config")
        return self._config()

    async def set_posture_sizing(self, enabled: bool) -> dict:
        """Arm/disarm posture-scaled gross + per-sector cap in build_proposals.
        OFF (default) = legacy sizing. The env master ``swing_posture_sizing`` must
        also be on for the scaling to apply."""
        self._sqlite.execute(
            "UPDATE bot_config SET swing_posture_sizing = ?, updated_at = datetime('now') WHERE id = 1",
            [1 if enabled else 0],
        )
        log.info("swing POSTURE SIZING -> %s", "ENABLED" if enabled else "DISABLED")
        await self._broadcast("config")
        return self._config()

    async def set_enabled(self, enabled: bool) -> dict:
        self._sqlite.execute(
            "UPDATE bot_config SET enabled = ?, updated_at = datetime('now') WHERE id = 1",
            [1 if enabled else 0],
        )
        canceled = None
        if not enabled and self._broker.enabled:
            # Kill switch: best-effort cancel of any resting orders.
            try:
                canceled = await self._broker.cancel_all()
            except BrokerError as exc:
                log.warning("kill switch: cancel_all failed (%s)", exc)
        log.info("bot kill switch -> %s", "ENABLED" if enabled else "DISABLED")
        await self._broadcast("config")
        out = self._config()
        if canceled is not None:
            out["canceled_orders"] = canceled
        return out

    # ---- swing execution overhaul helpers ---------------------------------
    def _sizing_posture(self) -> dict | None:
        """The posture dict to scale build_proposals by, or None for LEGACY sizing.
        Gated by BOTH the panel toggle and the env master — None preserves today's
        behaviour exactly (gross_factor 1.0, no sector cap)."""
        if not (self._config().get("posture_sizing")
                and getattr(self._s, "swing_posture_sizing", False)):
            return None
        try:
            from app.edge.posture import compute_posture
            return compute_posture(self._duck)
        except Exception:
            log.debug("posture compute failed", exc_info=True)
            return None

    def _sector_max_pct(self) -> float | None:
        return getattr(self._s, "swing_sector_max_pct", None)

    async def set_mode(self, mode: str) -> dict:
        if mode not in ("proposal", "auto"):
            raise ValueError("mode must be 'proposal' or 'auto'")
        self._sqlite.execute(
            "UPDATE bot_config SET mode = ?, updated_at = datetime('now') WHERE id = 1",
            [mode],
        )
        await self._broadcast("config")
        return self._config()

    # ---- universe ----------------------------------------------------------
    def _watchlist_alpaca(self) -> set[str]:
        out: set[str] = set()
        for r in self._sqlite.fetchall("SELECT symbol, asset_class FROM watchlist"):
            mapped = alpaca_symbol(r["symbol"], r["asset_class"])
            if mapped is not None:
                out.add(mapped[0])
        return out

    def _latest_snapshot(self) -> dict | None:
        row = self._duck.fetchone(
            "SELECT detail FROM strategist_snapshots ORDER BY ts DESC LIMIT 1"
        )
        if not row or not row[0]:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    # ---- propose -----------------------------------------------------------
    async def propose(self) -> dict:
        """Generate proposals from the latest strategist snapshot vs the broker
        account. Read-only on the broker; persists the proposals. No orders."""
        if not self._broker.enabled:
            return {"ok": False, "detail": "Alpaca paper keys not configured",
                    "config": self._config()}
        snapshot = self._latest_snapshot()
        if snapshot is None:
            return {"ok": False, "detail": "no strategist snapshot yet — run /api/strategist/run",
                    "config": self._config()}

        account = await self._broker.account()
        positions = await self._broker.positions()
        open_orders = await self._broker.open_orders()
        swing_pct = float(self._optimizer.latest().get("swing_pct") or 100.0)
        posture = self._sizing_posture()
        plan = build_proposals(
            snapshot, account, positions, self._watchlist_alpaca(),
            self._cfg, self._stop_pct, open_orders=open_orders,
            sleeve_pct=swing_pct, exclude_qty=sleeve_holdings(self._sqlite, "day"),
            posture=posture, sector_max_pct=self._sector_max_pct() if posture else None,
        )
        plan["sleeve_pct"] = swing_pct
        plan["posture"] = posture

        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        strat_asof = snapshot.get("as_of")

        # Supersede the previous open swing set so "current proposals" = latest run.
        self._sqlite.execute(
            "UPDATE bot_proposals SET status = 'expired', updated_at = datetime('now') "
            "WHERE status IN ('proposed', 'blocked', 'skipped') AND sleeve = 'swing'"
        )
        ids: list[int] = []
        for p in plan["proposals"]:
            # Atomic INSERT+rowid: a separate SELECT last_insert_rowid() could
            # return another thread's rowid on the shared SQLite connection and
            # poison the deterministic client_order_id.
            pid = self._sqlite.execute_returning_id(
                "INSERT INTO bot_proposals (run_id, created_at, symbol, strategist_sym, "
                "bucket, side, order_type, qty, notional, target_pct, current_pct, "
                "target_value, current_value, delta_value, conviction, max_loss_est, "
                "rationale, status, blocks, strategist_asof, sleeve) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'swing')",
                [run_id, now, p["symbol"], p["strategist_sym"], p["bucket"], p["side"],
                 p["order_type"], p["qty"], p["notional"], p["target_pct"],
                 p["current_pct"], p["target_value"], p["current_value"],
                 p["delta_value"], p["conviction"], p["max_loss_est"],
                 json.dumps(p["rationale"]), p["status"], json.dumps(p["blocks"]),
                 strat_asof],
            )
            p["id"] = pid
            ids.append(pid)

        await self._broadcast("propose")
        n_actionable = sum(1 for p in plan["proposals"] if p["status"] == "proposed")
        log.info("bot proposed run %s: %d proposals (%d actionable)",
                 run_id, len(plan["proposals"]), n_actionable)
        return {
            "ok": True,
            "run_id": run_id,
            "config": self._config(),
            "account": self._account_summary(account),
            "strategist_as_of": strat_asof,
            "n_actionable": n_actionable,
            **plan,
            "disclaimer": DISCLAIMER,
        }

    def _latest_price(self, symbol: str) -> float | None:
        """Latest known close for whole-share sizing of non-fractionable buys.
        Reads the cached daily history (any source) — None if we have no price."""
        try:
            row = self._duck.fetchone(
                "SELECT close FROM ts_price WHERE symbol = ? AND close IS NOT NULL "
                "ORDER BY ts DESC LIMIT 1",
                [symbol],
            )
            return float(row[0]) if row and row[0] is not None else None
        except Exception:
            return None

    # ---- execute (the human gate) -----------------------------------------
    async def execute(self, proposal_id: int) -> dict:
        """Submit one proposed order to the paper account after re-validating it
        against a FRESH account snapshot. Refused while the kill switch is off."""
        cfg_state = self._config()
        if not cfg_state["enabled"]:
            return {"ok": False, "detail": "bot is disabled (kill switch off) — enable to trade"}
        if not self._broker.enabled:
            return {"ok": False, "detail": "Alpaca paper keys not configured"}

        row = self._sqlite.fetchone(
            "SELECT * FROM bot_proposals WHERE id = ?", [proposal_id]
        )
        if row is None:
            return {"ok": False, "detail": f"proposal {proposal_id} not found"}
        if row["status"] != "proposed":
            return {"ok": False, "detail": f"proposal {proposal_id} is '{row['status']}', not actionable"}

        symbol = row["symbol"]
        side = row["side"]
        notional = _to_float(row["notional"])
        qty = _to_float(row["qty"])

        # Re-derive this symbol's action from FRESH state. If you already traded
        # it (or it filled), the fresh plan no longer calls for it and we refuse
        # — the stale-info guard. Sizing also comes from the FRESH plan, never
        # the possibly-stale stored qty/notional.
        account = await self._broker.account(fresh=True)
        positions = await self._broker.positions(fresh=True)
        open_orders = await self._broker.open_orders(fresh=True)
        swing_pct = float(self._optimizer.latest().get("swing_pct") or 100.0)
        snapshot = self._latest_snapshot() or {}
        posture = self._sizing_posture()
        fresh = build_proposals(
            snapshot, account, positions, self._watchlist_alpaca(),
            self._cfg, self._stop_pct, open_orders=open_orders,
            sleeve_pct=swing_pct, exclude_qty=sleeve_holdings(self._sqlite, "day"),
            posture=posture, sector_max_pct=self._sector_max_pct() if posture else None,
        )
        fp = next((x for x in fresh["proposals"]
                   if norm_symbol(x["symbol"]) == norm_symbol(symbol)), None)
        if fp is None or fp["side"] != side or fp["status"] == "skipped":
            self._sqlite.execute(
                "UPDATE bot_proposals SET status = 'stale', blocks = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                [json.dumps([f"stale — {symbol} already at/over target or thesis changed "
                             "since this proposal; re-propose for a current plan"]), proposal_id],
            )
            await self._broadcast("execute")
            return {"ok": False, "detail": "stale proposal — already satisfied; re-propose",
                    "stale": True}
        if fp["status"] == "blocked":
            self._sqlite.execute(
                "UPDATE bot_proposals SET status = 'blocked', blocks = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                [json.dumps(fp["blocks"]), proposal_id],
            )
            await self._broadcast("execute")
            return {"ok": False, "detail": "blocked on re-validation", "blocks": fp["blocks"]}

        # Adopt the fresh sizing.
        notional = fp.get("notional")
        qty = fp.get("qty")

        # Non-fractionable assets (some bond / income ETFs like XFIV) REJECT
        # notional / fractional orders. Size them in WHOLE shares instead of
        # re-proposing and re-rejecting every cycle.
        if side == "buy" and notional and not await self._broker.fractionable(symbol):
            price = self._latest_price(symbol)
            whole = int(notional // price) if price and price > 0 else 0
            if whole >= 1:
                qty, notional = float(whole), None
            else:
                reason = (f"{symbol} is not fractionable; ${notional:,.0f} is below one share "
                          f"(${price:,.2f})" if price else
                          f"{symbol} is not fractionable and no price is available to size whole shares")
                self._sqlite.execute(
                    "UPDATE bot_proposals SET status = 'blocked', blocks = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    [json.dumps([reason]), proposal_id],
                )
                await self._broadcast("execute")
                return {"ok": False, "detail": reason, "blocks": [reason]}

        # Don't stack a second order on a symbol+side that already has a resting
        # bot order — propose() mints fresh proposal ids each run, so without
        # this a re-propose+execute could double up while the first is unfilled.
        try:
            for o in await self._broker.open_orders(fresh=True):
                cid = o.get("client_order_id") or ""
                if (cid.startswith("bot-")
                        and norm_symbol(o.get("symbol", "")) == norm_symbol(symbol)
                        and (o.get("side") or "").lower() == side):
                    return {"ok": False,
                            "detail": f"a resting bot {side} order for {symbol} already exists "
                                      f"({cid}) — reconcile/cancel it first"}
        except BrokerError as exc:
            return {"ok": False, "detail": f"could not check open orders: {exc.reason}"}

        client_order_id = f"bot-{proposal_id}"
        is_crypto = "/" in symbol
        tif = "gtc" if is_crypto else "day"

        # Record the intent BEFORE submitting (deterministic client_order_id),
        # so even a lost response leaves a row reconcile() can resolve against
        # the broker — no phantom, untracked order.
        # Exactly one of qty / notional is set: sells and non-fractionable buys
        # carry a whole-share qty; ordinary (fractionable) buys carry a dollar
        # notional. Keying off "is qty set?" — not the side — is what lets a
        # non-fractionable BUY (e.g. XFIV, sized to whole shares above) go through.
        use_notional = notional if qty is None else None
        self._sqlite.execute(
            "INSERT OR IGNORE INTO bot_orders (proposal_id, client_order_id, symbol, side, "
            "order_type, qty, notional, status, submitted_at, sleeve) "
            "VALUES (?,?,?,?,?,?,?, 'submitting', datetime('now'), 'swing')",
            [proposal_id, client_order_id, symbol, side, "market", qty, use_notional],
        )
        try:
            order = await self._broker.submit_order(
                symbol, side, qty=qty, notional=use_notional,
                order_type="market", time_in_force=tif,
                client_order_id=client_order_id,
            )
        except BrokerError as exc:
            # status is set for a definite broker reject (4xx); None means the
            # request may have landed (transport error) — leave it reconcilable.
            definite_reject = exc.status is not None and 400 <= exc.status < 500
            self._sqlite.execute(
                "UPDATE bot_orders SET status = ?, error = ? WHERE client_order_id = ?",
                ["rejected" if definite_reject else "unknown", exc.reason, client_order_id],
            )
            self._sqlite.execute(
                "UPDATE bot_proposals SET status = ?, blocks = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                ["rejected" if definite_reject else "submitted",
                 json.dumps([f"broker {'rejected' if definite_reject else 'submit ambiguous'}: {exc.reason}"]),
                 proposal_id],
            )
            await self._broadcast("execute")
            log.warning("bot order %s (%s %s): %s",
                        "rejected" if definite_reject else "ambiguous", side, symbol, exc.reason)
            if definite_reject:
                return {"ok": False, "detail": f"broker rejected: {exc.reason}"}
            return {"ok": False, "detail": f"submit status unknown ({exc.reason}) — run /api/bot/reconcile"}

        self._sqlite.execute(
            "UPDATE bot_orders SET broker_order_id = ?, status = ?, filled_qty = ?, "
            "filled_avg_price = ?, raw = ? WHERE client_order_id = ?",
            [order.get("id"), order.get("status"), _to_float(order.get("filled_qty")),
             _to_float(order.get("filled_avg_price")), json.dumps(order), client_order_id],
        )
        self._sqlite.execute(
            "UPDATE bot_proposals SET status = 'submitted', updated_at = datetime('now') WHERE id = ?",
            [proposal_id],
        )
        await self._broadcast("execute")
        log.info("bot submitted %s %s (proposal %s, broker %s)",
                 side, symbol, proposal_id, order.get("id"))
        return {"ok": True, "proposal_id": proposal_id, "order": order}

    # ---- enforced protective exits ----------------------------------------
    @staticmethod
    def _floor6(qty: float) -> float:
        """Floor to 6dp so a sell request never rounds UP past the held balance."""
        return math.floor(max(0.0, qty) * 1_000_000) / 1_000_000

    def _pos_state(self, norm_sym: str) -> dict | None:
        row = self._sqlite.fetchone(
            "SELECT hwm, trimmed FROM swing_pos_state WHERE symbol = ?", [norm_sym]
        )
        return dict(row) if row is not None else None

    def _set_pos_state(self, norm_sym: str, hwm: float, trimmed: int) -> None:
        self._sqlite.execute(
            "INSERT INTO swing_pos_state (symbol, hwm, trimmed, updated_at) "
            "VALUES (?,?,?, datetime('now')) ON CONFLICT(symbol) DO UPDATE SET "
            "hwm = excluded.hwm, trimmed = excluded.trimmed, updated_at = excluded.updated_at",
            [norm_sym, hwm, trimmed],
        )

    def _clear_pos_state(self, norm_sym: str) -> None:
        self._sqlite.execute("DELETE FROM swing_pos_state WHERE symbol = ?", [norm_sym])

    async def _protective_exits(self, positions: list[dict], account: dict) -> list[dict]:
        """Enforce real protective SELLs on held SWING positions BEFORE rebalancing.

        For each name the sleeve owns (sleeve_qty = broker qty − day-sleeve qty):
          * STOP-LOSS  — price ≤ entry·(1−bucket stop%) → sell all.
          * TRAILING   — in profit and price ≤ HWM·(1−trail%) → sell all.
          * PROFIT-TAKE— up ≥ profit_take% and not yet trimmed → sell a fraction.
          * ROTATION   — RRG lifecycle exit → sell all; trim → sell a fraction.
        Sells never exceed sleeve_qty and at most ONE action runs per symbol per
        run (idempotent). Gated by the panel toggle AND env master AND kill switch.
        """
        cfg = self._config()
        if not (cfg.get("managed_exits") and getattr(self._s, "swing_managed_exits", False)
                and cfg.get("enabled")):
            return []
        exclude = sleeve_holdings(self._sqlite, "day")  # shares the day sleeve owns
        bucket_of: dict[str, str] = {}
        for r in self._sqlite.fetchall(
            "SELECT symbol, bucket FROM bot_proposals WHERE sleeve = 'swing' ORDER BY id ASC"
        ):
            if r["symbol"]:
                bucket_of[norm_symbol(r["symbol"])] = r["bucket"]

        # Posture + RRG once for the rotation-lifecycle leg (best-effort).
        posture = None
        quad_by_etf: dict[str, str] = {}
        name_lifecycle = None
        sector_for = None
        try:
            from app.edge.posture import compute_posture, name_lifecycle as _nl
            from app.edge.rotation import compute_rrg
            from app.trading.context import sector_for as _sf
            name_lifecycle, sector_for = _nl, _sf
            posture = compute_posture(self._duck)
            sectors = list(getattr(self._s, "sector_etfs", []) or [])
            bench = getattr(self._s, "rrg_benchmark", "SPY")
            if sectors:
                for s in (compute_rrg(self._duck, sectors, bench).get("sectors") or []):
                    if s.get("symbol") and s.get("quadrant"):
                        quad_by_etf[s["symbol"]] = s["quadrant"]
        except Exception:
            log.debug("posture/RRG for protective exits failed", exc_info=True)

        monthly_state = ((posture or {}).get("monthly") or {}).get("state", "unknown")
        weekly_state = ((posture or {}).get("weekly") or {}).get("state", "unknown")
        posture_state = (posture or {}).get("state", "neutral")
        trail_pct = float(getattr(self._s, "swing_trail_pct", 8.0))
        pt_pct = float(getattr(self._s, "swing_profit_take_pct", 20.0))
        pt_frac = float(getattr(self._s, "swing_profit_take_frac", 0.25))

        actions: list[dict] = []
        for p in positions:
            sym = p.get("symbol")
            if not sym:
                continue
            n = norm_symbol(sym)
            broker_qty = _to_float(p.get("qty")) or 0.0
            sleeve_qty = self._floor6(max(0.0, broker_qty - exclude.get(n, 0.0)))
            if sleeve_qty <= 0:
                continue
            avg_entry = _to_float(p.get("avg_entry_price"))
            price = _to_float(p.get("current_price"))
            if not avg_entry or not price or price <= 0:
                continue

            # High-water mark: persist max(seen, price) so the trailing stop never
            # ratchets down. trimmed persists across runs until a FULL exit clears it.
            state = self._pos_state(n) or {}
            trimmed = bool(state.get("trimmed"))
            hwm = max(_to_float(state.get("hwm")) or 0.0, price)
            self._set_pos_state(n, hwm, 1 if trimmed else 0)

            bucket = bucket_of.get(n)
            stop_pct = float(self._stop_pct.get(bucket or "", 10.0))

            # Rotation lifecycle (only when posture/RRG are available).
            rot_action, quad = None, None
            if posture and name_lifecycle and sector_for:
                etf = _SECTOR_TO_ETF.get(sector_for(sym))
                quad = quad_by_etf.get(etf) if etf else None
                rot_action = name_lifecycle(quad, monthly_state, weekly_state,
                                            posture_state).get("action")

            qty, reason, full = 0.0, "", False
            if price <= avg_entry * (1 - stop_pct / 100.0):
                qty, reason, full = sleeve_qty, (
                    f"stop-loss: {price:.2f} ≤ entry {avg_entry:.2f} −{stop_pct:.0f}%"), True
            elif price >= avg_entry and price <= hwm * (1 - trail_pct / 100.0):
                qty, reason, full = sleeve_qty, (
                    f"trailing stop: {price:.2f} ≤ HWM {hwm:.2f} −{trail_pct:.0f}%"), True
            elif rot_action == "exit":
                qty, reason, full = sleeve_qty, f"RRG {quad or 'n/a'} — rotate out", True
            elif price >= avg_entry * (1 + pt_pct / 100.0) and not trimmed:
                qty, reason, full = self._floor6(sleeve_qty * pt_frac), (
                    f"profit-take: up ≥ {pt_pct:.0f}%, trim {pt_frac * 100:.0f}%"), False
            elif rot_action == "trim" and not trimmed:
                qty, reason, full = self._floor6(sleeve_qty * pt_frac), (
                    f"RRG {quad or 'n/a'} — trim"), False

            qty = min(self._floor6(qty), sleeve_qty)
            if qty <= 0 or not reason:
                continue
            ok = await self._submit_protective_sell(sym, qty, reason, bucket)
            if ok:
                if full:
                    self._clear_pos_state(n)   # full exit -> reset HWM + trimmed
                else:
                    self._set_pos_state(n, hwm, 1)  # partial -> mark trimmed
            actions.append({"symbol": sym, "qty": qty, "reason": reason,
                            "full_exit": full, "submitted": ok})
        if actions:
            log.info("swing protective exits: %d submitted", sum(1 for a in actions if a["submitted"]))
            await self._broadcast("protective_exits")
        return actions

    async def _submit_protective_sell(self, symbol: str, qty: float, reason: str,
                                      bucket: str | None) -> bool:
        """Plain market SELL for a protective exit, recorded as a swing
        proposal+order (mirrors execute()'s insert-before-submit pattern)."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        pid = self._sqlite.execute_returning_id(
            "INSERT INTO bot_proposals (run_id, created_at, symbol, strategist_sym, bucket, "
            "side, order_type, qty, conviction, max_loss_est, rationale, status, blocks, sleeve) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'swing')",
            [now[:10].replace("-", ""), now, symbol, None, bucket, "sell", "market", qty,
             None, None, json.dumps({"kind": "protective_exit", "reason": reason}),
             "proposed", json.dumps([])],
        )
        cid = f"bot-exit-{pid}"
        is_crypto = "/" in symbol
        tif = "gtc" if is_crypto else "day"
        self._sqlite.execute(
            "INSERT OR IGNORE INTO bot_orders (proposal_id, client_order_id, symbol, side, "
            "order_type, qty, status, submitted_at, sleeve) "
            "VALUES (?,?,?,?,?,?, 'submitting', datetime('now'), 'swing')",
            [pid, cid, symbol, "sell", "market", qty],
        )
        try:
            order = await self._broker.submit_order(
                symbol, "sell", qty=qty, order_type="market", time_in_force=tif,
                client_order_id=cid)
        except BrokerError as exc:
            definite = exc.status is not None and 400 <= exc.status < 500
            self._sqlite.execute(
                "UPDATE bot_orders SET status = ?, error = ? WHERE client_order_id = ?",
                ["rejected" if definite else "unknown", exc.reason, cid])
            self._sqlite.execute(
                "UPDATE bot_proposals SET status = 'rejected', blocks = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                [json.dumps([f"protective exit {exc.reason}"]), pid])
            log.warning("swing protective sell %s rejected: %s", symbol, exc.reason)
            return False
        self._sqlite.execute(
            "UPDATE bot_orders SET broker_order_id = ?, status = ?, filled_qty = ?, "
            "filled_avg_price = ?, raw = ? WHERE client_order_id = ?",
            [order.get("id"), order.get("status"), _to_float(order.get("filled_qty")),
             _to_float(order.get("filled_avg_price")), json.dumps(order), cid])
        self._sqlite.execute(
            "UPDATE bot_proposals SET status = 'submitted', updated_at = datetime('now') WHERE id = ?",
            [pid])
        log.info("swing protective SELL %s qty %s (%s)", symbol, qty, reason)
        return True

    # ---- run (auto, paper-only) -------------------------------------------
    async def run(self) -> dict:
        """Propose, then auto-execute every actionable proposal IFF mode='auto'
        and the kill switch is on. Otherwise behaves exactly like propose().

        Enforced protective exits run FIRST (independent of rebalancing) — gated
        by the managed-exits toggle + env master + kill switch."""
        exits: list[dict] = []
        me_on = (self._config().get("managed_exits")
                 and getattr(self._s, "swing_managed_exits", False)
                 and self._config().get("enabled"))
        if me_on and self._broker.enabled:
            try:
                ex_account = await self._broker.account(fresh=True)
                ex_positions = await self._broker.positions(fresh=True)
                exits = await self._protective_exits(ex_positions, ex_account)
            except BrokerError as exc:
                log.warning("protective exits skipped: %s", exc.reason)
        result = await self.propose()
        result["protective_exits"] = exits
        if not result.get("ok"):
            return result
        cfg_state = self._config()
        if cfg_state["mode"] != "auto" or not cfg_state["enabled"]:
            result["auto_executed"] = 0
            result["note"] = ("proposals only — set mode=auto and enable the kill "
                              "switch to auto-execute (paper)")
            return result
        executed = []
        for p in result["proposals"]:
            if p.get("status") == "proposed" and p.get("id") is not None:
                res = await self.execute(int(p["id"]))
                executed.append({"proposal_id": p["id"], "ok": res.get("ok"),
                                 "detail": res.get("detail")})
        await self.reconcile()
        result["auto_executed"] = sum(1 for e in executed if e["ok"])
        result["executions"] = executed
        return result

    # ---- reconcile (broker is truth) --------------------------------------
    async def reconcile(self) -> dict:
        """Overwrite local order + proposal state from the broker's orders.
        Never trust our own optimistic write — the broker is the ledger."""
        if not self._broker.enabled:
            return {"ok": False, "detail": "Alpaca paper keys not configured"}
        orders = await self._broker.list_orders("all", 200)
        by_cid = {o.get("client_order_id"): o for o in orders if o.get("client_order_id")}
        updated = 0
        rows = self._sqlite.fetchall(
            "SELECT id, proposal_id, client_order_id FROM bot_orders WHERE sleeve = 'swing'"
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
                if status in ("canceled", "expired") and filled > 0:
                    # Partially filled, then terminated: it DID trade — don't
                    # mislabel it 'canceled' and hide the fill.
                    pstatus = "filled"
                else:
                    pstatus = {
                        "filled": "filled", "partially_filled": "submitted",
                        "canceled": "canceled", "expired": "canceled",
                        "rejected": "rejected",
                    }.get(status, "submitted")
                self._sqlite.execute(
                    "UPDATE bot_proposals SET status = ?, updated_at = datetime('now') "
                    "WHERE id = ? AND status IN ('submitted','rejected','canceled','filled')",
                    [pstatus, r["proposal_id"]],
                )
            updated += 1
        return {"ok": True, "reconciled": updated}

    # ---- status ------------------------------------------------------------
    async def status(self) -> dict:
        cfg_state = self._config()
        out: dict = {
            "config": cfg_state,
            "broker": {
                "enabled": self._broker.enabled,
                "is_paper": self._broker.is_paper,
                "base_url": self._broker.base_url,
            },
            "guardrails": {
                "max_position_pct": self._cfg.max_position_pct,
                "max_position_notional": self._cfg.max_position_notional,
                "min_order_notional": self._cfg.min_order_notional,
                "daily_loss_limit_pct": self._cfg.daily_loss_limit_pct,
                "rebalance_band_pp": self._cfg.rebalance_band_pp,
                "allow_live": self._cfg.allow_live,
            },
            "sleeve": "swing",
            "optimizer": self._optimizer.latest(),
            "proposals": self._open_proposals(),
            "recent_orders": self._recent_orders(),
            "disclaimer": DISCLAIMER,
        }
        if self._broker.enabled:
            try:
                # Only hit the broker's order list when something is actually in
                # flight — keeps a wall of UI polls from spending API budget.
                if self._has_inflight("swing"):
                    await self.reconcile()
                account = await self._broker.account()
                positions = await self._broker.positions()
                out["account"] = self._account_summary(account)
                out["positions"] = [
                    {"symbol": p.get("symbol"),
                     "qty": _to_float(p.get("qty")),
                     "market_value": _to_float(p.get("market_value")),
                     "avg_entry_price": _to_float(p.get("avg_entry_price")),
                     "unrealized_pl": _to_float(p.get("unrealized_pl")),
                     "unrealized_plpc": _to_float(p.get("unrealized_plpc"))}
                    for p in positions
                ]
                # Flag open proposals that fresh state no longer calls for, so
                # the UI can grey out trades you've already effectively done.
                try:
                    open_orders = await self._broker.open_orders()
                    snap = self._latest_snapshot() or {}
                    swing_pct = float(self._optimizer.latest().get("swing_pct") or 100.0)
                    posture = self._sizing_posture()
                    fresh = build_proposals(
                        snap, account, positions, self._watchlist_alpaca(), self._cfg,
                        self._stop_pct, open_orders=open_orders, sleeve_pct=swing_pct,
                        exclude_qty=sleeve_holdings(self._sqlite, "day"),
                        posture=posture,
                        sector_max_pct=self._sector_max_pct() if posture else None,
                    )
                    live = {(norm_symbol(x["symbol"]), x["side"])
                            for x in fresh["proposals"] if x["status"] == "proposed"}
                    for p in out["proposals"]:
                        if p.get("status") == "proposed":
                            p["stale"] = (norm_symbol(p["symbol"]), p["side"]) not in live
                except Exception:
                    log.debug("staleness flagging failed", exc_info=True)
            except BrokerError as exc:
                out["account_error"] = exc.reason
        return out

    async def portfolio(self) -> dict:
        """Aggregated, attribution-aware portfolio overview for the big panel.

        Pulls the broker account + positions (GROUND TRUTH), splits each position
        across the two sleeves (day vs swing, from each sleeve's filled book) plus
        any 'manual' remainder, flags winners, and attaches an exit plan per name.
        """
        out: dict = {
            "ok": False,
            "broker": {
                "enabled": self._broker.enabled,
                "is_paper": self._broker.is_paper,
                "base_url": self._broker.base_url,
            },
            "optimizer": self._optimizer.latest(),
            "disclaimer": DISCLAIMER,
        }
        if not self._broker.enabled:
            out["detail"] = "Alpaca paper keys not configured"
            return out

        try:
            # In-flight orders on either sleeve -> sync first so attribution is fresh.
            if self._has_inflight("swing") or self._has_inflight("day"):
                await self.reconcile()
            account = await self._broker.account()
            positions = await self._broker.positions()
        except BrokerError as exc:
            out["detail"] = exc.reason
            out["status"] = exc.status
            return out

        day_book = sleeve_holdings(self._sqlite, "day")
        swing_book = sleeve_holdings(self._sqlite, "swing")
        # Which sleeve has EVER traded each symbol (any real, non-synthetic order)
        # and how much. Used to attribute a broker-vs-ledger residual to the bot
        # that actually trades the name instead of dumping it in "manual": the
        # local ledger lags the broker (a just-filled bracket entry sits 'new'
        # until reconcile; old heals can over-count a sell), so a name the day bot
        # is actively trading would otherwise flash as a phantom "manual" holding.
        traded = self._traded_by_sleeve()
        # Latest swing proposal per symbol -> bucket / target / invalidation.
        swing_props: dict[str, dict] = {}
        for r in self._sqlite.fetchall(
            "SELECT symbol, bucket, target_value, target_pct, rationale "
            "FROM bot_proposals WHERE sleeve = 'swing' ORDER BY id ASC"
        ):
            swing_props[norm_symbol(r["symbol"])] = dict(r)

        SLEEVE_LABEL = {"day": "Day trader", "swing": "Swing bot", "manual": "Manual / other"}
        sleeves = {k: {"label": v, "value": 0.0, "unrealized_pl": 0.0, "n": 0}
                   for k, v in SLEEVE_LABEL.items()}
        enriched: list[dict] = []
        total_unrealized = 0.0

        for p in positions:
            sym = p.get("symbol") or ""
            n = norm_symbol(sym)
            qty = _to_float(p.get("qty")) or 0.0
            mv = _to_float(p.get("market_value"))
            upl = _to_float(p.get("unrealized_pl"))
            avg_entry = _to_float(p.get("avg_entry_price"))
            day_qty = max(0.0, day_book.get(n, 0.0))
            swing_qty = max(0.0, swing_book.get(n, 0.0))
            # Remainder beyond what either bot's filled book accounts for.
            residual = max(0.0, qty - day_qty - swing_qty)
            # Attribute that residual to whichever bot actually trades this name
            # (ledger lag / heal over-counts, not a real manual buy). Only a name
            # NEITHER bot has ever traded stays "manual / other".
            day_vol = traded.get(("day", n), 0.0)
            swing_vol = traded.get(("swing", n), 0.0)
            if residual > 1e-9 and (day_vol or swing_vol):
                if day_vol >= swing_vol:
                    day_qty += residual
                else:
                    swing_qty += residual
                manual_qty = 0.0
            else:
                manual_qty = residual
            # Dominant sleeve = whoever owns the most shares; 'mixed' only labels
            # the primary, the per-sleeve qty split is reported alongside.
            owners = {"day": day_qty, "swing": swing_qty, "manual": manual_qty}
            primary = max(owners, key=owners.get) if qty > 0 else "manual"

            if upl is not None:
                total_unrealized += upl
            # Split market value / P&L across owners by share weight for the rollup.
            if qty > 0:
                for key, oqty in owners.items():
                    if oqty <= 0:
                        continue
                    w = oqty / qty
                    sleeves[key]["value"] += (mv or 0.0) * w
                    sleeves[key]["unrealized_pl"] += (upl or 0.0) * w
                sleeves[primary]["n"] += 1

            exit_sleeve = primary if primary in ("day", "swing") else (
                "day" if day_qty > 0 else "swing" if swing_qty > 0 else "manual")
            exit_plan = (
                _exit_plan(sym, exit_sleeve, avg_entry, swing_props.get(n), self._stop_pct)
                if exit_sleeve in ("day", "swing") else None
            )

            enriched.append({
                "symbol": sym,
                "qty": qty,
                "market_value": mv,
                "avg_entry_price": avg_entry,
                "unrealized_pl": upl,
                "unrealized_plpc": _to_float(p.get("unrealized_plpc")),
                "sleeve": primary,
                "sleeve_label": SLEEVE_LABEL[primary],
                "day_qty": round(day_qty, 6),
                "swing_qty": round(swing_qty, 6),
                "manual_qty": round(manual_qty, 6),
                "winning": (upl or 0.0) > 0,
                "exit": exit_plan,
            })

        enriched.sort(key=lambda x: (x["unrealized_pl"] or 0.0), reverse=True)
        winners = [x for x in enriched if x["winning"]]
        losers = [x for x in enriched if (x["unrealized_pl"] or 0.0) < 0]
        for s in sleeves.values():
            s["value"] = round(s["value"], 2)
            s["unrealized_pl"] = round(s["unrealized_pl"], 2)

        summary = self._account_summary(account)
        equity = summary.get("equity") or 0.0
        out.update({
            "ok": True,
            "total_value": equity,  # paper account equity = the big number
            "equity": equity,
            "last_equity": summary.get("last_equity"),
            "cash": summary.get("cash"),
            "buying_power": summary.get("buying_power"),
            "day_pnl_pct": summary.get("day_pnl_pct"),
            "unrealized_pl": round(total_unrealized, 2),
            "unrealized_pl_pct": round(total_unrealized / (equity - total_unrealized) * 100.0, 2)
            if (equity - total_unrealized) else None,
            "n_positions": len(enriched),
            "n_winners": len(winners),
            "n_losers": len(losers),
            "sleeves": sleeves,
            "positions": enriched,
            "winners": winners,
            "account": summary,
        })
        return out

    def _has_inflight(self, sleeve: str) -> bool:
        row = self._sqlite.fetchone(
            "SELECT 1 FROM bot_orders WHERE sleeve = ? AND status IN "
            "('submitting','new','accepted','pending_new','partially_filled',"
            "'pending_replace','accepted_for_bidding') LIMIT 1",
            [sleeve],
        )
        return row is not None

    # ---- helpers -----------------------------------------------------------
    def _allowlist(self) -> set[str]:
        snap = self._latest_snapshot() or {}
        out = set(self._watchlist_alpaca())
        for bucket in snap.get("buckets", []) or []:
            asset_class = BUCKET_ASSET_CLASS.get(bucket.get("key"), "equity")
            for h in bucket.get("holdings", []) or []:
                mapped = alpaca_symbol(h.get("symbol", ""), asset_class)
                if mapped is not None:
                    out.add(mapped[0])
        return out

    @staticmethod
    def _account_summary(account: dict) -> dict:
        from app.trading.guardrails import daily_pnl_pct
        return {
            "equity": _to_float(account.get("equity")),
            "last_equity": _to_float(account.get("last_equity")),
            "cash": _to_float(account.get("cash")),
            "buying_power": _to_float(account.get("buying_power")),
            "day_pnl_pct": daily_pnl_pct(account),
            "daytrade_count": account.get("daytrade_count"),
            "pattern_day_trader": account.get("pattern_day_trader"),
            "status": account.get("status"),
            "trading_blocked": account.get("trading_blocked"),
        }

    def _open_proposals(self) -> list[dict]:
        rows = self._sqlite.fetchall(
            "SELECT * FROM bot_proposals WHERE sleeve = 'swing' AND "
            "status IN ('proposed','blocked','submitted','rejected') "
            "ORDER BY CASE status WHEN 'proposed' THEN 0 WHEN 'submitted' THEN 1 "
            "WHEN 'blocked' THEN 2 ELSE 3 END, abs(delta_value) DESC LIMIT 100"
        )
        return [self._proposal_row(r) for r in rows]

    def _recent_orders(self) -> list[dict]:
        rows = self._sqlite.fetchall(
            "SELECT id, proposal_id, symbol, side, order_type, qty, notional, status, "
            "filled_qty, filled_avg_price, submitted_at, error FROM bot_orders "
            "WHERE sleeve = 'swing' ORDER BY id DESC LIMIT 50"
        )
        return [dict(r) for r in rows]

    @staticmethod
    def _proposal_row(r) -> dict:
        d = dict(r)
        for k in ("rationale", "blocks"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except (ValueError, TypeError):
                    pass
        return d

    async def _broadcast(self, event: str) -> None:
        try:
            await self._hub.broadcast(BOT_TOPIC, {"type": "bot", "event": event})
        except Exception:  # broadcasting must never break a trade path
            log.debug("bot broadcast failed", exc_info=True)


def proposals_for_run(sqlite: SqliteStore, run_id: str) -> list[dict]:
    rows = sqlite.fetchall(
        "SELECT * FROM bot_proposals WHERE run_id = ? ORDER BY abs(delta_value) DESC",
        [run_id],
    )
    return [TradingBotService._proposal_row(r) for r in rows]


def sleeve_holdings(sqlite: SqliteStore, sleeve: str) -> dict[str, float]:
    """Net shares a sleeve owns, from its FILLED orders (buy +, sell -). The one
    shared paper account is partitioned this way so each sleeve diffs against
    only its own book and the two bots never trade each other's positions."""
    out: dict[str, float] = {}
    for r in sqlite.fetchall(
        "SELECT symbol, side, filled_qty FROM bot_orders "
        "WHERE sleeve = ? AND status = 'filled' AND filled_qty IS NOT NULL",
        [sleeve],
    ):
        q = _to_float(r["filled_qty"]) or 0.0
        if not q:
            continue
        n = norm_symbol(r["symbol"])
        out[n] = out.get(n, 0.0) + (q if r["side"] == "buy" else -q)
    return {k: v for k, v in out.items() if abs(v) > 1e-9}
