"""Multi-timeframe POSTURE engine for the swing sleeve (the user's top-down method).

Formalizes "run weekly/monthly trend, rotate out when the higher timeframe
weakens" into a single posture read that BOTH the strategist and the swing bot
consume — so the whole book runs one coherent strategy:

  * MONTHLY = permission / regime → the POSTURE (how much gross to deploy, risk-on
    vs defensive). Driven by SPY vs its 10-month MA + the macro_composite regime +
    breadth (% > 200DMA) + the net-liquidity trend.
  * WEEKLY  = trend + leadership → which names to hold, via the RRG quadrant
    lifecycle (leading/improving = own, weakening = trim, lagging = exit) confirmed
    by the weekly (30-wk MA) trend.
  * DAILY   = timing + risk → entries, stops, trims (the swing bot's exit loop).

Reads DuckDB (ts_price daily closes, macro_composite, ts_macro series, RRG); the
per-name lifecycle classifier is pure. Defensive throughout — missing data yields
a NEUTRAL posture rather than an exception.
"""
from __future__ import annotations

import logging

log = logging.getLogger("market.edge.posture")

# Posture → fraction of the sleeve's gross to actually deploy (the macro dial).
_GROSS_FACTOR = {"aggressive": 1.0, "neutral": 0.65, "defensive": 0.3}


def _closes(duck, symbol: str, days: int = 1600):
    """Ascending [(date, close)] for a symbol, last ~`days` calendar days."""
    try:
        rows = duck.fetchall(
            "SELECT ts, close FROM ts_price WHERE symbol = ? AND close IS NOT NULL "
            "ORDER BY ts ASC",
            [symbol],
        )
    except Exception:
        return []
    return [(r[0], float(r[1])) for r in rows[-days:]] if rows else []


def _resample_last(closes, period: str):
    """Period-end closes via pandas (period: 'ME' month-end, 'W-FRI' weekly)."""
    try:
        import pandas as pd
    except Exception:
        return None
    if not closes:
        return None
    s = pd.Series([c for _, c in closes],
                  index=pd.to_datetime([t for t, _ in closes]))
    return s.resample(period).last().dropna()


def _trend_state(series, ma_len: int) -> dict:
    """Trend of a resampled price series vs its `ma_len` MA: up / down / flat,
    with whether the MA itself is rising (the slope confirms the cross)."""
    if series is None or len(series) < ma_len + 2:
        return {"state": "unknown", "detail": "insufficient history"}
    ma = series.rolling(ma_len).mean()
    last = float(series.iloc[-1])
    m_now, m_prev = float(ma.iloc[-1]), float(ma.iloc[-3])
    above = last >= m_now
    rising = m_now >= m_prev
    if above and rising:
        state = "up"
    elif (not above) and (not rising):
        state = "down"
    else:
        state = "flat"
    return {"state": state, "last": round(last, 2), "ma": round(m_now, 2),
            "ma_rising": rising,
            "detail": f"{'above' if above else 'below'} {ma_len}-bar MA, MA "
                      f"{'rising' if rising else 'falling'}"}


def _series_trend(duck, series_id: str, lookback: int = 60) -> dict:
    """Latest value + simple rising/falling trend of a ts_macro series."""
    try:
        rows = duck.fetchall(
            "SELECT value FROM ts_macro WHERE series_id = ? AND value IS NOT NULL "
            "ORDER BY ts ASC", [series_id])
    except Exception:
        rows = []
    vals = [float(r[0]) for r in rows][-lookback:] if rows else []
    if len(vals) < 5:
        return {"value": None, "trend": "unknown"}
    recent = sum(vals[-5:]) / 5
    prior = sum(vals[:5]) / 5
    trend = "rising" if recent > prior * 1.001 else "falling" if recent < prior * 0.999 else "flat"
    return {"value": round(vals[-1], 2), "trend": trend}


def compute_posture(duck, *, benchmark: str = "SPY") -> dict:
    """The book's macro POSTURE: aggressive / neutral / defensive + a gross factor.

    Scores the monthly + weekly trend of the benchmark, the macro_composite regime,
    breadth, and net liquidity into one posture the strategist + swing bot share."""
    closes = _closes(duck, benchmark)
    monthly = _trend_state(_resample_last(closes, "ME"), 10)   # classic 10-month MA
    weekly = _trend_state(_resample_last(closes, "W-FRI"), 30)  # 30-week MA

    regime, score = "unknown", None
    try:
        row = duck.fetchone("SELECT regime, score FROM macro_composite ORDER BY ts DESC LIMIT 1")
        if row:
            regime, score = str(row[0]), float(row[1])
    except Exception:
        pass
    breadth = _series_trend(duck, "BREADTH_PCT_ABOVE_200DMA")
    netliq = _series_trend(duck, "NET_LIQUIDITY")

    # --- score the posture (the monthly tide dominates) --------------------
    pts, reasons = 0, []
    if monthly["state"] == "up":
        pts += 2; reasons.append("monthly trend up (SPY > 10-mo MA)")
    elif monthly["state"] == "down":
        pts -= 2; reasons.append("monthly trend DOWN (SPY < 10-mo MA) — defensive tide")
    if weekly["state"] == "up":
        pts += 1
    elif weekly["state"] == "down":
        pts -= 1; reasons.append("weekly trend down")
    rl = regime.lower()
    if "stress" in rl or "risk-off" in rl or "risk_off" in rl:
        pts -= 2; reasons.append(f"regime {regime}")
    elif "risk-on" in rl or "risk_on" in rl or "expansion" in rl:
        pts += 1; reasons.append(f"regime {regime}")
    bv = breadth.get("value")
    if bv is not None:
        if bv >= 60:
            pts += 1; reasons.append(f"breadth healthy ({bv:.0f}% > 200DMA)")
        elif bv < 40:
            pts -= 1; reasons.append(f"breadth weak ({bv:.0f}% > 200DMA)")
    if netliq.get("trend") == "rising":
        pts += 1; reasons.append("net liquidity rising")
    elif netliq.get("trend") == "falling":
        pts -= 1; reasons.append("net liquidity falling")

    state = "aggressive" if pts >= 3 else "defensive" if pts <= -1 else "neutral"
    return {
        "state": state,
        "gross_factor": _GROSS_FACTOR[state],
        "score": pts,
        "monthly": monthly,
        "weekly": weekly,
        "regime": regime,
        "composite_score": score,
        "breadth": breadth,
        "net_liquidity": netliq,
        "reasons": reasons or ["mixed signals — neutral"],
        "allow_single_names": state == "aggressive",
        "favor_safe_assets": state == "defensive",
    }


def name_lifecycle(quadrant: str | None, monthly_state: str, weekly_state: str,
                   posture_state: str) -> dict:
    """Per-name RRG lifecycle → {action, trend_strength}. action in
    {enter, hold, trim, exit}. The monthly posture is an override: in a defensive
    tide even a 'leading' name is at most held, never added; a broken weekly trend
    forces a trim/exit regardless of quadrant."""
    q = (quadrant or "").lower()
    # Defensive tide: shed everything not clearly leading; never add.
    if posture_state == "defensive":
        if q == "leading" and weekly_state != "down":
            return {"action": "hold", "trend_strength": 0.5,
                    "reason": "leading but defensive tide — hold, don't add"}
        return {"action": "exit" if q in ("lagging", "") else "trim",
                "trend_strength": 0.2, "reason": f"defensive tide, RRG {q or 'n/a'}"}
    # Normal / aggressive tide: drive off the quadrant, confirmed by weekly trend.
    if q == "leading":
        action = "hold" if weekly_state != "down" else "trim"
        ts = 1.0 if weekly_state == "up" else 0.7
        return {"action": action, "trend_strength": ts, "reason": f"RRG leading, weekly {weekly_state}"}
    if q == "improving":
        action = "enter" if (weekly_state != "down" and monthly_state != "down") else "hold"
        return {"action": action, "trend_strength": 0.6, "reason": f"RRG improving, weekly {weekly_state}"}
    if q == "weakening":
        return {"action": "trim", "trend_strength": 0.4, "reason": "RRG weakening — take partial profit"}
    if q == "lagging":
        return {"action": "exit", "trend_strength": 0.1, "reason": "RRG lagging — rotate out"}
    # No RRG read (single name not in the sector grid): lean on weekly trend.
    if weekly_state == "down":
        return {"action": "trim", "trend_strength": 0.3, "reason": "no RRG, weekly down"}
    return {"action": "hold", "trend_strength": 0.5, "reason": "no RRG, weekly ok"}
