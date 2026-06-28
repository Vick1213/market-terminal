"""Polymarket ingest — Phase 15 leg B (the on-chain front-running venue).

Polymarket is a public prediction market settled on Polygon, so every market's
odds are world-readable with NO API key. We pull, on a slow cadence:

  * Gamma API (gamma-api.polymarket.com/markets) — active markets + their
    current implied probability (outcomePrices). We keep only binary Yes/No
    markets whose question text reads geopolitical/major-news, and tag each
    with an ``escalation_sign`` (+1 if a "Yes" means MORE conflict, -1 if "Yes"
    means de-escalation) so an odds move can be read as risk pressure later.
  * Data API (data-api.polymarket.com/holders) — best-effort holder
    concentration for the tracked markets. A sharp odds move carried by a few
    concentrated wallets is the smart/insider-wallet fingerprint. This endpoint
    shape is undocumented/volatile, so its failure is swallowed — odds history
    (jump detection) is the load-bearing signal and comes from Gamma alone.

Odds snapshots accumulate in ``polymarket_odds`` (one row per market per run,
minute-truncated) — that time series is what the divergence engine and the
alert rules diff to detect pre-news jumps. We cannot de-anonymise a wallet to
"a White House staffer", but we CAN surface the behavioural fingerprint and
cross-reference its timing against the official narrative (edge/divergence.py).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.db.duck import DuckStore
from app.db.sqlite import SqliteStore
from app.ingest.http import HttpClient

log = logging.getLogger("market.ingest.polymarket")

GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
DATA_HOLDERS = "https://data-api.polymarket.com/holders"

# A market is "tracked" if its question mentions any of these. Broad on
# purpose — the divergence engine weights by escalation_sign + volume, so an
# off-topic match that stays sign-0 simply never contributes pressure.
GEO_KEYWORDS = (
    "war", "ceasefire", "truce", "invade", "invasion", "strike", "airstrike",
    "attack", "missile", "nuclear", "troops", "military", "conflict",
    "sanction", "hostage", "annex", "drone", "escalat", "nato", "iran",
    "israel", "gaza", "hezbollah", "ukraine", "russia", "putin", "taiwan",
    "north korea", "venezuela", "tariff", "shutdown",
)
# "Yes" resolves toward MORE danger.
_SIGN_POS = (
    "war", "invade", "invasion", "strike", "airstrike", "attack", "missile",
    "nuclear", "bomb", "annex", "escalat", "drone", "shutdown",
)
# "Yes" resolves toward LESS danger (de-escalation).
_SIGN_NEG = (
    "ceasefire", "truce", "peace", "deal", "agreement", "withdraw",
    "release", "resolution", "end the war", "end of war",
)
# Drop obvious sports/entertainment that share a country keyword (e.g. "Will
# Iran win the World Cup") — they pollute the geopolitical list.
_EXCLUDE = (
    "world cup", "super bowl", "oscar", "grammy", "fifa", "ballon",
    "championship", "playoff", "olympic", "eurovision", "box office",
)


def classify(question: str) -> tuple[bool, int]:
    """(is_tracked, escalation_sign) from a market question."""
    q = question.lower()
    if any(k in q for k in _EXCLUDE):
        return False, 0
    if not any(k in q for k in GEO_KEYWORDS):
        return False, 0
    neg = any(k in q for k in _SIGN_NEG)
    pos = any(k in q for k in _SIGN_POS)
    # De-escalation phrasing wins ties ("ceasefire in the war" => Yes = calmer).
    sign = -1 if neg else (1 if pos else 0)
    return True, sign


def _yes_prob(outcomes: list, prices: list) -> float | None:
    """Implied probability of the 'Yes' leg of a binary market."""
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None
    for name, px in zip(outcomes, prices):
        if str(name).strip().lower() == "yes":
            try:
                return float(px)
            except (TypeError, ValueError):
                return None
    return None


def _loads(val) -> list:
    """Gamma ships outcomes/prices/tokenIds as JSON strings (sometimes lists)."""
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val:
        try:
            out = json.loads(val)
            return out if isinstance(out, list) else []
        except ValueError:
            return []
    return []


class PolymarketPipeline:
    def __init__(
        self,
        duck: DuckStore,
        sqlite: SqliteStore,
        http: HttpClient,
        *,
        max_markets: int = 30,
        fetch_limit: int = 250,
        holders: bool = True,
    ) -> None:
        self._duck = duck
        self._sqlite = sqlite
        self._http = http
        self._max_markets = max_markets
        self._fetch_limit = fetch_limit
        self._holders = holders

    async def _fetch_markets(self) -> list[dict]:
        """Active markets, highest-volume first, filtered to geopolitical/news."""
        try:
            data = await self._http.get_json(
                GAMMA_MARKETS,
                params={
                    "closed": "false",
                    "active": "true",
                    "order": "volumeNum",
                    "ascending": "false",
                    "limit": str(self._fetch_limit),
                },
                conditional=False,  # odds change constantly; never serve a cached body
            )
        except Exception as exc:
            log.warning("polymarket gamma fetch failed: %s", exc)
            return []
        rows = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
        tracked: list[dict] = []
        for m in rows:
            question = (m.get("question") or "").strip()
            if not question:
                continue
            ok, sign = classify(question)
            if not ok:
                continue
            outcomes = _loads(m.get("outcomes"))
            prices = _loads(m.get("outcomePrices"))
            yes = _yes_prob(outcomes, prices)
            if yes is None:  # non-binary / no Yes leg — skip for the thin slice
                continue
            tokens = _loads(m.get("clobTokenIds"))
            tracked.append({
                "id": m.get("conditionId") or m.get("id"),
                "cond_id": m.get("conditionId"),
                "slug": m.get("slug"),
                "question": question,
                "sign": sign,
                "yes_token": tokens[0] if tokens else None,
                "end_date": m.get("endDate"),
                "closed": bool(m.get("closed")),
                "volume": _f(m.get("volumeNum") or m.get("volume")),
                "liquidity": _f(m.get("liquidityNum") or m.get("liquidity")),
                "yes_prob": yes,
            })
            if len(tracked) >= self._max_markets:
                break
        return tracked

    async def _fetch_holders(self, market: dict) -> None:
        """Best-effort holder concentration; any failure is swallowed.

        The Data API ``/holders`` endpoint keys off the market **conditionId**
        (NOT the clobTokenId — passing a token 400s) and returns a list of
        per-token groups: ``[{"token": "...", "holders": [{proxyWallet,
        amount, ...}]}]``. We read the group for this market's Yes token,
        falling back to all groups when it isn't matched."""
        cond = market.get("cond_id")
        if not cond or not str(cond).startswith("0x"):
            return  # holders needs a real conditionId; numeric ids 400
        try:
            data = await self._http.get_json(
                DATA_HOLDERS, params={"market": cond, "limit": "20"},
                conditional=False,
            )
        except Exception:
            return  # undocumented endpoint — never let it break the sweep
        # Shape: list of {token, holders:[...]} groups. Prefer the Yes-token
        # group; fall back to flattening all groups if it isn't present.
        groups = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        yes_token = market.get("yes_token")
        holders: list = []
        for g in groups:
            if not isinstance(g, dict):
                continue
            hs = g.get("holders")
            if not isinstance(hs, list):
                continue
            if yes_token and str(g.get("token")) == str(yes_token):
                holders = hs
                break
            holders.extend(hs)
        if not holders:
            return
        amounts = []
        for h in holders:
            if not isinstance(h, dict):
                continue
            amt = _f(h.get("amount") or h.get("balance") or h.get("shares"))
            if amt:
                amounts.append((amt, h.get("proxyWallet") or h.get("wallet") or h.get("name")))
        if not amounts:
            return
        total = sum(a for a, _ in amounts)
        if total <= 0:
            return
        amounts.sort(reverse=True)
        top_amt, top_wallet = amounts[0]
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0, tzinfo=None)
        self._duck.execute(
            "INSERT OR REPLACE INTO polymarket_holders "
            "(market_id, ts, n_holders, top_share, top_holder, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                market["id"], now, len(amounts), round(top_amt / total, 4),
                str(top_wallet) if top_wallet else None,
                json.dumps([{"wallet": str(w), "amt": a} for a, w in amounts[:10]]),
            ],
        )

    async def run(self) -> int:
        markets = await self._fetch_markets()
        if not markets:
            return 0
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0, tzinfo=None)
        for m in markets:
            self._duck.execute(
                "INSERT OR REPLACE INTO polymarket_markets "
                "(id, slug, question, category, escalation_sign, yes_token, "
                " end_date, closed, volume, liquidity, updated_at) "
                "VALUES (?, ?, ?, 'geopolitics', ?, ?, ?, ?, ?, ?, ?)",
                [
                    m["id"], m["slug"], m["question"], m["sign"], m["yes_token"],
                    _dt(m["end_date"]), m["closed"], m["volume"], m["liquidity"], now,
                ],
            )
            self._duck.execute(
                "INSERT OR REPLACE INTO polymarket_odds "
                "(market_id, ts, yes_prob, volume) VALUES (?, ?, ?, ?)",
                [m["id"], now, m["yes_prob"], m["volume"]],
            )
        if self._holders:
            # Bound the Data API calls to the highest-volume tracked markets.
            for m in sorted(markets, key=lambda x: x["volume"] or 0, reverse=True)[:15]:
                await self._fetch_holders(m)
        log.info("polymarket ingest: %s tracked market(s) snapshotted", len(markets))
        return len(markets)


def polymarket_summary(duck: DuckStore, move_lookback_hours: int = 24) -> list[dict]:
    """Tracked markets with latest odds, the move vs ~`lookback` ago, volume,
    escalation sign and (if known) holder concentration — newest snapshot per
    market. Sorted by absolute escalation-weighted move (the front-running tell)."""
    rows = duck.fetchall(
        """
        SELECT m.id, m.question, m.slug, m.escalation_sign, m.end_date, m.volume,
               o.yes_prob, o.ts
        FROM polymarket_markets m
        JOIN polymarket_odds o ON o.market_id = m.id
        WHERE o.ts = (SELECT max(ts) FROM polymarket_odds WHERE market_id = m.id)
        """
    )
    out: list[dict] = []
    for mid, q, slug, sign, end_date, vol, yes, ts in rows:
        prior = duck.fetchone(
            "SELECT yes_prob FROM polymarket_odds WHERE market_id = ? "
            "AND ts <= ? - INTERVAL (?) HOUR ORDER BY ts DESC LIMIT 1",
            [mid, ts, move_lookback_hours],
        )
        prev = float(prior[0]) if prior and prior[0] is not None else None
        move = (float(yes) - prev) if (prev is not None and yes is not None) else None
        conc = duck.fetchone(
            "SELECT top_share, n_holders FROM polymarket_holders WHERE market_id = ? "
            "ORDER BY ts DESC LIMIT 1",
            [mid],
        )
        out.append({
            "id": mid, "question": q, "slug": slug,
            "url": f"https://polymarket.com/event/{slug}" if slug else None,
            "escalation_sign": int(sign or 0),
            "end_date": str(end_date)[:10] if end_date else None,
            "volume": float(vol) if vol is not None else None,
            "yes_prob": round(float(yes), 4) if yes is not None else None,
            "prev_prob": round(prev, 4) if prev is not None else None,
            "move": round(move, 4) if move is not None else None,
            "top_share": round(float(conc[0]), 4) if conc and conc[0] is not None else None,
            "n_holders": int(conc[1]) if conc and conc[1] is not None else None,
            "ts": str(ts),
        })
    out.sort(
        key=lambda r: abs((r["move"] or 0) * (r["escalation_sign"] or 0)) or abs(r["move"] or 0),
        reverse=True,
    )
    return out


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _dt(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
