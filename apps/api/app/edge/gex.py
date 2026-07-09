"""Self-computed dealer gamma exposure (PLAN §6 #8).

The CBOE delayed-quotes options JSON is UNOFFICIAL and schema-fragile, so this
is the one isolated adapter the plan demands: every pull is schema-validated
field by field, and any mismatch degrades to "no snapshot" with a log line —
nothing downstream ever sees a partially-parsed chain.

Convention (gex-tracker formula reference): dealers are long calls / short
puts, so per contract
    GEX($ per 1% move) = gamma * OI * 100 * spot^2 * 0.01   (calls +, puts −)
Flip = strike where cumulative net GEX crosses zero; call/put walls are the
largest positive / most negative net-GEX strikes. Expiries beyond 45 days are
ignored (front gamma dominates dealer hedging).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from app.db.duck import DuckStore
from app.ingest.http import HttpClient
from app.profile import gated

log = logging.getLogger("market.edge.gex")

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"

# e.g. SPX260620C05000000 / SPXW260620P04500000
_OPT_RE = re.compile(r"^[A-Z]+(\d{6})([CP])(\d{8})$")

_MAX_DTE_DAYS = 45


def _parse_chain(payload: dict, symbol: str) -> dict | None:
    """Validate + reduce the raw chain to a per-strike net-GEX profile."""
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    options = data.get("options")
    spot = data.get("current_price") or data.get("close")
    if not isinstance(options, list) or not isinstance(spot, (int, float)) or spot <= 0:
        return None

    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=_MAX_DTE_DAYS)
    by_strike: dict[float, float] = {}
    parsed = 0
    for o in options:
        if not isinstance(o, dict):
            continue
        m = _OPT_RE.match(str(o.get("option", "")))
        gamma, oi = o.get("gamma"), o.get("open_interest")
        if not m or not isinstance(gamma, (int, float)) or not isinstance(oi, (int, float)):
            continue
        try:
            expiry = datetime.strptime(m.group(1), "%y%m%d").date()
        except ValueError:
            continue
        if expiry < today or expiry > horizon or oi <= 0 or gamma <= 0:
            continue
        strike = int(m.group(3)) / 1000
        gex = gamma * oi * 100 * spot * spot * 0.01
        if m.group(2) == "P":
            gex = -gex
        by_strike[strike] = by_strike.get(strike, 0.0) + gex
        parsed += 1
    if parsed < 50:  # a real SPX/SPY chain has thousands of contracts
        log.warning("gex %s: only %s parseable contracts — schema drift?", symbol, parsed)
        return None

    strikes = sorted(by_strike)
    cum, flip = 0.0, None
    prev_cum = None
    for k in strikes:
        cum += by_strike[k]
        if prev_cum is not None and prev_cum < 0 <= cum:
            flip = k
        prev_cum = cum
    pos = {k: v for k, v in by_strike.items() if v > 0}
    neg = {k: v for k, v in by_strike.items() if v < 0}
    call_wall = max(pos, key=pos.get) if pos else None
    put_wall = min(neg, key=neg.get) if neg else None

    # Profile trimmed to ±15% of spot for the UI.
    lo, hi = spot * 0.85, spot * 1.15
    profile = [
        {"strike": k, "gex": round(by_strike[k] / 1e9, 3)}
        for k in strikes if lo <= k <= hi
    ]
    return {
        "spot": float(spot),
        "total_gex": sum(by_strike.values()),
        "flip": flip,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "profile": profile,
        "contracts": parsed,
    }


class GexAdapter:
    def __init__(self, duck: DuckStore, http: HttpClient, symbols: list[str]) -> None:
        self._duck = duck
        self._http = http
        self._symbols = symbols

    @gated("cboe", default=lambda: 0)
    async def run(self) -> int:
        """Cboe Market Data Policy bars auto-extraction/redistribution of the
        delayed-options-chain endpoint this whole panel depends on — gated
        off in MARKET_PROFILE=commercial (no free substitute for GEX)."""
        stored = 0
        for symbol in self._symbols:
            try:
                payload = await self._http.get_json(
                    CBOE_URL.format(symbol=symbol), conditional=False
                )
            except Exception as exc:
                log.warning("gex %s fetch failed: %s", symbol, exc)
                continue
            result = _parse_chain(payload, symbol)
            if result is None:
                continue
            now = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
            self._duck.execute(
                "INSERT OR REPLACE INTO gex_snapshots "
                "(symbol, ts, spot, total_gex, flip, call_wall, put_wall, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    symbol, now, result["spot"], result["total_gex"],
                    result["flip"], result["call_wall"], result["put_wall"],
                    json.dumps({"profile": result["profile"],
                                "contracts": result["contracts"]}),
                ],
            )
            stored += 1
            log.info(
                "gex %s: total %.1f$bn, flip %s, call wall %s, put wall %s",
                symbol, result["total_gex"] / 1e9, result["flip"],
                result["call_wall"], result["put_wall"],
            )
        return stored
