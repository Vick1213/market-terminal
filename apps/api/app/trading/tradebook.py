"""Trade-level (paired entry/exit) view over the raw bot order log.

The bot records raw fills in ``bot_orders`` (one row per Alpaca order). A *trade*
is a round-trip: a FILLED buy (entry) paired against a subsequent FILLED sell
(exit) of the same symbol+sleeve, FIFO. This generalizes the day-sleeve P&L
pairing in ``day_review._attribute_one`` to both sleeves and to lot-level
splitting (a 10-share buy can be closed by two 5-share sells, and vice-versa).

``list_trades`` returns one dict per closed round-trip plus one per still-open
lot. Open lots are marked to the latest known price (DuckDB ``ts_price``) when a
``duck`` handle is supplied, else ``pnl``/``pnl_pct`` are ``None``. The function
is defensive: a single malformed order row is skipped, never fatal.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone

from app.trading.guardrails import norm_symbol

log = logging.getLogger("market.trading.tradebook")

# A lot whose remaining qty falls below this is "dust" — a float-precision
# residual left when the broker's recorded sell qty is a rounded-down copy of the
# buy qty (e.g. buy 0.418113347 closed by sell 0.418113). Drop it so it never
# surfaces as a phantom ~0 open position. Smaller than any economically real
# fractional-share / crypto lot the bot would ever take.
_EPS = 1e-6


def _reason_from_rationale(raw) -> str | None:
    """Pull a short human "why" out of a bot_proposals.rationale JSON blob.

    Day sleeve: ``reason`` (e.g. "momentum buy (breakout +0.4% …)"), else a
    ``signal`` kind+detail, else ``kind``. Swing sleeve: the first ``evidence``
    bullet(s), else ``bear_case``. Never raises."""
    if not raw:
        return None
    try:
        r = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(r, dict):
        return None
    # Day-sleeve hedged-bracket rationale.
    if r.get("reason"):
        return str(r["reason"])[:200]
    sig = r.get("signal")
    if isinstance(sig, dict):
        s = " ".join(str(p) for p in (sig.get("kind"), sig.get("detail")) if p)
        if s:
            return s[:200]
    # Swing-sleeve strategist rationale.
    ev = r.get("evidence")
    if isinstance(ev, list) and ev:
        return "; ".join(str(e) for e in ev[:2])[:200]
    if r.get("kind"):
        return str(r["kind"])[:200]
    if r.get("bear_case"):
        return str(r["bear_case"])[:200]
    return None


def _is_synthetic(order_type, client_order_id) -> bool:
    """True for the ledger-adjustment rows a broker resync inserts into
    bot_orders (cap/flatten/heal). These are NOT real trades: they exist only to
    reconcile our local ledger down to the broker's actual positions. We let them
    *flatten* open lots (so positions match broker truth) but never emit them as
    closed trades — that would invent phantom round-trips with bogus P&L."""
    if (order_type or "").lower() == "reconcile":
        return True
    coid = (client_order_id or "").lower()
    return coid.startswith(("reconcile", "heal", "flatday"))


def _reason_map(sqlite) -> dict[int, str]:
    """proposal_id -> short reason string, for every proposal with a rationale."""
    out: dict[int, str] = {}
    try:
        rows = sqlite.fetchall(
            "SELECT id, rationale FROM bot_proposals WHERE rationale IS NOT NULL"
        )
    except Exception:
        return out
    for r in rows:
        try:
            reason = _reason_from_rationale(r["rationale"])
            if reason:
                out[int(r["id"])] = reason
        except Exception:
            continue
    return out


def _to_float(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_dt(s) -> datetime | None:
    """Best-effort ISO-8601 parse of a stored ``submitted_at`` string."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    txt = str(s).strip()
    if not txt:
        return None
    # Normalize a trailing Z and space-separated date/time.
    txt = txt.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(txt)
    except ValueError:
        # Fall back to the date-time prefix (drops fractional/tz junk).
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(txt[: len(fmt) + 6], fmt)
            except ValueError:
                continue
    return None


def _hold_minutes(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    try:
        # Make both tz-aware (assume UTC for naive) so subtraction never raises.
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        delta = (end - start).total_seconds() / 60.0
        return round(delta, 2)
    except Exception:
        return None


def _latest_prices(duck, symbols: set[str]) -> dict[str, float]:
    """Latest close per raw symbol from ts_price, keyed by norm_symbol. Best
    effort — any failure yields an empty/partial map, never an exception."""
    out: dict[str, float] = {}
    if duck is None or not symbols:
        return out
    for sym in symbols:
        try:
            row = duck.fetchone(
                "SELECT close FROM ts_price WHERE symbol = ? AND close IS NOT NULL "
                "ORDER BY ts DESC LIMIT 1",
                [sym],
            )
            if row and row[0] is not None:
                out[norm_symbol(sym)] = float(row[0])
        except Exception:
            log.debug("latest price lookup failed for %s", sym, exc_info=True)
    return out


def list_trades(sqlite, *, sleeve: str | None = None, status: str | None = None,
                duck=None) -> list[dict]:
    """Pair FILLED buys (entries) with subsequent FILLED sells (exits) per
    symbol+sleeve, FIFO, and return trade dicts.

    Each trade::

        { sleeve, symbol, qty, entry_price, entry_time, exit_price, exit_time,
          status: "open"|"closed", pnl, pnl_pct, hold_minutes }

    ``sleeve`` filters to one of 'swing' | 'day'. ``status`` filters the result
    to "open" or "closed". ``duck`` (optional) marks open lots to last price.
    """
    params: list = []
    where = ["status = 'filled'", "filled_qty IS NOT NULL"]
    if sleeve:
        where.append("sleeve = ?")
        params.append(sleeve)
    sql = (
        "SELECT sleeve, symbol, side, filled_qty, filled_avg_price, submitted_at, "
        "proposal_id, order_type, client_order_id, id FROM bot_orders WHERE "
        + " AND ".join(where) + " ORDER BY submitted_at ASC, id ASC"
    )
    try:
        rows = sqlite.fetchall(sql, params)
    except Exception:
        log.warning("tradebook query failed", exc_info=True)
        return []

    reasons = _reason_map(sqlite)

    # Buckets per (sleeve, normalized-symbol). Each bucket keeps a FIFO queue of
    # open buy lots; sells consume them oldest-first, splitting on quantity.
    open_lots: dict[tuple[str, str], deque[dict]] = defaultdict(deque)
    trades: list[dict] = []
    raw_symbols: set[str] = set()

    for r in rows:
        try:
            slv = (r["sleeve"] or "swing")
            raw_sym = r["symbol"] or ""
            nsym = norm_symbol(raw_sym)
            side = (r["side"] or "").lower()
            qty = _to_float(r["filled_qty"]) or 0.0
            price = _to_float(r["filled_avg_price"])
            ts = r["submitted_at"]
            if qty <= 0:
                continue
            synthetic = _is_synthetic(r["order_type"], r["client_order_id"])
            key = (slv, nsym)
            lots = open_lots[key]

            if side == "sell" and synthetic:
                # Ledger-adjustment exit: consume open lots FIFO so the position
                # reconciles to broker truth, but emit NO closed trade (no price,
                # no real round-trip → no phantom P&L). Price is irrelevant here.
                remaining = qty
                while remaining > _EPS and lots:
                    lot = lots[0]
                    matched = min(remaining, lot["qty"])
                    lot["qty"] -= matched
                    remaining -= matched
                    if lot["qty"] <= _EPS:
                        lots.popleft()
                continue

            # Synthetic *buys* are not real entries — never seed a lot from them.
            if synthetic:
                continue
            if price is None:
                continue

            if side == "buy":
                raw_symbols.add(raw_sym)
                pid = r["proposal_id"]
                reason = reasons.get(int(pid)) if pid is not None else None
                lots.append({
                    "sleeve": slv, "symbol": raw_sym, "qty": qty,
                    "entry_price": price, "entry_time": ts, "reason": reason,
                })
            elif side == "sell":
                remaining = qty
                while remaining > _EPS and lots:
                    lot = lots[0]
                    matched = min(remaining, lot["qty"])
                    trades.append(_closed_trade(lot, price, ts, matched))
                    lot["qty"] -= matched
                    remaining -= matched
                    if lot["qty"] <= _EPS:
                        lots.popleft()
                # A leftover real sell with no matching buy (short / out-of-band
                # exit) is intentionally dropped — we only model long round-trips.
        except Exception:
            log.debug("tradebook: skipping bad order row", exc_info=True)
            continue

    # Whatever buy quantity is still unmatched is an open position.
    marks = _latest_prices(duck, raw_symbols)
    now = datetime.now(timezone.utc)
    for (slv, nsym), lots in open_lots.items():
        for lot in lots:
            if lot["qty"] <= _EPS:
                continue
            trades.append(_open_trade(lot, marks.get(nsym), now))

    if status in ("open", "closed"):
        trades = [t for t in trades if t["status"] == status]

    # Newest first by the trade's most recent timestamp.
    def _sort_key(t: dict):
        dt = _parse_dt(t.get("exit_time")) or _parse_dt(t.get("entry_time"))
        return dt or datetime.min.replace(tzinfo=timezone.utc)

    trades.sort(key=_sort_key, reverse=True)
    return trades


def _closed_trade(lot: dict, exit_price: float, exit_time, qty: float) -> dict:
    ep = lot["entry_price"]
    pnl = round((exit_price - ep) * qty, 4)
    pnl_pct = round((exit_price / ep - 1.0) * 100, 4) if ep else None
    return {
        "sleeve": lot["sleeve"],
        "symbol": lot["symbol"],
        "qty": round(qty, 8),
        "entry_price": ep,
        "entry_time": lot["entry_time"],
        "exit_price": exit_price,
        "exit_time": exit_time,
        "status": "closed",
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "hold_minutes": _hold_minutes(_parse_dt(lot["entry_time"]), _parse_dt(exit_time)),
        "reason": lot.get("reason"),
    }


def _open_trade(lot: dict, mark: float | None, now: datetime) -> dict:
    ep = lot["entry_price"]
    qty = lot["qty"]
    pnl = round((mark - ep) * qty, 4) if mark is not None else None
    pnl_pct = round((mark / ep - 1.0) * 100, 4) if (mark is not None and ep) else None
    return {
        "sleeve": lot["sleeve"],
        "symbol": lot["symbol"],
        "qty": round(qty, 8),
        "entry_price": ep,
        "entry_time": lot["entry_time"],
        "exit_price": None,
        "exit_time": None,
        "status": "open",
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "hold_minutes": _hold_minutes(_parse_dt(lot["entry_time"]), now),
        "reason": lot.get("reason"),
    }
