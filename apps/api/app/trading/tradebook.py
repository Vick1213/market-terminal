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
    # Every ledger-adjustment / manual-flatten prefix the cleanup + resync scripts
    # use: reconcile/heal (broker resync), and flat*/manual* (one-off position
    # flattens done directly on Alpaca, e.g. "flatall-QCOM", "flat2-heal-…",
    # "flatday-…", "manual-flatten-sh"). These close lots to match broker truth
    # but must NOT surface as real round-trips with invented P&L.
    return coid.startswith(("reconcile", "heal", "flat", "manual"))


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


def _bracket_from_rationale(raw) -> dict | None:
    """Pull the protective-exit levels (stop-loss / take-profit / trailing) the
    day bot computed for an ENTRY, out of a bot_proposals.rationale blob. The day
    sleeve stores them per-leg under ``legs`` (the primary buy leg carries
    sl_price/tp_price/trail_price/rr/exit_type); swing entries have none. Returns
    a flat dict of the present levels, or None. Never raises."""
    if not raw:
        return None
    try:
        r = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(r, dict):
        return None
    legs = r.get("legs")
    leg = None
    if isinstance(legs, list):
        # The primary (entry) leg carries the brackets; fall back to the first.
        leg = next((l for l in legs if isinstance(l, dict) and l.get("role") == "primary"), None)
        if leg is None:
            leg = next((l for l in legs if isinstance(l, dict)), None)
    src = leg if isinstance(leg, dict) else r  # tolerate a flat (non-legged) shape
    out = {
        "sl_price": _to_float(src.get("sl_price")),
        "tp_price": _to_float(src.get("tp_price")),
        "trail_price": _to_float(src.get("trail_price")),
        "rr": _to_float(src.get("rr")),
        "exit_type": src.get("exit_type"),
    }
    if any(out[k] is not None for k in ("sl_price", "tp_price", "trail_price")):
        return out
    return None


def _bracket_map(sqlite) -> dict[int, dict]:
    """proposal_id -> {sl_price, tp_price, trail_price, rr, exit_type} for every
    entry proposal that recorded protective levels."""
    out: dict[int, dict] = {}
    try:
        rows = sqlite.fetchall(
            "SELECT id, rationale FROM bot_proposals WHERE rationale IS NOT NULL"
        )
    except Exception:
        return out
    for r in rows:
        try:
            br = _bracket_from_rationale(r["rationale"])
            if br:
                out[int(r["id"])] = br
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
                duck=None, marks: dict[str, float] | None = None) -> list[dict]:
    """Pair FILLED buys (entries) with subsequent FILLED sells (exits) per
    symbol+sleeve, FIFO, and return trade dicts.

    Each trade::

        { sleeve, symbol, qty, entry_price, entry_time, exit_price, exit_time,
          status: "open"|"closed", pnl, pnl_pct, hold_minutes }

    ``sleeve`` filters to one of 'swing' | 'day'. ``status`` filters the result
    to "open" or "closed". Open lots are marked to a current price: the live
    broker price in ``marks`` (norm_symbol -> price) when supplied takes
    precedence; ``duck``'s ts_price close is the fallback. The broker price is the
    accurate intraday mark — ts_price is a daily close and would show a stale,
    sometimes wildly wrong, open P&L (e.g. yesterday's close on an intraday name).
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
    brackets = _bracket_map(sqlite)

    # Buckets per (sleeve, normalized-symbol). Each bucket holds a FIFO queue of
    # open lots that are ALL the same direction (you're net long OR net short, not
    # both). A same-side order opens/adds a lot; an opposite-side order closes them
    # oldest-first (direction-aware P&L), and any leftover FLIPS the position by
    # opening a lot the other way. This models LONG (buy→sell) and SHORT (sell→buy)
    # round-trips uniformly.
    open_lots: dict[tuple[str, str], deque[dict]] = defaultdict(deque)
    trades: list[dict] = []
    raw_symbols: set[str] = set()

    def _seed(lots, *, direction, slv, raw_sym, qty, price, ts, pid):
        raw_symbols.add(raw_sym)
        lots.append({
            "sleeve": slv, "symbol": raw_sym, "direction": direction, "qty": qty,
            "entry_price": price, "entry_time": ts,
            "reason": reasons.get(int(pid)) if pid is not None else None,
            "bracket": brackets.get(int(pid)) if pid is not None else None,
        })

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
            lots = open_lots[(slv, nsym)]
            cur_dir = lots[0]["direction"] if lots else None
            open_dir = "long" if side == "buy" else "short"  # which a fresh lot would be

            # CLOSE path: order is opposite the open lots → consume them FIFO.
            if cur_dir is not None and cur_dir != open_dir:
                remaining = qty
                while remaining > _EPS and lots:
                    lot = lots[0]
                    matched = min(remaining, lot["qty"])
                    # Synthetic (reconcile/heal/flatten) exits consume to reconcile
                    # to broker truth but emit NO trade (no real round-trip P&L).
                    if not synthetic and price is not None:
                        trades.append(_closed_trade(lot, price, ts, matched))
                    lot["qty"] -= matched
                    remaining -= matched
                    if lot["qty"] <= _EPS:
                        lots.popleft()
                # Leftover after fully closing → a real FLIP opens the other way.
                if remaining > _EPS and not synthetic and price is not None:
                    _seed(lots, direction=open_dir, slv=slv, raw_sym=raw_sym,
                          qty=remaining, price=price, ts=ts, pid=r["proposal_id"])
                continue

            # OPEN path: same direction (or flat). Synthetic opens aren't real
            # entries (a heal/flatten buy/sell) → never seed a lot from them.
            if synthetic or price is None:
                continue
            _seed(lots, direction=open_dir, slv=slv, raw_sym=raw_sym,
                  qty=qty, price=price, ts=ts, pid=r["proposal_id"])
        except Exception:
            log.debug("tradebook: skipping bad order row", exc_info=True)
            continue

    # Whatever buy quantity is still unmatched is an open position. Mark it to a
    # current price: ts_price close as the base, then OVERRIDE with the live
    # broker price where we have it (accurate intraday mark).
    mark_map = _latest_prices(duck, raw_symbols)
    if marks:
        mark_map.update({norm_symbol(k): v for k, v in marks.items() if v is not None})
    now = datetime.now(timezone.utc)
    for (slv, nsym), lots in open_lots.items():
        for lot in lots:
            if lot["qty"] <= _EPS:
                continue
            trades.append(_open_trade(lot, mark_map.get(nsym), now))

    if status in ("open", "closed"):
        trades = [t for t in trades if t["status"] == status]

    # Newest first by the trade's most recent timestamp.
    def _sort_key(t: dict):
        dt = _parse_dt(t.get("exit_time")) or _parse_dt(t.get("entry_time"))
        return dt or datetime.min.replace(tzinfo=timezone.utc)

    trades.sort(key=_sort_key, reverse=True)
    return trades


def _bracket_fields(lot: dict) -> dict:
    """The entry's planned protective levels, flattened onto the trade dict
    (all None when the entry had no recorded bracket — e.g. swing trades)."""
    br = lot.get("bracket") or {}
    return {
        "sl_price": br.get("sl_price"),
        "tp_price": br.get("tp_price"),
        "trail_price": br.get("trail_price"),
        "rr": br.get("rr"),
        "exit_type": br.get("exit_type"),
    }


def _dir_pnl(direction: str, entry: float, exit_: float, qty: float):
    """Direction-aware P&L: a long gains when price rises, a short when it falls."""
    sign = -1.0 if direction == "short" else 1.0
    pnl = round(sign * (exit_ - entry) * qty, 4)
    pnl_pct = round(sign * (exit_ / entry - 1.0) * 100, 4) if entry else None
    return pnl, pnl_pct


def _closed_trade(lot: dict, exit_price: float, exit_time, qty: float) -> dict:
    ep = lot["entry_price"]
    direction = lot.get("direction", "long")
    pnl, pnl_pct = _dir_pnl(direction, ep, exit_price, qty)
    return {
        "sleeve": lot["sleeve"],
        "symbol": lot["symbol"],
        "direction": direction,
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
        **_bracket_fields(lot),
    }


def _open_trade(lot: dict, mark: float | None, now: datetime) -> dict:
    ep = lot["entry_price"]
    qty = lot["qty"]
    direction = lot.get("direction", "long")
    pnl, pnl_pct = (_dir_pnl(direction, ep, mark, qty) if mark is not None else (None, None))
    return {
        "sleeve": lot["sleeve"],
        "symbol": lot["symbol"],
        "direction": direction,
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
        **_bracket_fields(lot),
    }
