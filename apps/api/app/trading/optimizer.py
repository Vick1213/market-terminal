"""Portfolio optimizer — splits capital between the SWING and DAY sleeves.

Swing (the strategist allocator) is the long-term book and gets the large
majority; the day sleeve (the fast trader) is a small, condition-scaled slice.
The split is driven by stored market conditions only (no network):
  * regime + composite (macro_composite)
  * stress radar (corr cookbook _meta)
  * SPX dealer gamma (gex_snapshots)

Bias: keep the fast sleeve SMALL when conditions are dangerous for intraday
trading (stress lit, short-gamma amplification, risk-off) and allow a touch
more when the tape is calm/risk-on. Always clamped to [day_min, day_max].
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ws.hub import ConnectionManager

log = logging.getLogger("market.trading.optimizer")

OPTIMIZER_TOPIC = "bot"


def compute_split(duck: DuckStore, day_min: float, day_max: float) -> dict:
    """Pure read: market conditions -> {swing_pct, day_pct, regime, reason,
    signals}. day_pct is clamped to [day_min, day_max]."""
    mid = (day_min + day_max) / 2.0
    day = mid
    signals: list[dict] = []
    reasons: list[str] = []

    # regime
    regime = "unknown"
    row = duck.fetchone("SELECT regime, score, ts FROM macro_composite ORDER BY ts DESC LIMIT 1")
    if row and row[0]:
        regime = row[0]
        signals.append({"key": "regime", "value": regime, "detail": f"composite {row[1]}"})
        if regime == "risk-on":
            day += 1.5
            reasons.append("risk-on: a touch more day budget")
        elif regime == "risk-off":
            day -= 1.5
            reasons.append("risk-off: trim the fast sleeve")
        elif regime == "stress":
            day = day_min
            reasons.append("stress regime: day sleeve to the floor")

    # stress radar
    row = duck.fetchone(
        "SELECT detail FROM corr_snapshots WHERE card_id = '_meta' ORDER BY ts DESC LIMIT 1"
    )
    if row and row[0]:
        try:
            stress_on = bool(json.loads(row[0]).get("stress", {}).get("on"))
        except (ValueError, TypeError):
            stress_on = False
        signals.append({"key": "stress_radar", "value": "ON" if stress_on else "off", "detail": ""})
        if stress_on:
            day = min(day, day_min)
            reasons.append("stress radar lit: intraday chop risk — day at floor")

    # SPX dealer gamma
    row = duck.fetchone(
        "SELECT spot, flip FROM gex_snapshots WHERE symbol = '_SPX' ORDER BY ts DESC LIMIT 1"
    )
    if row and row[0] is not None and row[1] is not None:
        short_gamma = float(row[0]) < float(row[1])
        signals.append({"key": "gex", "value": "short" if short_gamma else "long",
                        "detail": f"spot {row[0]:.0f} vs flip {row[1]:.0f}"})
        if short_gamma:
            day -= 1.0
            reasons.append("short gamma: amplified moves — keep the fast sleeve smaller")

    day = max(day_min, min(day_max, round(day, 1)))
    swing = round(100.0 - day, 1)
    return {
        "swing_pct": swing,
        "day_pct": day,
        "regime": regime,
        "reason": "; ".join(reasons) or "neutral conditions — midpoint split",
        "signals": signals,
    }


class PortfolioOptimizer:
    def __init__(
        self,
        duck: DuckStore,
        sqlite: SqliteStore,
        hub: ConnectionManager,
        *,
        day_min: float,
        day_max: float,
    ) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._hub = hub
        self._day_min = day_min
        self._day_max = day_max

    def split(self) -> dict:
        """Current split (does not persist)."""
        return compute_split(self._duck, self._day_min, self._day_max)

    async def run(self) -> dict:
        out = self.split()
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._sqlite.execute(
            "INSERT OR REPLACE INTO optimizer_snapshots (ts, swing_pct, day_pct, regime, reason, detail) "
            "VALUES (?,?,?,?,?,?)",
            [ts, out["swing_pct"], out["day_pct"], out["regime"], out["reason"],
             json.dumps(out["signals"])],
        )
        try:
            await self._hub.broadcast(OPTIMIZER_TOPIC, {"type": "bot", "event": "optimizer"})
        except Exception:
            pass
        log.info("optimizer split: swing %.1f%% / day %.1f%% (%s)",
                 out["swing_pct"], out["day_pct"], out["regime"])
        return out

    def latest(self) -> dict:
        row = self._sqlite.fetchone(
            "SELECT ts, swing_pct, day_pct, regime, reason, detail FROM optimizer_snapshots "
            "ORDER BY ts DESC LIMIT 1"
        )
        if row is None:
            # never run yet — compute live without persisting
            out = self.split()
            out["ts"] = None
            return out
        try:
            signals = json.loads(row["detail"]) if row["detail"] else []
        except (ValueError, TypeError):
            signals = []
        return {
            "ts": row["ts"], "swing_pct": row["swing_pct"], "day_pct": row["day_pct"],
            "regime": row["regime"], "reason": row["reason"], "signals": signals,
        }
