"""Learning loop for the SWING (long-term) sleeve.

The DAY learning loop (``day_review``) mines an intraday signal journal one
session at a time. The swing book is a weeks-to-months horizon, so this loop
works off the round-trip *tradebook* instead of a per-tick journal: it pairs the
swing sleeve's filled buys/sells into closed (and still-open, marked) trades via
``tradebook.list_trades`` and asks the slow questions that matter for a
position book:

  1. Overall realized win-rate / average / total P&L on CLOSED swing trades,
     plus the marked unrealized P&L of the OPEN book.
  2. The same broken down BY the strategist proposal BUCKET (equities / metals /
     crypto / cash — mapped from ``bot_proposals``) and BY direction (long vs
     short).
  3. A few advisory *findings* (e.g. a bucket that consistently loses) using the
     same sample-size discipline ``day_review`` uses — a 3-trade red flag is an
     observation, not a warning, and only a real sample turns a suggestion
     actionable.
  4. A short deterministic narrative (LLM if one is wired) and one persisted
     ``swing_review`` row (INSERT OR REPLACE, keyed by the date it ran).

The compute path is blocking (SQLite reads + a little math); callers dispatch it
via ``run_in_executor``. It is defensive throughout — a single bad row is
skipped, never fatal, and it never raises on missing data.
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
from app.trading.tradebook import list_trades

log = logging.getLogger("market.trading.swing_review")

# Sample-size tiers — same discipline as day_review. A swing book accumulates
# trades far more slowly than the intraday sleeve, so these gate the *tone*
# (info vs warn) and whether a suggestion is actionable, never whether a stat is
# shown.
#   _MIN_TRADES        — show a per-bucket/per-symbol stat / raise an info finding.
#   _MIN_FINDING_WARN  — escalate a negative finding from info -> warn.
#   _MIN_SUGGEST       — emit an ACTIONABLE parameter change (else advisory only).
_MIN_TRADES = 3
_MIN_FINDING_WARN = 8
_MIN_SUGGEST = 15


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


def _confidence(n: int) -> str:
    if n >= _MIN_SUGGEST:
        return "high"
    if n >= _MIN_FINDING_WARN:
        return "medium"
    return "low"


def _rate(wins: int, losses: int) -> float | None:
    n = wins + losses
    return round(wins / n, 3) if n else None


# --------------------------------------------------------------------------- #
# Bucket attribution
# --------------------------------------------------------------------------- #
def _bucket_map(sqlite) -> dict[str, str]:
    """norm_symbol -> the swing proposal bucket (equities | metals | crypto |
    cash), from the latest swing proposal that recorded one. Best-effort."""
    out: dict[str, str] = {}
    try:
        rows = sqlite.fetchall(
            "SELECT symbol, bucket FROM bot_proposals "
            "WHERE sleeve = 'swing' AND bucket IS NOT NULL ORDER BY id ASC"
        )
    except Exception:
        return out
    for r in rows:
        try:
            nsym = norm_symbol(r["symbol"] or "")
            b = (r["bucket"] or "").strip()
            if nsym and b:
                out[nsym] = b  # ascending id -> last write wins (most recent)
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
def _leg() -> dict:
    return {"n": 0, "wins": 0, "losses": 0, "flat": 0, "pnl_sum": 0.0}


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


def _compute_stats(trades: list[dict], buckets: dict[str, str]) -> dict:
    """Mine closed + open swing round-trips into overall / per-bucket /
    per-direction / per-symbol legs. Closed trades drive win-rate; open trades
    are summed separately as marked (unrealized) P&L."""
    overall = _leg()
    by_bucket: dict[str, dict] = defaultdict(_leg)
    by_direction: dict[str, dict] = defaultdict(_leg)
    by_symbol: dict[str, dict] = defaultdict(_leg)

    n_closed = 0
    n_open = 0
    open_pnl_sum = 0.0
    open_marked = 0  # open lots that actually had a usable mark

    for t in trades:
        try:
            sym = (t.get("symbol") or "?").upper()
            nsym = norm_symbol(t.get("symbol") or "")
            bucket = buckets.get(nsym, "unknown")
            dirn = "short" if (t.get("direction") == "short") else "long"
            pnl = _to_float(t.get("pnl"))

            if t.get("status") == "open":
                n_open += 1
                if pnl is not None:
                    open_pnl_sum += pnl
                    open_marked += 1
                continue

            # Closed round-trip.
            n_closed += 1
            pnl = pnl or 0.0
            outcome = "win" if pnl > 0 else ("loss" if pnl < 0 else "flat")
            for leg in (overall, by_bucket[bucket], by_direction[dirn], by_symbol[sym]):
                leg["n"] += 1
                leg["pnl_sum"] += pnl
                if outcome == "win":
                    leg["wins"] += 1
                elif outcome == "loss":
                    leg["losses"] += 1
                else:
                    leg["flat"] += 1
        except Exception:
            log.debug("swing review: skipping bad trade row", exc_info=True)
            continue

    overall_out = _finalize({"all": overall}).get("all", {
        "n": 0, "wins": 0, "losses": 0, "win_rate": None,
        "avg_pnl": None, "total_pnl": 0.0,
    })
    return {
        "trades_total": len(trades),
        "n_closed": n_closed,
        "n_open": n_open,
        "overall": overall_out,
        "open_pnl": round(open_pnl_sum, 4) if open_marked else None,
        "by_bucket": _finalize(by_bucket),
        "by_direction": _finalize(by_direction),
        "by_symbol": _finalize(by_symbol),
    }


# --------------------------------------------------------------------------- #
# Findings + suggestions
# --------------------------------------------------------------------------- #
def _losing_note(closed: int) -> str:
    return ("" if closed >= _MIN_SUGGEST
            else f" Sample is small (n={closed}) — observe, don't tune yet.")


def _derive_findings(stats: dict) -> list[dict]:
    findings: list[dict] = []

    # 1. A bucket consistently losing money.
    for bucket, s in stats["by_bucket"].items():
        if bucket in ("unknown", "cash"):
            continue
        closed = s["wins"] + s["losses"]
        if closed >= _MIN_TRADES and (s["avg_pnl"] or 0) < 0:
            findings.append({
                "severity": "warn" if closed >= _MIN_FINDING_WARN else "info",
                "title": f"{bucket} bucket losing money",
                "detail": (f"{bucket} swing trades averaged {s['avg_pnl']:+.2f} P&L over "
                           f"{closed} closed ({(s['win_rate'] or 0):.0%} win-rate)."
                           + _losing_note(closed)),
                "tag": f"bucket:{bucket}",
                "n": closed,
                "confidence": _confidence(closed),
            })

    # 2. A direction (long or short) systematically losing.
    for dirn, s in stats["by_direction"].items():
        closed = s["wins"] + s["losses"]
        if closed >= _MIN_TRADES and (s["avg_pnl"] or 0) < 0:
            findings.append({
                "severity": "warn" if closed >= _MIN_FINDING_WARN else "info",
                "title": f"{dirn} swing trades losing money",
                "detail": (f"{dirn} side averaged {s['avg_pnl']:+.2f} P&L over {closed} closed "
                           f"({(s['win_rate'] or 0):.0%} win-rate)." + _losing_note(closed)),
                "tag": f"direction:{dirn}",
                "n": closed,
                "confidence": _confidence(closed),
            })

    # 3. A symbol repeatedly bleeding.
    for sym, s in stats["by_symbol"].items():
        closed = s["wins"] + s["losses"]
        if closed >= _MIN_TRADES and (s["win_rate"] is not None and s["win_rate"] < 0.34) \
                and (s["avg_pnl"] or 0) < 0:
            findings.append({
                "severity": "warn" if closed >= _MIN_FINDING_WARN else "info",
                "title": f"{sym} bleeds repeatedly",
                "detail": (f"{sym} won {s['win_rate']:.0%} of {closed} closed swing trades, "
                           f"avg {s['avg_pnl']:+.2f}." + _losing_note(closed)),
                "tag": f"symbol:{sym}",
                "n": closed,
                "confidence": _confidence(closed),
            })

    # 4. Whole book underwater on a real sample (advisory at low n).
    o = stats["overall"]
    closed = o["wins"] + o["losses"]
    if closed >= _MIN_TRADES and (o["total_pnl"] or 0) < 0 \
            and (o["win_rate"] is not None and o["win_rate"] < 0.5):
        findings.append({
            "severity": "warn" if closed >= _MIN_FINDING_WARN else "info",
            "title": "Swing book net negative",
            "detail": (f"realized {o['total_pnl']:+.2f} over {closed} closed swing trades "
                       f"({o['win_rate']:.0%} win-rate)." + _losing_note(closed)),
            "tag": "overall",
            "n": closed,
            "confidence": _confidence(closed),
        })

    if not findings:
        findings.append({
            "severity": "info",
            "title": "No systematic swing mistakes detected",
            "detail": "Not enough closed swing trades, or no bucket/symbol cleared a red-flag bar.",
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
        n = n_by_tag.get(finding, 0) if n is None else n
        actionable = n >= _MIN_SUGGEST
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
            "confidence": _confidence(n),
            "actionable": actionable,
        })

    # A bucket / the whole book losing -> bank gains sooner and tighten the
    # give-back so swing losers are cut faster. These reference the real swing
    # managed-exit knobs.
    losing = [t for t in tags
              if t == "overall" or t.startswith("bucket:") or t.startswith("symbol:")]
    if losing:
        worst = max(losing, key=lambda t: n_by_tag.get(t, 0))
        n = n_by_tag.get(worst, 0)
        cur_take = float(getattr(settings, "swing_profit_take_pct", 20.0))
        add("swing_profit_take_pct", cur_take, _clamp(cur_take - 4.0, 8.0, 40.0),
            "Swing trades gave back gains: take profit a little earlier to lock in winners.",
            worst, n=n)
        cur_trail = float(getattr(settings, "swing_trail_pct", 8.0))
        add("swing_trail_pct", cur_trail, _clamp(cur_trail - 1.5, 4.0, 15.0),
            "Tighten the trailing-stop give-back so swing losers are cut faster.",
            worst, n=n)

    return out


# --------------------------------------------------------------------------- #
# Narrative
# --------------------------------------------------------------------------- #
def _template_summary(review_date: str, stats: dict, findings: list[dict],
                      suggestions: list[dict]) -> str:
    o = stats["overall"]
    closed = o["wins"] + o["losses"]
    parts = [
        f"Swing review {review_date}: {stats['n_closed']} closed swing trades "
        f"({o['wins']}W/{o['losses']}L"
        + (f", {o['win_rate']:.0%} win-rate" if o["win_rate"] is not None else "")
        + f"), realized {o['total_pnl']:+.2f}."
    ]
    if stats["n_open"]:
        op = stats.get("open_pnl")
        parts.append(
            f"{stats['n_open']} open"
            + (f" (marked {op:+.2f})." if op is not None else " (unmarked).")
        )
    # Best / worst bucket.
    bks = [(b, v) for b, v in stats["by_bucket"].items()
           if b != "unknown" and (v["wins"] + v["losses"])]
    if bks:
        bks.sort(key=lambda x: x[1]["avg_pnl"] if x[1]["avg_pnl"] is not None else 0)
        worst, best = bks[0], bks[-1]
        if best[0] != worst[0]:
            parts.append(
                f"Best bucket: {best[0]} (avg {best[1]['avg_pnl']:+.2f}); "
                f"worst: {worst[0]} (avg {worst[1]['avg_pnl']:+.2f}).")
    top = [f for f in findings if f["tag"] != "none"][:2]
    if top:
        parts.append("Flags: " + "; ".join(f["title"] for f in top) + ".")
    if suggestions:
        parts.append("Suggested tweaks: " + ", ".join(
            f"{s['param']} {s['current']}→{s['proposed']}" for s in suggestions[:3]) + ".")
    else:
        parts.append("No parameter changes suggested.")
    return " ".join(parts)


_LLM_PROMPT = """You are the coach for a paper SWING (weeks-to-months) trading
sleeve. Below is a JSON review: realized win-rate and average P&L overall and by
strategist bucket / direction / symbol, detected findings, and proposed config
tweaks. Write a tight 3-5 sentence narrative for the trader: how the swing book
is doing, the single clearest systematic mistake, and which one or two parameter
tweaks matter most. Plain prose, no markdown headers, no preamble, only facts
from the JSON.

DATA:
{data}
"""


def _llm_summary(llm, payload: dict) -> tuple[str, str] | None:
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
        log.warning("swing-review LLM summary failed (%s) — using template", exc)
        return None


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def review_swing(sqlite, duck, settings, *, review_date: str | None = None,
                 llm=None) -> dict:
    """Review the swing sleeve: pair its round-trips, mine win-rate/P&L overall
    and by bucket/direction/symbol, derive advisory findings + suggestions,
    persist one ``swing_review`` row, and return the full review dict. Blocking;
    dispatch via run_in_executor. ``duck`` is used to mark open lots."""
    if review_date is None:
        review_date = _today_et()

    try:
        trades = list_trades(sqlite, sleeve="swing", duck=duck)
    except Exception:
        log.warning("swing review: list_trades failed", exc_info=True)
        trades = []

    if not trades:
        log.info("swing review: no swing trades — no-op")
        return {
            "review_date": review_date,
            "stats": {}, "findings": [], "suggestions": [],
            "summary": "No swing trades booked yet — nothing to review.",
            "model": "deterministic",
            "no_data": True,
        }

    buckets = _bucket_map(sqlite)
    stats = _compute_stats(trades, buckets)
    findings = _derive_findings(stats)
    suggestions = _derive_suggestions(stats, findings, settings)

    payload = {"review_date": review_date, "stats": stats,
               "findings": findings, "suggestions": suggestions}
    llm_out = _llm_summary(llm, payload)
    if llm_out is not None:
        summary, model = llm_out
    else:
        summary = _template_summary(review_date, stats, findings, suggestions)
        model = "deterministic"

    result = {
        "review_date": review_date,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": stats,
        "findings": findings,
        "suggestions": suggestions,
        "summary": summary,
        "model": model,
    }

    try:
        sqlite.execute(
            "INSERT OR REPLACE INTO swing_review "
            "(review_date, created_at, stats, findings, suggestions, summary, model) "
            "VALUES (?,?,?,?,?,?,?)",
            [review_date, result["created_at"], json.dumps(stats),
             json.dumps(findings), json.dumps(suggestions), summary, model],
        )
    except Exception:
        log.warning("swing_review persist failed for %s", review_date, exc_info=True)

    # Also drop a human-readable learnings file (best-effort, never fatal).
    try:
        from app.trading.learnings import write_learnings_file
        path = write_learnings_file(result, sleeve="swing", data_dir=settings.data_dir)
        if path is not None:
            result["learnings_file"] = str(path)
    except Exception:
        log.debug("swing learnings file skipped", exc_info=True)

    log.info("swing review %s: %d closed, %d open, %d findings, %d suggestions (model %s)",
             review_date, stats["n_closed"], stats["n_open"],
             len([f for f in findings if f["tag"] != "none"]), len(suggestions), model)
    return result


def latest_swing_review(sqlite, review_date: str | None = None) -> dict:
    """Read a persisted swing review row (latest, or a specific date). {} if none."""
    if review_date:
        row = sqlite.fetchone(
            "SELECT * FROM swing_review WHERE review_date = ?", [review_date])
    else:
        row = sqlite.fetchone(
            "SELECT * FROM swing_review ORDER BY review_date DESC LIMIT 1")
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
