"""Narrative-vs-money divergence engine — Phase 15.

The thesis (PLAN §15): informed money positions *against* the official
narrative ahead of the public. The classic shape the user described — "the
president says there's no war, cash gets shorted into the weekend, then the
weekend confirms the war" — is the same phenomenon as "White House-adjacent
wallets front-run major news on Polymarket". Two venues, one signal.

This engine fuses three legs into one 0-100 "divergence" score:

  1. NARRATIVE  — tone of recent geopolitical/major-news headlines (the FinBERT
     score the news pipeline already stamps on every item). + = calm/reassuring,
     - = fear/escalation. This is the "what is being said" leg.
  2. RISK-OFF   — a basket of cross-asset hedging proxies (defense ITA, oil USO,
     gold GLD, long bonds TLT, dollar UUP up; SPY/QQQ down; VIX up), each
     z-scored on its recent 2-day move and signed so "up = bracing". This is the
     "what equity/bond money is doing" leg.
  3. PM PRESSURE — volume-weighted drift of tracked Polymarket geopolitical odds
     toward escalation (ingest/polymarket.py). This is the "what the on-chain
     prediction crowd is betting" leg.

The score is high when money/markets are bracing (legs 2+3) WHILE the narrative
is calm (leg 1) — i.e. someone is hedging against news the public hasn't been
told. The forward-outcome loop (weekend_gaps) is what keeps this honest: every
flagged divergence is later checked against the realised SPY weekend gap, so the
panel shows a measured track record, not a vibe.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ingest.polymarket import GEO_KEYWORDS, polymarket_summary
from app.ingest.prices import ensure_daily_history
from app.ws.hub import ConnectionManager

log = logging.getLogger("market.edge.divergence")

DIVERGENCE_TOPIC = "divergence"

# (symbol, asset_class, direction) — direction +1 means "a rising price is
# risk-off / a hedge"; -1 means "a falling price is risk-off". VIX is handled
# separately from ts_macro (it isn't a tradable bar in ts_price here).
RISKOFF_PROXIES: list[tuple[str, str, int]] = [
    ("SPY", "equity", -1),   # the thing being de-risked
    ("QQQ", "equity", -1),
    ("ITA", "equity", +1),   # aerospace & defense
    ("USO", "equity", +1),   # crude oil
    ("GLD", "equity", +1),   # gold
    ("TLT", "equity", +1),   # 20y+ Treasuries (flight to safety)
    ("UUP", "equity", +1),   # US dollar bull (haven bid)
]

_WINDOW = 70   # trading-day window for the z baseline
_MOVE_DAYS = 2  # cumulative move that defines "the recent positioning"


def _zscore_move(duck: DuckStore, symbol: str) -> tuple[float, float] | None:
    """(recent 2-day return, z of that move vs its `_WINDOW` history)."""
    rows = duck.fetchall(
        "SELECT close FROM ts_price WHERE source = 'yahoo' AND symbol = ? "
        "ORDER BY ts DESC LIMIT ?",
        [symbol, _WINDOW + _MOVE_DAYS + 1],
    )
    closes = [float(r[0]) for r in rows if r[0] is not None][::-1]
    if len(closes) < 30:
        return None
    moves = [
        closes[i] / closes[i - _MOVE_DAYS] - 1.0
        for i in range(_MOVE_DAYS, len(closes))
        if closes[i - _MOVE_DAYS]
    ]
    if len(moves) < 20:
        return None
    recent = moves[-1]
    hist = moves[:-1]
    mu = sum(hist) / len(hist)
    var = sum((x - mu) ** 2 for x in hist) / len(hist)
    sd = var ** 0.5
    if sd <= 0:
        return None
    return recent, (recent - mu) / sd


def _vix_move_z(duck: DuckStore) -> tuple[float, float] | None:
    rows = duck.fetchall(
        "SELECT value FROM ts_macro WHERE series_id = 'VIX' ORDER BY ts DESC LIMIT ?",
        [_WINDOW + _MOVE_DAYS + 1],
    )
    vals = [float(r[0]) for r in rows if r[0] is not None][::-1]
    if len(vals) < 30:
        return None
    moves = [vals[i] - vals[i - _MOVE_DAYS] for i in range(_MOVE_DAYS, len(vals))]
    if len(moves) < 20:
        return None
    recent, hist = moves[-1], moves[:-1]
    mu = sum(hist) / len(hist)
    sd = (sum((x - mu) ** 2 for x in hist) / len(hist)) ** 0.5
    if sd <= 0:
        return None
    return recent, (recent - mu) / sd


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class DivergencePipeline:
    def __init__(
        self,
        duck: DuckStore,
        sqlite: SqliteStore,
        http,
        hub: ConnectionManager,
        *,
        news_lookback_hours: int = 48,
        pm_lookback_hours: int = 24,
        pm_move_scale: float = 5.0,
    ) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._http = http
        self._hub = hub
        self._news_lookback = news_lookback_hours
        self._pm_lookback = pm_lookback_hours
        self._pm_scale = pm_move_scale

    # ---------------------------------------------------------------- legs

    async def _ensure_proxies(self) -> None:
        for symbol, asset_class, _ in RISKOFF_PROXIES:
            try:
                await ensure_daily_history(self._http, self._duck, symbol, asset_class)
            except Exception as exc:  # one stale proxy never sinks the basket
                log.warning("divergence: proxy %s ensure failed: %s", symbol, exc)

    def _riskoff(self) -> tuple[float | None, list[dict]]:
        legs: list[dict] = []
        contribs: list[float] = []
        for symbol, _ac, direction in RISKOFF_PROXIES:
            mz = _zscore_move(self._duck, symbol)
            if mz is None:
                continue
            ret, z = mz
            contribs.append(z * direction)
            legs.append({
                "symbol": symbol, "ret_2d": round(ret * 100, 2),
                "z": round(z, 2), "signed": round(z * direction, 2),
            })
        vz = _vix_move_z(self._duck)
        if vz is not None:
            ret, z = vz
            contribs.append(z)  # VIX up = risk-off (direction +1)
            legs.append({"symbol": "VIX", "ret_2d": round(ret, 2),
                         "z": round(z, 2), "signed": round(z, 2)})
        if not contribs:
            return None, legs
        return sum(contribs) / len(contribs), legs

    def _narrative(self) -> tuple[float | None, int, list[str]]:
        """Mean FinBERT tone of recent geopolitical headlines (+ = calm)."""
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            hours=self._news_lookback
        )
        rows = self._duck.fetchall(
            "SELECT title, score FROM news_items "
            "WHERE published >= ? AND score IS NOT NULL "
            "ORDER BY published DESC LIMIT 400",
            [cutoff],
        )
        scores: list[float] = []
        samples: list[str] = []
        for title, score in rows:
            t = (title or "").lower()
            if not any(k in t for k in GEO_KEYWORDS):
                continue
            scores.append(float(score))
            if len(samples) < 5:
                samples.append(title)
        if len(scores) < 3:
            return None, len(scores), samples
        return sum(scores) / len(scores), len(scores), samples

    def _pm_pressure(self) -> tuple[float | None, list[dict]]:
        markets = polymarket_summary(self._duck, self._pm_lookback)
        directed = [m for m in markets if m["escalation_sign"] and m["move"] is not None]
        if not directed:
            return None, markets[:6]
        num = den = 0.0
        for m in directed:
            w = (m["volume"] or 0.0) ** 0.5 + 1.0  # damp the volume weighting
            num += m["move"] * m["escalation_sign"] * w
            den += w
        raw = num / den if den else 0.0
        return _clamp(raw * self._pm_scale, -1.0, 1.0), markets[:6]

    def _regime(self) -> str:
        row = self._duck.fetchone(
            "SELECT regime FROM macro_composite ORDER BY ts DESC LIMIT 1"
        )
        return row[0] if row and row[0] else "unknown"

    # ---------------------------------------------------------------- run

    async def run(self) -> dict:
        await self._ensure_proxies()
        riskoff_z, legs = self._riskoff()
        narrative, n_news, samples = self._narrative()
        pm_pressure, pm_markets = self._pm_pressure()
        regime = self._regime()

        money_signal = _clamp((riskoff_z or 0.0) / 3.0, 0.0, 1.0)
        pm_signal = _clamp(pm_pressure or 0.0, 0.0, 1.0)
        calm = _clamp(narrative or 0.0, 0.0, 1.0)
        brace = 0.6 * money_signal + 0.4 * pm_signal
        score = round(100.0 * min(1.0, brace * (1.0 + 0.6 * calm)), 1)

        headline = self._headline(riskoff_z, narrative, pm_pressure, calm)
        detail = {
            "riskoff_legs": legs,
            "narrative": {"tone": round(narrative, 3) if narrative is not None else None,
                          "n_headlines": n_news, "samples": samples},
            "polymarket": {"pressure": round(pm_pressure, 3) if pm_pressure is not None else None,
                           "markets": pm_markets},
            "signals": {"money": round(money_signal, 3), "pm": round(pm_signal, 3),
                        "calm": round(calm, 3), "brace": round(brace, 3)},
        }
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0, tzinfo=None)
        self._duck.execute(
            "INSERT OR REPLACE INTO divergence_snapshots "
            "(ts, score, narrative, riskoff_z, pm_pressure, regime, headline, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [now, score, narrative, riskoff_z, pm_pressure, regime, headline,
             json.dumps(detail)],
        )
        payload = {
            "type": "divergence", "ts": now.isoformat(), "score": score,
            "narrative": narrative, "riskoff_z": riskoff_z,
            "pm_pressure": pm_pressure, "regime": regime, "headline": headline,
        }
        try:
            await self._hub.broadcast(DIVERGENCE_TOPIC, payload)
        except Exception:
            pass
        log.info("divergence sweep: score=%s riskoff_z=%s narrative=%s pm=%s",
                 score, _r(riskoff_z), _r(narrative), _r(pm_pressure))
        return {"score": score, "headline": headline, **payload}

    @staticmethod
    def _headline(riskoff_z, narrative, pm_pressure, calm) -> str:
        parts = []
        if riskoff_z is not None:
            if riskoff_z >= 0.5:
                parts.append(f"cross-asset money is bracing (risk-off z {riskoff_z:+.1f})")
            elif riskoff_z <= -0.5:
                parts.append(f"money is risk-ON (z {riskoff_z:+.1f})")
            else:
                parts.append(f"money is neutral (z {riskoff_z:+.1f})")
        if narrative is not None:
            mood = "calm" if narrative > 0.1 else ("fearful" if narrative < -0.1 else "mixed")
            parts.append(f"geopolitical news tone is {mood} ({narrative:+.2f})")
        if pm_pressure is not None and abs(pm_pressure) >= 0.05:
            parts.append(
                f"Polymarket drifting toward {'escalation' if pm_pressure > 0 else 'de-escalation'}"
                f" ({pm_pressure:+.2f})"
            )
        if not parts:
            return "warming up — not enough proxy/news/market history yet"
        lead = "⚠ Narrative–money divergence: " if calm > 0.2 and (riskoff_z or 0) > 0.5 else ""
        return lead + "; ".join(parts) + "."


def _r(v):
    return round(v, 2) if isinstance(v, (int, float)) else v


# -------------------------------------------------------------- read helpers


def latest_divergence(duck: DuckStore) -> dict | None:
    row = duck.fetchone(
        "SELECT ts, score, narrative, riskoff_z, pm_pressure, regime, headline, detail "
        "FROM divergence_snapshots ORDER BY ts DESC LIMIT 1"
    )
    if row is None:
        return None
    return {
        "ts": str(row[0]), "score": float(row[1]) if row[1] is not None else None,
        "narrative": float(row[2]) if row[2] is not None else None,
        "riskoff_z": float(row[3]) if row[3] is not None else None,
        "pm_pressure": float(row[4]) if row[4] is not None else None,
        "regime": row[5], "headline": row[6],
        "detail": json.loads(row[7]) if row[7] else {},
    }


def divergence_history(duck: DuckStore, days: int = 60) -> list[dict]:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    rows = duck.fetchall(
        "SELECT ts, score, narrative, riskoff_z, pm_pressure FROM divergence_snapshots "
        "WHERE ts >= ? ORDER BY ts",
        [cutoff],
    )
    return [
        {"ts": str(r[0]), "score": float(r[1]) if r[1] is not None else None,
         "narrative": float(r[2]) if r[2] is not None else None,
         "riskoff_z": float(r[3]) if r[3] is not None else None,
         "pm_pressure": float(r[4]) if r[4] is not None else None}
        for r in rows
    ]


def weekend_gaps(duck: DuckStore, limit: int = 10) -> list[dict]:
    """Realised SPY gaps across a non-trading break (the weekend-news outcome
    the divergence score is meant to anticipate). gap_pct = (next open − prior
    close) / prior close, only for sessions ≥2 calendar days apart."""
    rows = duck.fetchall(
        "SELECT ts, open, close FROM ts_price WHERE source = 'yahoo' AND symbol = 'SPY' "
        "ORDER BY ts DESC LIMIT 400"
    )
    bars = [(r[0], r[1], r[2]) for r in rows if r[1] is not None and r[2] is not None][::-1]
    out: list[dict] = []
    for i in range(1, len(bars)):
        prev_ts, _po, prev_close = bars[i - 1]
        cur_ts, cur_open, _cc = bars[i]
        if (cur_ts.date() - prev_ts.date()).days < 2 or not prev_close:
            continue
        out.append({
            "from": str(prev_ts)[:10], "to": str(cur_ts)[:10],
            "prev_close": round(float(prev_close), 2),
            "next_open": round(float(cur_open), 2),
            "gap_pct": round((float(cur_open) - float(prev_close)) / float(prev_close) * 100, 2),
        })
    return out[-limit:][::-1]
