"""Day-trade signal math — pure functions over 1-minute bars.

Two setups, the order the user asked for:
  * momentum / breakout — price pushing the top of its recent intraday range
    with a real move behind it (ride strength), and
  * mean-reversion — price stretched far from intraday VWAP (fade the extreme).

Plus a major-news gate (only the biggest, freshest, multi-outlet items move the
needle) and a portfolio-conflict check so the fast trader never piles into a
name the long-term book is already heavy in. No I/O here — trivially testable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _vwap(bars: list[dict]) -> float | None:
    num = sum((b.get("vw") or b["c"]) * b.get("v", 0.0) for b in bars)
    den = sum(b.get("v", 0.0) for b in bars)
    if den:
        return num / den
    return bars[-1]["c"] if bars else None


def intraday_signal(
    bars: list[dict],
    *,
    breakout_buffer_pct: float,
    momentum_min_pct: float,
    reversion_z: float,
) -> dict:
    """Single setup from recent 1-min bars. direction in {buy, sell, none};
    strength ~1-3. 'sell' means fade/exit, not go short."""
    none = {"kind": "none", "direction": "none", "strength": 0.0,
            "last": None, "vwap": None, "z": 0.0, "sigma": None,
            "win_high": None, "win_low": None, "detail": "insufficient bars"}
    if len(bars) < 5:
        return none
    closes = [b["c"] for b in bars]
    last = closes[-1]
    win_high = max(b["h"] for b in bars)
    win_low = min(b["l"] for b in bars)
    first = bars[0].get("o") or closes[0]
    intraday_ret = (last - first) / first * 100.0 if first else 0.0
    vwap = _vwap(bars) or last
    mean = sum(closes) / len(closes)
    std = (sum((c - mean) ** 2 for c in closes) / len(closes)) ** 0.5
    z = (last - vwap) / std if std else 0.0

    # Structure levels the bracket needs (σ = intraday close stdev), exposed so
    # the day trader can size vol-normalized, structure-aware stops/targets.
    struct = {"sigma": round(std, 4), "win_high": round(win_high, 4),
              "win_low": round(win_low, 4)}

    # momentum / breakout: near the window high with a real intraday move.
    near_high = last >= win_high * (1 - breakout_buffer_pct / 100.0)
    if near_high and intraday_ret >= momentum_min_pct and last >= vwap:
        return {"kind": "momentum", "direction": "buy",
                "strength": round(min(3.0, intraday_ret / max(momentum_min_pct, 1e-6)), 2),
                "last": last, "vwap": round(vwap, 4), "z": round(z, 2), **struct,
                "detail": f"breakout +{intraday_ret:.2f}% intraday, pressing {win_high:.2f} high"}

    # mean reversion: stretched from VWAP.
    if z <= -reversion_z:
        return {"kind": "reversion", "direction": "buy",
                "strength": round(min(3.0, abs(z) / reversion_z), 2),
                "last": last, "vwap": round(vwap, 4), "z": round(z, 2), **struct,
                "detail": f"oversold {z:.1f}σ below VWAP ({win_low:.2f} low) — fade up"}
    if z >= reversion_z:
        return {"kind": "reversion", "direction": "sell",
                "strength": round(min(3.0, abs(z) / reversion_z), 2),
                "last": last, "vwap": round(vwap, 4), "z": round(z, 2), **struct,
                "detail": f"overbought {z:.1f}σ above VWAP — take profit / fade"}

    return {"kind": "none", "direction": "none", "strength": 0.0,
            "last": last, "vwap": round(vwap, 4), "z": round(z, 2), **struct,
            "detail": "no setup"}


def major_news(duck, symbol: str, *, max_age_min: int, min_abs_score: float,
               min_outlets: int) -> dict | None:
    """The single biggest fresh, corroborated headline for a symbol, or None.
    Used only as an override — most ticks have nothing here."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=max_age_min)
    row = duck.fetchone(
        "SELECT title, score, outlets, published FROM news_items "
        "WHERE upper(symbol) = ? AND score IS NOT NULL AND published >= ? "
        "AND abs(score) >= ? AND coalesce(outlets, 1) >= ? "
        "ORDER BY abs(score) DESC, outlets DESC LIMIT 1",
        [symbol.upper(), cutoff, min_abs_score, min_outlets],
    )
    if not row:
        return None
    return {"title": row[0], "score": float(row[1]),
            "outlets": int(row[2] or 1), "published": str(row[3])}


def portfolio_conflict(
    *,
    symbol: str,
    side: str,
    swing_value: float,
    equity: float,
    swing_heavy_pct: float = 8.0,
) -> str | None:
    """Reason this day trade conflicts with the book, or None. A day BUY into a
    name the swing sleeve already holds heavily is redundant concentration —
    there's 'no point taking the trade' (the user's words). Exits are always ok."""
    if side != "buy":
        return None
    if equity > 0 and swing_value > 0 and (swing_value / equity * 100.0) >= swing_heavy_pct:
        return (f"swing book already holds {swing_value / equity * 100.0:.1f}% of equity in "
                f"{symbol} (>= {swing_heavy_pct:.0f}%) — skip, would just concentrate")
    return None
