"""Phase 3 learning loop — end-of-day review of the day-trader's decisions.

Every signal the fast loop sees is journaled into ``day_signal_journal`` (acted,
skipped, blocked, or hedge) with the full market context it decided against.
``review_day`` mines one trading session of that journal:

  1. Backfills realized/marked P&L onto every ``acted`` row by pairing its entry
     fill (``bot_orders``, sleeve='day') with the next sell of the same symbol
     (FIFO), or marking it to the last seen price while still open.
  2. Computes per-setup / per-regime / per-symbol win-rates + average P&L, the
     winner-vs-loser conviction gap, the most common skip/block reasons, and the
     net-beta hedge rebalance count (a churn proxy).
  3. Derives systematic-mistake *findings* and concrete, bounded *suggestions*
     that reference the real ``day_*`` config knobs.
  4. Writes a short narrative (LLM if one is wired, deterministic template
     otherwise — it never hard-depends on an LLM) and persists one row to
     ``day_review`` (INSERT OR REPLACE).

The compute path is blocking (SQLite reads + a little math); callers dispatch it
via ``run_in_executor`` so the event loop is never blocked. The optional LLM
call is run synchronously from inside that worker thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo always present on 3.9+
    _ET = None

from app.trading.guardrails import norm_symbol
from app.trading import knobs

# How many trailing SESSIONS the finding/suggestion engine aggregates over. A
# single intraday session is too small to tune on; pooling the last N sessions'
# journal rows turns per-setup/per-symbol stats into a real sample (the "learn
# from history, not just today" fix). The daily narrative still describes today.
_REVIEW_WINDOW_SESSIONS = 20

log = logging.getLogger("market.trading.day_review")

# Sample-size tiers. Tiny intraday samples are noise, and a grid-search over a
# handful of net-move trades overfits (the same PBO/DSR trap the ML harness
# guards against) — so we separate "worth showing" from "worth acting on":
#   _MIN_TRADES        — show a per-setup/per-symbol STAT at all (informational).
#   _MIN_FINDING_WARN  — escalate a negative finding from info → warn.
#   _MIN_SUGGEST       — emit an ACTIONABLE parameter change (else advisory only).
#   _MIN_GRID          — run the σ grid-search AND require out-of-sample confirmation.
_MIN_TRADES = 3
_MIN_FINDING_WARN = 12
_MIN_SUGGEST = 30
_MIN_GRID = 40
# Flat band: |pnl| below this fraction of entry notional counts as a scratch.
_FLAT_FRAC = 0.0005


def _to_float(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _today_et() -> str:
    now = datetime.now(_ET) if _ET else datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")


def _latest_trade_date(sqlite) -> str | None:
    row = sqlite.fetchone("SELECT max(trade_date) AS d FROM day_signal_journal")
    return row["d"] if row and row["d"] else None


# --------------------------------------------------------------------------- #
# P&L attribution
# --------------------------------------------------------------------------- #
def _row_sigma(r) -> float | None:
    """The entry σ the day trader sized this trade with — stashed in the journal's
    JSON ``context`` column (no schema migration). None if absent / unparseable."""
    ctx = r["context"] if "context" in r.keys() else None
    if not ctx:
        return None
    try:
        s = json.loads(ctx).get("sigma")
        return float(s) if s is not None else None
    except (ValueError, TypeError):
        return None


def _backfill_outcomes(sqlite, trade_date: str, rows: list) -> list[dict]:
    """Attribute realized/marked P&L to every acted row and UPDATE the journal.

    Entry fill = the row's linked proposal's filled order (or, lacking a link,
    the first day-sleeve buy of that symbol at/after the journal ts). Exit = the
    next day-sleeve sell of the same symbol after entry (FIFO-consumed so two
    entries never claim the same exit); still-open entries mark to the last
    price the journal saw for that symbol. Never raises on a single bad row.
    """
    # All filled day-sleeve orders, oldest first — entries and exits both live
    # here. Loaded once; exits can fall on a later calendar day so we don't
    # date-filter the order book.
    orders = sqlite.fetchall(
        "SELECT proposal_id, symbol, side, filled_qty, filled_avg_price, submitted_at "
        "FROM bot_orders WHERE sleeve = 'day' AND status = 'filled' "
        "AND filled_qty IS NOT NULL ORDER BY submitted_at ASC, id ASC"
    )
    by_proposal: dict[int, dict] = {}
    sells: dict[str, list[dict]] = defaultdict(list)
    buys_by_symbol: dict[str, list[dict]] = defaultdict(list)
    for o in orders:
        rec = {
            "proposal_id": o["proposal_id"],
            "symbol": o["symbol"],
            "nsym": norm_symbol(o["symbol"] or ""),
            "side": (o["side"] or "").lower(),
            "qty": _to_float(o["filled_qty"]) or 0.0,
            "price": _to_float(o["filled_avg_price"]),
            "ts": o["submitted_at"] or "",
            "used": False,
        }
        if o["proposal_id"] is not None:
            by_proposal[int(o["proposal_id"])] = rec
        if rec["side"] == "sell":
            sells[rec["nsym"]].append(rec)
        elif rec["side"] == "buy":
            buys_by_symbol[rec["nsym"]].append(rec)

    # Last price the journal saw per symbol on this date — the mark for opens.
    last_seen: dict[str, float] = {}
    for r in rows:
        lp = _to_float(r["last_price"])
        if lp is not None:
            last_seen[norm_symbol(r["symbol"] or "")] = lp  # rows are ts-asc below

    updates: list[tuple] = []
    trades: list[dict] = []
    for r in rows:
        if (r["decision"] or "") != "acted":
            continue
        try:
            outcome, pnl, ep, xp, qty = _attribute_one(
                r, by_proposal, sells, buys_by_symbol, last_seen)
        except Exception:
            log.debug("pnl attribution failed for journal id=%s", r["id"], exc_info=True)
            continue
        updates.append((outcome, pnl, r["id"]))
        # Trade record for the risk metrics + σ grid-search (closed trades only).
        if outcome in ("win", "loss", "flat") and ep and xp and qty:
            notional = abs(ep * qty)
            trades.append({
                "symbol": (r["symbol"] or "?").upper(),
                "kind": (r["signal_kind"] or "none"),
                "outcome": outcome,
                "entry": ep, "exit": xp, "qty": qty,
                "sigma": _row_sigma(r),
                "pnl": pnl,
                "pnl_pct": (pnl / notional * 100.0) if notional else None,
            })

    for outcome, pnl, rid in updates:
        try:
            sqlite.execute(
                "UPDATE day_signal_journal SET outcome = ?, pnl = ? WHERE id = ?",
                [outcome, pnl, rid],
            )
        except Exception:
            log.debug("journal outcome update failed for id=%s", rid, exc_info=True)
    return trades


def _attribute_one(r, by_proposal, sells, buys_by_symbol, last_seen):
    """Return (outcome, pnl, entry_price, exit_price, qty) for one acted journal
    row. Long entries only; a sell-side entry (rare for the day sleeve) is marked
    open. entry/exit/qty are None when there's no usable entry fill."""
    nsym = norm_symbol(r["symbol"] or "")
    jts = r["ts"] or ""
    entry = None
    pid = r["proposal_id"]
    if pid is not None:
        entry = by_proposal.get(int(pid))
    if entry is None or entry["side"] != "buy" or entry["price"] is None:
        # Fallback: first unused day-sleeve buy of this symbol at/after the
        # journal timestamp.
        for b in buys_by_symbol.get(nsym, []):
            if not b["used"] and b["price"] is not None and b["ts"] >= jts:
                entry = b
                break
    if entry is None or entry["price"] is None or entry["side"] != "buy":
        return "open", None, None, None, None

    qty = entry["qty"] or 0.0
    ep = entry["price"]
    notional = abs(ep * qty)
    flat_eps = max(0.01, notional * _FLAT_FRAC)

    # Find the next unused sell of this symbol after the entry fill (FIFO).
    exit_order = None
    for s in sells.get(nsym, []):
        if not s["used"] and s["price"] is not None and s["ts"] >= entry["ts"]:
            exit_order = s
            break
    if exit_order is not None:
        exit_order["used"] = True
        xp = exit_order["price"]
        pnl = (xp - ep) * qty
        if abs(pnl) < flat_eps:
            return "flat", round(pnl, 4), ep, xp, qty
        return ("win" if pnl > 0 else "loss"), round(pnl, 4), ep, xp, qty

    # Still open — mark to last seen price for the symbol.
    mark = last_seen.get(nsym, ep)
    pnl = (mark - ep) * qty
    return "open", round(pnl, 4), ep, mark, qty


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
def _rate(wins: int, losses: int) -> float | None:
    n = wins + losses
    return round(wins / n, 3) if n else None


def _compute_stats(rows: list) -> dict:
    counts_by_decision: dict[str, int] = defaultdict(int)
    by_kind: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0,
                                                     "flat": 0, "open": 0, "pnl_sum": 0.0})
    by_regime: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0,
                                                      "pnl_sum": 0.0})
    by_symbol: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0,
                                                      "pnl_sum": 0.0})
    by_direction: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0,
                                                         "pnl_sum": 0.0})
    skip_reasons: dict[str, int] = defaultdict(int)
    block_reasons: dict[str, int] = defaultdict(int)
    conv_winners: list[float] = []
    conv_losers: list[float] = []
    # momentum in non-risk-on regime
    mom_offrisk = {"n": 0, "pnl_sum": 0.0, "wins": 0, "losses": 0}

    for r in rows:
        decision = r["decision"] or "unknown"
        counts_by_decision[decision] += 1
        reason = (r["reason"] or "").strip()
        if decision == "skipped" and reason:
            skip_reasons[reason[:80]] += 1
        elif decision == "blocked" and reason:
            block_reasons[reason[:80]] += 1
        if decision != "acted":
            continue

        outcome = (r["outcome"] or "open")
        pnl = _to_float(r["pnl"]) or 0.0
        kind = (r["signal_kind"] or "none")
        regime = (r["regime"] or "unknown")
        symbol = (r["symbol"] or "?").upper()
        conv = _to_float(r["conviction"])
        # Long vs short: classify by the action taken. A short entry trades the
        # 'short' side; everything else is the long lifecycle (buy open / sell close).
        dirn = "short" if (r["side"] or "").lower() == "short" else "long"

        k = by_kind[kind]
        k["n"] += 1
        k["pnl_sum"] += pnl
        rg = by_regime[regime]
        rg["n"] += 1
        rg["pnl_sum"] += pnl
        sy = by_symbol[symbol]
        sy["n"] += 1
        sy["pnl_sum"] += pnl
        dd = by_direction[dirn]
        dd["n"] += 1
        dd["pnl_sum"] += pnl

        if outcome == "win":
            k["wins"] += 1; rg["wins"] += 1; sy["wins"] += 1; dd["wins"] += 1
            if conv is not None:
                conv_winners.append(conv)
        elif outcome == "loss":
            k["losses"] += 1; rg["losses"] += 1; sy["losses"] += 1; dd["losses"] += 1
            if conv is not None:
                conv_losers.append(conv)
        elif outcome == "flat":
            k["flat"] += 1
        else:
            k["open"] += 1

        if kind == "momentum" and regime not in ("risk-on", "risk_on"):
            mom_offrisk["n"] += 1
            mom_offrisk["pnl_sum"] += pnl
            if outcome == "win":
                mom_offrisk["wins"] += 1
            elif outcome == "loss":
                mom_offrisk["losses"] += 1

    def _finalize(d: dict) -> dict:
        out = {}
        for key, v in d.items():
            n = v["n"]
            out[key] = {
                "n": n,
                "wins": v["wins"],
                "losses": v["losses"],
                "win_rate": _rate(v["wins"], v["losses"]),
                "avg_pnl": round(v["pnl_sum"] / n, 4) if n else None,
                "total_pnl": round(v["pnl_sum"], 4),
            }
        return out

    def _avg(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 3) if xs else None

    acted = counts_by_decision.get("acted", 0)
    total = sum(counts_by_decision.values())
    return {
        "trade_date_rows": total,
        "counts_by_decision": dict(counts_by_decision),
        "by_signal_kind": _finalize(by_kind),
        "by_direction": _finalize(by_direction),
        "by_regime": _finalize(by_regime),
        "by_symbol": _finalize(by_symbol),
        "conviction": {
            "winners_avg": _avg(conv_winners),
            "losers_avg": _avg(conv_losers),
            "n_winners": len(conv_winners),
            "n_losers": len(conv_losers),
        },
        "top_skip_reasons": [
            {"reason": k, "count": v}
            for k, v in sorted(skip_reasons.items(), key=lambda x: x[1], reverse=True)[:6]
        ],
        "top_block_reasons": [
            {"reason": k, "count": v}
            for k, v in sorted(block_reasons.items(), key=lambda x: x[1], reverse=True)[:6]
        ],
        "hedge_rebalances": counts_by_decision.get("hedge", 0),
        "n_acted": acted,
        "skip_rate": round(counts_by_decision.get("skipped", 0) / total, 3) if total else None,
        "_mom_offrisk": mom_offrisk,  # internal, dropped before persist
    }


# --------------------------------------------------------------------------- #
# Risk:reward, Sharpe, and the σ-multiple grid search
# --------------------------------------------------------------------------- #
def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _std(xs: list[float]) -> float | None:
    """Sample (n-1) standard deviation; None below 2 points."""
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def _risk_reward(trades: list[dict]) -> dict:
    """Avg win / avg loss in $ and %, plus the realized reward:risk ratio."""
    wins = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    avg_win_usd = _mean([t["pnl"] for t in wins])
    avg_loss_usd = _mean([t["pnl"] for t in losses])  # negative
    avg_win_pct = _mean([t["pnl_pct"] for t in wins if t["pnl_pct"] is not None])
    avg_loss_pct = _mean([t["pnl_pct"] for t in losses if t["pnl_pct"] is not None])
    rr_usd = (abs(avg_win_usd / avg_loss_usd)
              if avg_win_usd is not None and avg_loss_usd not in (None, 0) else None)
    rr_pct = (abs(avg_win_pct / avg_loss_pct)
              if avg_win_pct is not None and avg_loss_pct not in (None, 0) else None)
    return {
        "n_wins": len(wins), "n_losses": len(losses),
        "avg_win_usd": round(avg_win_usd, 4) if avg_win_usd is not None else None,
        "avg_loss_usd": round(avg_loss_usd, 4) if avg_loss_usd is not None else None,
        "avg_win_pct": round(avg_win_pct, 3) if avg_win_pct is not None else None,
        "avg_loss_pct": round(avg_loss_pct, 3) if avg_loss_pct is not None else None,
        "reward_risk_usd": round(rr_usd, 2) if rr_usd is not None else None,
        "reward_risk_pct": round(rr_pct, 2) if rr_pct is not None else None,
    }


def _sharpe(trades: list[dict], sqlite) -> dict:
    """Per-trade Sharpe (mean/stdev of per-trade % returns) and, when ≥2 session
    days of closed P&L exist, a daily Sharpe over per-day P&L totals. Both are
    raw (not annualized) — directional, small-sample reads, not risk-adjusted
    return claims."""
    rets = [t["pnl_pct"] for t in trades
            if t["outcome"] in ("win", "loss", "flat") and t["pnl_pct"] is not None]
    pt_mean, pt_std = _mean(rets), _std(rets)
    per_trade = round(pt_mean / pt_std, 3) if (pt_mean is not None and pt_std) else None

    rows = sqlite.fetchall(
        "SELECT trade_date, SUM(pnl) AS d FROM day_signal_journal "
        "WHERE decision = 'acted' AND outcome IN ('win','loss','flat') AND pnl IS NOT NULL "
        "GROUP BY trade_date ORDER BY trade_date"
    )
    daily = [_to_float(r["d"]) for r in rows if _to_float(r["d"]) is not None]
    d_mean, d_std = _mean(daily), _std(daily)
    daily_sharpe = round(d_mean / d_std, 3) if (d_mean is not None and d_std) else None
    return {"per_trade": per_trade, "n_trades": len(rets),
            "daily": daily_sharpe, "n_days": len(daily)}


# σ-multiplier grids the grid-search sweeps (stop distance, target/trail reach).
_STOP_SIGMA_GRID = [0.5, 0.75, 1.0, 1.25, 1.5]
_TP_SIGMA_GRID = [2.0, 2.5, 3.0, 3.5, 4.0, 5.0]


def _grid_search(trades: list[dict], settings) -> dict:
    """Over recent closed day trades (using the journaled entry σ + entry/exit
    prices), find the (stop_sigma, tp_sigma) grid point that would have maximized
    per-trade expectancy in σ units.

    HONEST CAVEAT: with only entry+exit (no intrabar path) we use the NET move as
    a proxy — a trade whose net move ≤ −stop_sigma is treated as stopped at
    −stop_sigma, one whose net move ≥ tp_sigma as a +tp_sigma exit, else the net
    move. This ignores intrabar whipsaw (a real stop could be hit before a winner
    runs), so the result is directional only, and meaningless on a small sample."""
    pts = [t for t in trades
           if t.get("sigma") and t["sigma"] > 0 and t["outcome"] in ("win", "loss", "flat")]
    n = len(pts)
    cur_stop = float(getattr(settings, "day_stop_sigma", 1.0))
    cur_tp = float(getattr(settings, "day_tp_sigma", 3.0))
    if n < _MIN_GRID:
        # Below this we don't even propose a direction: a grid max over a handful
        # of net-move samples is overfit noise (PBO trap). Show the count only.
        return {"ok": False, "actionable": False, "n_trades": n,
                "note": (f"only {n} σ-tagged closed trades (need ≥{_MIN_GRID} for an "
                         "out-of-sample-validated grid search) — not tuning stop/tp σ yet")}
    moves = [(t["exit"] - t["entry"]) / t["sigma"] for t in pts]

    def expectancy_over(ms: list[float], s: float, tp: float) -> float:
        return _mean([max(-s, min(tp, m)) for m in ms]) or 0.0

    def best_over(ms: list[float]) -> dict:
        b = None
        for s in _STOP_SIGMA_GRID:
            for tp in _TP_SIGMA_GRID:
                if tp / s < 1.0:
                    continue  # a target tighter than the stop is nonsensical
                e = expectancy_over(ms, s, tp)
                if b is None or e > b["exp"]:
                    b = {"stop_sigma": s, "tp_sigma": tp, "exp": e}
        return b

    # OUT-OF-SAMPLE guard against overfitting: optimize on the chronological first
    # half (train), then require the winning (stop, tp) to ALSO beat the current
    # setting on the held-out second half (test). A grid corner that only wins
    # in-sample (the classic overfit) fails this and is NOT proposed as actionable.
    half = n // 2
    train, test = moves[:half], moves[half:]
    b_train = best_over(train)
    b_full = best_over(moves)
    test_best = expectancy_over(test, b_train["stop_sigma"], b_train["tp_sigma"])
    test_cur = expectancy_over(test, cur_stop, cur_tp)
    oos_ok = (b_train["stop_sigma"] != cur_stop or b_train["tp_sigma"] != cur_tp) \
        and (test_best > test_cur + 0.02)
    return {
        "ok": True,
        "actionable": bool(oos_ok),
        "n_trades": n,
        "current_stop_sigma": cur_stop, "current_tp_sigma": cur_tp,
        "current_expectancy_sigma": round(expectancy_over(moves, cur_stop, cur_tp), 3),
        # The OOS-validated (train-chosen) point is what we'd actually propose;
        # the full-sample best is shown for transparency but never proposed alone.
        "best_stop_sigma": b_train["stop_sigma"], "best_tp_sigma": b_train["tp_sigma"],
        "best_expectancy_sigma": round(expectancy_over(moves, b_train["stop_sigma"], b_train["tp_sigma"]), 3),
        "insample_best_stop_sigma": b_full["stop_sigma"], "insample_best_tp_sigma": b_full["tp_sigma"],
        "oos_test_expectancy": round(test_best, 3), "oos_current_expectancy": round(test_cur, 3),
        "oos_ok": bool(oos_ok),
        "caveat": ("net entry→exit move used as a path proxy (NO intrabar excursion, so a tight "
                   "stop's real whipsaw rate is understated); train/test split on "
                   f"{n} trades — directional, not a true backtest"),
    }


# --------------------------------------------------------------------------- #
# Findings + suggestions
# --------------------------------------------------------------------------- #
def _confidence(n: int) -> str:
    """Map a closed-trade count to a confidence label used across findings."""
    if n >= _MIN_SUGGEST:
        return "high"
    if n >= _MIN_FINDING_WARN:
        return "medium"
    return "low"


def _derive_findings(stats: dict) -> list[dict]:
    findings: list[dict] = []

    # 1. A setup type with negative avg P&L. Severity scales with sample size —
    #    a 3-trade red flag is an observation (info), not a warning (warn).
    for kind, s in stats["by_signal_kind"].items():
        if kind == "none":
            continue
        closed = s["wins"] + s["losses"]
        if closed >= _MIN_TRADES and (s["avg_pnl"] or 0) < 0:
            findings.append({
                "severity": "warn" if closed >= _MIN_FINDING_WARN else "info",
                "title": f"{kind} setups losing money",
                "detail": (f"{kind} trades averaged {s['avg_pnl']:+.2f} P&L over "
                           f"{closed} closed ({s['win_rate']:.0%} win-rate)."
                           + ("" if closed >= _MIN_SUGGEST
                              else f" Sample is small (n={closed}) — observe, don't tune yet.")),
                "tag": f"setup:{kind}",
                "n": closed,
                "confidence": _confidence(closed),
            })

    # 1b. A DIRECTION (long or short) systematically losing money.
    for dirn, s in stats.get("by_direction", {}).items():
        closed = s["wins"] + s["losses"]
        if closed >= _MIN_TRADES and (s["avg_pnl"] or 0) < 0:
            findings.append({
                "severity": "warn" if closed >= _MIN_FINDING_WARN else "info",
                "title": f"{dirn} trades losing money",
                "detail": (f"{dirn} side averaged {s['avg_pnl']:+.2f} P&L over {closed} closed "
                           f"({s['win_rate']:.0%} win-rate)."
                           + ("" if closed >= _MIN_SUGGEST
                              else f" Sample is small (n={closed}) — observe, don't tune yet.")),
                "tag": f"direction:{dirn}",
                "n": closed,
                "confidence": _confidence(closed),
            })

    # 2. A symbol repeatedly losing.
    for sym, s in stats["by_symbol"].items():
        closed = s["wins"] + s["losses"]
        if closed >= _MIN_TRADES and (s["win_rate"] is not None and s["win_rate"] < 0.34) \
                and (s["avg_pnl"] or 0) < 0:
            findings.append({
                "severity": "warn" if closed >= _MIN_FINDING_WARN else "info",
                "title": f"{sym} bleeds repeatedly",
                "detail": (f"{sym} won {s['win_rate']:.0%} of {closed} closed trades, "
                           f"avg {s['avg_pnl']:+.2f}."
                           + ("" if closed >= _MIN_SUGGEST
                              else f" Sample is small (n={closed}) — observe, don't tune yet.")),
                "tag": f"symbol:{sym}",
                "n": closed,
                "confidence": _confidence(closed),
            })

    # 3. Heavy skipping for budget / sleeve-size reasons.
    budget_skips = sum(
        r["count"] for r in stats["top_skip_reasons"]
        if any(w in r["reason"].lower() for w in ("budget", "sleeve", "buying power",
                                                  "notional", "over-deployed", "deployed"))
    )
    total_rows = stats["trade_date_rows"] or 0
    if total_rows and budget_skips / total_rows >= 0.20:
        findings.append({
            "severity": "info",
            "title": "Many signals skipped for day-budget limits",
            "detail": (f"{budget_skips} of {total_rows} signals ({budget_skips / total_rows:.0%}) "
                       "were skipped for sleeve/budget reasons — the day sleeve may be too "
                       "small or already fully deployed."),
            "tag": "budget",
        })

    # 4. Hedge churn (net-beta rebalance spam).
    hedges = stats["hedge_rebalances"]
    if hedges >= 8:
        findings.append({
            "severity": "info",
            "title": "High net-beta hedge churn",
            "detail": (f"{hedges} net-beta hedge rebalances in one session. This is usually a "
                       "reconcile-lag OSCILLATION (the hedge can't see its own just-filled SH "
                       "order so it overshoots the target and ping-pongs), NOT just a tight band "
                       "— consider disarming the SH hedge (it's a weak unlevered hedge) rather "
                       "than only widening the band."),
            "tag": "hedge_churn",
        })

    # 5. Momentum losing in non-risk-on regimes.
    mo = stats.get("_mom_offrisk") or {}
    closed = mo.get("wins", 0) + mo.get("losses", 0)
    if closed >= _MIN_TRADES and (mo.get("pnl_sum", 0) < 0):
        avg = mo["pnl_sum"] / mo["n"] if mo.get("n") else 0
        findings.append({
            "severity": "warn" if closed >= _MIN_FINDING_WARN else "info",
            "title": "Momentum chasing in defensive regimes",
            "detail": (f"momentum entries in non-risk-on regimes averaged {avg:+.2f} over "
                       f"{closed} closed — buying breakouts against the tape."
                       + ("" if closed >= _MIN_SUGGEST
                          else f" Sample is small (n={closed}) — observe, don't tune yet.")),
            "tag": "momentum_offrisk",
            "n": closed,
            "confidence": _confidence(closed),
        })

    # 6. Conviction inversion — losers had higher conviction than winners.
    c = stats["conviction"]
    if c["winners_avg"] is not None and c["losers_avg"] is not None \
            and c["n_winners"] >= 2 and c["n_losers"] >= 2 \
            and c["losers_avg"] > c["winners_avg"]:
        findings.append({
            "severity": "info",
            "title": "Conviction score inverted",
            "detail": (f"losers averaged {c['losers_avg']:.2f} conviction vs winners "
                       f"{c['winners_avg']:.2f} — the ranking is not separating outcomes."),
            "tag": "conviction",
        })

    if not findings:
        findings.append({
            "severity": "info",
            "title": "No systematic mistakes detected",
            "detail": "Not enough closed trades, or no setup/symbol cleared a red-flag bar.",
            "tag": "none",
        })
    return findings


def _clamp(v: float, lo: float, hi: float) -> float:
    return round(max(lo, min(hi, v)), 3)


def _derive_suggestions(stats: dict, findings: list[dict], settings) -> list[dict]:
    tags = {f["tag"] for f in findings}
    n_by_tag = {f["tag"]: f.get("n", 0) for f in findings}
    out: list[dict] = []

    def add(param, current, proposed, rationale, finding, n=None):
        # A suggestion is only ACTIONABLE when it rests on a real sample. Below
        # _MIN_SUGGEST it's still surfaced (so you can see what the bot is leaning
        # toward) but flagged advisory — don't auto-apply it.
        n = n_by_tag.get(finding, 0) if n is None else n
        actionable = n >= _MIN_SUGGEST
        conf = _confidence(n)
        if not actionable:
            rationale = (f"⚠ ADVISORY (n={n} < {_MIN_SUGGEST}, low confidence — observe, "
                         f"don't apply): {rationale}")
        out.append({
            "param": param,
            "current": current,
            "proposed": proposed,
            "rationale": rationale,
            "finding": finding,
            "n": n,
            "confidence": conf,
            "actionable": actionable,
        })

    # Reversion setups losing -> demand a more extreme stretch.
    if "setup:reversion" in tags:
        cur = float(getattr(settings, "day_reversion_z", 2.0))
        add("day_reversion_z", cur, _clamp(cur + 0.5, 1.5, 3.5),
            "Reversion setups lost money: require a wider VWAP stretch before fading.",
            "setup:reversion")

    # Momentum setups losing (any regime) -> raise the move + signal bar.
    if "setup:momentum" in tags or "momentum_offrisk" in tags:
        cur = float(getattr(settings, "day_momentum_min_pct", 0.30))
        add("day_momentum_min_pct", cur, _clamp(cur + 0.15, 0.20, 1.0),
            "Momentum entries underperformed: demand a larger intraday move to call momentum.",
            "setup:momentum" if "setup:momentum" in tags else "momentum_offrisk")
    if "momentum_offrisk" in tags:
        cur = bool(getattr(settings, "day_respect_strategist", True))
        if not cur:
            add("day_respect_strategist", cur, True,
                "Momentum lost in defensive regimes: let the strategist's avoid-list veto "
                "day longs.", "momentum_offrisk")
        curm = float(getattr(settings, "day_min_signal", 1.0))
        add("day_min_signal", curm, _clamp(curm + 0.3, 0.5, 3.0),
            "Raise the combined signal-strength bar so weak breakouts in defensive tape "
            "don't trade.", "momentum_offrisk")

    # Budget skips -> let the day sleeve hold more, or open more names per tick.
    if "budget" in tags:
        cur = float(getattr(settings, "day_alloc_max_pct", 7.0))
        add("day_alloc_max_pct", cur, _clamp(cur + 2.0, 3.0, 15.0),
            "Many signals were skipped for budget: lift the day-sleeve ceiling so good "
            "setups aren't starved.", "budget")
        curn = int(getattr(settings, "day_max_new_positions", 3))
        add("day_max_new_positions", curn, int(_clamp(curn + 1, 1, 8)),
            "Allow one more new position per tick to absorb skipped budget-limited signals.",
            "budget")

    # Hedge churn -> widen the rebalance band.
    if "hedge_churn" in tags:
        cur = float(getattr(settings, "day_net_beta_band_pct", 1.0))
        add("day_net_beta_band_pct", cur, _clamp(cur + 0.5, 0.5, 4.0),
            "Net-beta hedge rebalanced too often: widen the band to cut round-trip churn.",
            "hedge_churn")

    # A symbol bleeding -> tighten the stop on the whole sleeve (closest knob).
    if any(t.startswith("symbol:") for t in tags):
        cur = float(getattr(settings, "day_stop_pct", 0.8))
        sym_finding = next(t for t in tags if t.startswith("symbol:"))
        add("day_stop_pct", cur, _clamp(cur - 0.2, 0.3, 2.0),
            f"A symbol bled repeatedly ({sym_finding.split(':', 1)[1]}): tighten the bracket "
            "stop so losers are cut faster.", sym_finding)

    # σ-multiple grid-search: propose stop_sigma / tp_sigma ONLY when the winning
    # point cleared the out-of-sample guard (it also beat the current setting on a
    # held-out half). An in-sample-only max is overfit and is NOT proposed.
    gs = stats.get("grid_search") or {}
    if gs.get("ok") and gs.get("oos_ok"):
        rationale = (
            f"σ grid-search (OUT-OF-SAMPLE VALIDATED) over {gs['n_trades']} closed trades: stop "
            f"{gs['best_stop_sigma']}σ / tp {gs['best_tp_sigma']}σ beat current both in-sample "
            f"({gs['current_expectancy_sigma']:+.2f}σ → {gs['best_expectancy_sigma']:+.2f}σ) and "
            f"on the held-out half ({gs['oos_current_expectancy']:+.2f}σ → "
            f"{gs['oos_test_expectancy']:+.2f}σ). CAVEAT: {gs['caveat']}.")
        if gs["best_stop_sigma"] != gs["current_stop_sigma"]:
            add("day_stop_sigma", gs["current_stop_sigma"], gs["best_stop_sigma"],
                rationale, "grid_search", n=gs["n_trades"])
        if gs["best_tp_sigma"] != gs["current_tp_sigma"]:
            add("day_tp_sigma", gs["current_tp_sigma"], gs["best_tp_sigma"],
                rationale, "grid_search", n=gs["n_trades"])

    return out


# --------------------------------------------------------------------------- #
# Narrative
# --------------------------------------------------------------------------- #
def _template_summary(trade_date: str, stats: dict, findings: list[dict],
                      suggestions: list[dict]) -> str:
    cbd = stats["counts_by_decision"]
    acted = stats["n_acted"]
    pnl_total = sum(s["total_pnl"] for s in stats["by_signal_kind"].values())
    parts = [
        f"Day review {trade_date}: {stats['trade_date_rows']} signals seen — "
        f"{acted} acted, {cbd.get('skipped', 0)} skipped, {cbd.get('blocked', 0)} blocked, "
        f"{cbd.get('hedge', 0)} hedge rebalances. Net acted P&L {pnl_total:+.2f}."
    ]
    # Best / worst setup.
    kinds = [(k, v) for k, v in stats["by_signal_kind"].items()
             if k != "none" and v["n"]]
    if kinds:
        kinds.sort(key=lambda x: x[1]["avg_pnl"] if x[1]["avg_pnl"] is not None else 0)
        worst = kinds[0]
        best = kinds[-1]
        parts.append(
            f"Best setup: {best[0]} (avg {best[1]['avg_pnl']:+.2f}); "
            f"worst: {worst[0]} (avg {worst[1]['avg_pnl']:+.2f})."
        )
    rr = stats.get("risk_reward") or {}
    if rr.get("reward_risk_usd") is not None:
        parts.append(
            f"Reward:risk {rr['reward_risk_usd']:.2f}:1 "
            f"(avg win {rr['avg_win_usd']:+.2f} vs avg loss {rr['avg_loss_usd']:+.2f}, "
            f"{rr['n_wins']}W/{rr['n_losses']}L).")
    sh = stats.get("sharpe") or {}
    if sh.get("per_trade") is not None:
        sd = f", daily {sh['daily']:.2f} over {sh['n_days']}d" if sh.get("daily") is not None else ""
        parts.append(f"Sharpe (per-trade) {sh['per_trade']:.2f} on {sh['n_trades']} trades{sd}.")
    gs = stats.get("grid_search") or {}
    if gs.get("ok"):
        parts.append(
            f"σ grid-search ({gs['n_trades']} trades): R:R best at stop "
            f"{gs['best_stop_sigma']}σ / tp {gs['best_tp_sigma']}σ "
            f"(expectancy {gs['best_expectancy_sigma']:+.2f}σ vs current "
            f"{gs['current_expectancy_sigma']:+.2f}σ) — {gs['caveat']}.")
    top_findings = [f for f in findings if f["tag"] != "none"][:2]
    if top_findings:
        parts.append("Flags: " + "; ".join(f["title"] for f in top_findings) + ".")
    if suggestions:
        parts.append("Suggested tweaks: " + ", ".join(
            f"{s['param']} {s['current']}→{s['proposed']}" for s in suggestions[:3]) + ".")
    else:
        parts.append("No parameter changes suggested.")
    return " ".join(parts)


_LLM_PROMPT = """You are the post-session coach for a paper day-trading bot.
Below is a JSON review: today's stats, a trailing-window aggregate
(``window_stats``) the findings/suggestions are actually derived from, detected
findings, proposed config tweaks, and ``memory`` — prior reviews plus the
suggestion ledger with before/after expectancy for tweaks that were accepted.

Write a tight 4-6 sentence narrative for the trader: what happened today, the
single clearest systematic mistake (cite the window sample, not just today), and
which one or two parameter tweaks matter most. CRUCIALLY, look at ``memory``: if a
previously accepted tweak has metric_after vs metric_before, say whether it
helped or hurt; if you're re-recommending something already rejected, acknowledge
it. Plain prose, no markdown headers, no preamble, only facts from the JSON.

DATA:
{data}
"""


def _llm_summary(llm, payload: dict) -> tuple[str, str] | None:
    """Best-effort LLM narrative; runs the async client synchronously from this
    worker thread. Returns (text, model_label) or None to fall back."""
    if llm is None:
        return None
    try:
        prompt = _LLM_PROMPT.format(data=json.dumps(payload, indent=1, default=str))
        text = asyncio.run(llm.generate(prompt))
        text = (text or "").strip()
        if len(text) < 40:
            raise ValueError("too short")
        return text, getattr(llm, "label", "llm")
    except Exception as exc:
        log.warning("day-review LLM summary failed (%s) — using template", exc)
        return None


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
# Learning-loop v2 helpers: trailing window, override-aware "current" values,
# expectancy grading, and the memory the LLM payload carries.
# --------------------------------------------------------------------------- #
_JOURNAL_COLS = (
    "id, ts, symbol, asset_class, sector, signal_kind, direction, strength, "
    "z, last_price, vwap, regime, vol_pctile, market_bias, decision, side, notional, "
    "qty, conviction, reason, context, proposal_id, outcome, pnl"
)


class _EffSettings:
    """Settings proxy that returns the LIVE (override-aware) value for tunable
    knobs, so a suggestion's 'current' is what the bot is actually running — not
    the env default. Everything else delegates to the real settings."""

    def __init__(self, settings, eff: dict):
        object.__setattr__(self, "_s", settings)
        object.__setattr__(self, "_eff", eff)

    def __getattr__(self, name):
        eff = object.__getattribute__(self, "_eff")
        if name in eff:
            return eff[name]
        return getattr(object.__getattribute__(self, "_s"), name)


def _session_dates(sqlite, trade_date: str, n: int) -> list[str]:
    """The last ``n`` distinct journal session dates on or before ``trade_date``."""
    rows = sqlite.fetchall(
        "SELECT DISTINCT trade_date FROM day_signal_journal WHERE trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT ?", [trade_date, n])
    return [r["trade_date"] for r in rows]


def _windowed_rows(sqlite, dates: list[str]) -> list[dict]:
    if not dates:
        return []
    ph = ",".join("?" for _ in dates)
    return sqlite.fetchall(
        f"SELECT {_JOURNAL_COLS} FROM day_signal_journal WHERE trade_date IN ({ph}) "
        "ORDER BY ts ASC", list(dates))


def _expectancy(sqlite, *, on_or_before: str | None = None, after: str | None = None):
    """(avg $ P&L per closed acted trade, n) over the journal, optionally bounded
    by session date. Used for metric_before / metric_after grading."""
    where = ["decision = 'acted'", "outcome IN ('win','loss','flat')", "pnl IS NOT NULL"]
    params: list = []
    if on_or_before:
        where.append("trade_date <= ?"); params.append(on_or_before)
    if after:
        where.append("trade_date > ?"); params.append(after)
    row = sqlite.fetchone(
        "SELECT avg(pnl) AS a, count(*) AS n FROM day_signal_journal WHERE "
        + " AND ".join(where), params)
    if not row or not row["n"] or row["a"] is None:
        return None, 0
    return round(float(row["a"]), 4), int(row["n"])


def _stop_noise_diagnostic(trades: list[dict]) -> dict:
    """Data-backed proxy for 'stops too tight' WITHOUT intrabar path: of closed
    LOSING trades, how many exited within ~0.15% of entry (a loss so small it's a
    microstructure stop-out, not a real adverse move). High share => the σ-stop is
    inside the noise band (the finding from the trade analysis)."""
    losers = [t for t in trades if t.get("outcome") == "loss" and t.get("entry") and t.get("exit")]
    if not losers:
        return {"n_losers": 0, "noise_stop_share": None}
    noise = sum(1 for t in losers
                if abs(t["exit"] - t["entry"]) / t["entry"] <= 0.0015)
    return {"n_losers": len(losers), "noise_stops": noise,
            "noise_stop_share": round(noise / len(losers), 3)}


def _review_memory(sqlite, sleeve: str, trade_date: str) -> dict:
    """Prior-review summaries + the suggestion ledger (with grades) — the memory
    fed to the LLM so it has continuity and can critique its own past advice."""
    hist = sqlite.fetchall(
        "SELECT trade_date, summary FROM day_review WHERE trade_date < ? "
        "ORDER BY trade_date DESC LIMIT 5", [trade_date])
    ledger = knobs.list_suggestions(sqlite, sleeve=sleeve, limit=20)
    return {
        "recent_reviews": [{"date": r["trade_date"], "summary": r["summary"]} for r in hist],
        "suggestion_ledger": [
            {"param": s["param"], "proposed": s["proposed_value"], "status": s["status"],
             "metric_before": s["metric_before"], "metric_after": s["metric_after"],
             "review_date": s["review_date"]}
            for s in ledger],
    }


# --------------------------------------------------------------------------- #
def review_day(sqlite, duck, settings, *, trade_date: str | None = None,
               llm=None) -> dict:
    """Review one day-trader session: backfill P&L, mine the journal, persist a
    ``day_review`` row, and return the full review dict. Blocking; dispatch via
    run_in_executor. ``duck`` is accepted for signature parity / future price
    fallbacks but the journal carries its own last-price marks."""
    if trade_date is None:
        trade_date = _latest_trade_date(sqlite) or _today_et()

    rows = sqlite.fetchall(
        "SELECT id, ts, symbol, asset_class, sector, signal_kind, direction, strength, "
        "z, last_price, vwap, regime, vol_pctile, market_bias, decision, side, notional, "
        "qty, conviction, reason, context, proposal_id, outcome, pnl "
        "FROM day_signal_journal WHERE trade_date = ? ORDER BY ts ASC",
        [trade_date],
    )
    if not rows:
        log.info("day review: no journal rows for %s — no-op", trade_date)
        return {
            "trade_date": trade_date,
            "stats": {}, "findings": [], "suggestions": [],
            "summary": f"No day-trader signals journaled for {trade_date}.",
            "model": "deterministic",
            "no_data": True,
        }

    trades = _backfill_outcomes(sqlite, trade_date, rows)
    # Re-read so stats see the freshly-written outcomes/pnl.
    rows = sqlite.fetchall(
        "SELECT id, ts, symbol, asset_class, sector, signal_kind, direction, strength, "
        "z, last_price, vwap, regime, vol_pctile, market_bias, decision, side, notional, "
        "qty, conviction, reason, context, proposal_id, outcome, pnl "
        "FROM day_signal_journal WHERE trade_date = ? ORDER BY ts ASC",
        [trade_date],
    )

    # Override-aware settings so a suggestion's "current" is the LIVE knob value.
    eff = _EffSettings(settings, knobs.effective_params(sqlite, settings))

    # TODAY's snapshot — drives the narrative, the persisted stats, the UI/file.
    stats = _compute_stats(rows)
    stats["risk_reward"] = _risk_reward(trades)
    stats["sharpe"] = _sharpe(trades, sqlite)
    stats["grid_search"] = _grid_search(trades, eff)
    stats["stop_noise"] = _stop_noise_diagnostic(trades)

    # TRAILING WINDOW — the finding/suggestion engine tunes on a real sample, not
    # a single session. Setup/symbol/direction/regime stats pool the last N
    # sessions; the σ grid-search (needs entry/exit pairing) stays today-scoped
    # with its own out-of-sample guard.
    window_dates = _session_dates(sqlite, trade_date, _REVIEW_WINDOW_SESSIONS)
    wrows = _windowed_rows(sqlite, window_dates)
    wstats = _compute_stats(wrows)
    wstats["risk_reward"] = stats["risk_reward"]
    wstats["sharpe"] = stats["sharpe"]
    wstats["grid_search"] = stats["grid_search"]
    findings = _derive_findings(wstats)
    suggestions = _derive_suggestions(wstats, findings, eff)
    stats["window"] = {"n_sessions": len(window_dates), "n_rows": len(wrows),
                       "start": window_dates[-1] if window_dates else None,
                       "end": window_dates[0] if window_dates else None}
    stats.pop("_mom_offrisk", None)  # internal only
    wstats.pop("_mom_offrisk", None)

    # Persist suggestions to the ledger (only whitelisted knobs; dedup pending),
    # and GRADE past accepted ones (before → after expectancy) so the loop learns
    # whether its advice helped.
    metric_before, _ = _expectancy(sqlite, on_or_before=trade_date)
    try:
        knobs.record_suggestions(sqlite, "day", trade_date, suggestions,
                                 metric_before=metric_before)

        def _after(decided_at):
            exp, n = _expectancy(sqlite, after=str(decided_at)[:10])
            return exp if n >= _MIN_TRADES else None

        knobs.grade_accepted(sqlite, "day", _after)
    except Exception:
        log.warning("suggestion ledger update failed for %s", trade_date, exc_info=True)

    # Narrative — LLM if wired, deterministic template otherwise. The payload now
    # carries MEMORY: prior reviews + the graded suggestion ledger, so the LLM has
    # continuity and can judge whether accepted tweaks actually helped.
    payload = {"trade_date": trade_date, "stats": stats,
               "window_stats": wstats, "findings": findings, "suggestions": suggestions,
               "memory": _review_memory(sqlite, "day", trade_date)}
    llm_out = _llm_summary(llm, payload)
    if llm_out is not None:
        summary, model = llm_out
    else:
        summary = _template_summary(trade_date, stats, findings, suggestions)
        model = "deterministic"

    result = {
        "trade_date": trade_date,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": stats,
        "findings": findings,
        "suggestions": suggestions,
        "summary": summary,
        "model": model,
    }

    try:
        sqlite.execute(
            "INSERT OR REPLACE INTO day_review "
            "(trade_date, created_at, stats, findings, suggestions, summary, model) "
            "VALUES (?,?,?,?,?,?,?)",
            [trade_date, result["created_at"], json.dumps(stats),
             json.dumps(findings), json.dumps(suggestions), summary, model],
        )
    except Exception:
        log.warning("day_review persist failed for %s", trade_date, exc_info=True)

    # Also drop a human-readable learnings file (best-effort, never fatal).
    try:
        from app.trading.learnings import write_learnings_file
        path = write_learnings_file(result, sleeve="day", data_dir=settings.data_dir)
        if path is not None:
            result["learnings_file"] = str(path)
    except Exception:
        log.debug("day learnings file skipped", exc_info=True)

    log.info("day review %s: %d signals, %d acted, %d findings, %d suggestions (model %s)",
             trade_date, stats["trade_date_rows"], stats["n_acted"],
             len([f for f in findings if f["tag"] != "none"]), len(suggestions), model)
    return result


def latest_review(sqlite, trade_date: str | None = None) -> dict:
    """Read a persisted review row (latest, or a specific date). {} if none."""
    if trade_date:
        row = sqlite.fetchone(
            "SELECT * FROM day_review WHERE trade_date = ?", [trade_date])
    else:
        row = sqlite.fetchone(
            "SELECT * FROM day_review ORDER BY trade_date DESC LIMIT 1")
    if not row:
        return {}
    d = dict(row)
    for k in ("stats", "findings", "suggestions"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (ValueError, TypeError):
                pass
    return d
