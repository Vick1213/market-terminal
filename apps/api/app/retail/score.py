"""Retail Market Score math (Panel b) — pure functions over stored snapshots.

PLAN §3b: the signal is the mention-volume SPIKE, not absolute mentions
(NVDA/TSLA are always-top noise), modulated by sentiment scored elsewhere.
Everything here reads ts_retail and returns plain dicts; the router and the
pipeline both call it (the pipeline to pick which tickers deserve a social
text poll, the router to serve the leaderboard).

Blocking DuckDB reads — call through an executor.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from app.db.duck import DuckStore

# A symbol needs this many ApeWisdom snapshots before the real mention z-score
# kicks in; younger symbols fall back to a pseudo-z from the 24h change ratio.
MIN_SNAPSHOTS_FOR_Z = 8
Z_WINDOW_DAYS = 7
# Text-source snapshots older than this no longer count as "fresh" for the
# per-symbol sentiment / cross-source confirmation badge.
SENTIMENT_FRESH_HOURS = 24
# Divergence flag: crowd piling IN (z above) while the scored text runs
# bearish (sentiment below) — the classic bagholder setup.
DIVERGENCE_Z = 1.5
DIVERGENCE_SENT = -0.15

TEXT_SOURCES = ("stocktwits", "bluesky", "tradestie")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clamp(v: float, lo: float = -4.0, hi: float = 4.0) -> float:
    return max(lo, min(hi, v))


def _asset_class(sources: set[str]) -> str:
    return "crypto" if sources and all("crypto" in s for s in sources) else "equity"


def _apewisdom_latest(duck: DuckStore) -> tuple[list[tuple], datetime | None]:
    """Rows of the most recent ApeWisdom snapshot (all filters share one ts
    because the pipeline stamps a whole run with a single timestamp)."""
    row = duck.fetchone("SELECT max(ts) FROM ts_retail WHERE source LIKE 'apewisdom:%'")
    if not row or row[0] is None:
        return [], None
    rows = duck.fetchall(
        "SELECT source, symbol, mentions, mentions_prev, rank, rank_prev, upvotes "
        "FROM ts_retail WHERE source LIKE 'apewisdom:%' AND ts = ?",
        [row[0]],
    )
    return rows, row[0]


def _mention_history(duck: DuckStore, cutoff: datetime) -> dict[str, list[float]]:
    """Per-symbol total mentions per snapshot (summed across filters)."""
    rows = duck.fetchall(
        "SELECT symbol, ts, SUM(mentions) FROM ts_retail "
        "WHERE source LIKE 'apewisdom:%' AND ts >= ? GROUP BY symbol, ts ORDER BY ts",
        [cutoff],
    )
    hist: dict[str, list[float]] = {}
    for sym, _ts, m in rows:
        hist.setdefault(sym, []).append(float(m or 0))
    return hist


def _fresh_sentiment(duck: DuckStore) -> dict[str, dict[str, tuple[float, int, str]]]:
    """symbol -> source -> (sentiment, mentions, ts iso) for fresh text rows."""
    cutoff = _now() - timedelta(hours=SENTIMENT_FRESH_HOURS)
    rows = duck.fetchall(
        "SELECT source, symbol, ts, mentions, sentiment_score FROM ts_retail "
        "WHERE source IN (?, ?, ?) AND ts >= ? AND sentiment_score IS NOT NULL "
        "ORDER BY ts",
        [*TEXT_SOURCES, cutoff],
    )
    out: dict[str, dict[str, tuple[float, int, str]]] = {}
    for source, sym, ts, mentions, sent in rows:  # later rows overwrite = latest
        out.setdefault(sym, {})[source] = (float(sent), int(mentions or 0), ts.isoformat())
    return out


def _mention_z(series: list[float], mentions: int, mentions_prev: int) -> float:
    if len(series) >= MIN_SNAPSHOTS_FOR_Z:
        base = series[:-1]
        mean = sum(base) / len(base)
        std = math.sqrt(sum((v - mean) ** 2 for v in base) / len(base))
        if std > 0:
            return _clamp((series[-1] - mean) / std)
        return 0.0
    # Pseudo-z until history accrues: log2 of the 24h change ratio
    # (x2 mentions ~ +1, x4 ~ +2), same clamp as the real thing.
    return _clamp(math.log2((mentions + 1) / (mentions_prev + 1)))


def compute_leaderboard(duck: DuckStore, *, limit: int = 50) -> list[dict]:
    rows, _snapshot_ts = _apewisdom_latest(duck)
    if not rows:
        return []

    # Merge per-filter rows for the same symbol: mentions add up, the best
    # (lowest) rank wins, and the filter set decides the asset class.
    merged: dict[str, dict] = {}
    for source, sym, mentions, prev, rank, rank_prev, upvotes in rows:
        m = merged.setdefault(
            sym,
            {"symbol": sym, "mentions": 0, "mentions_24h_ago": 0, "rank": None,
             "rank_prev": None, "upvotes": 0, "_filters": set()},
        )
        m["mentions"] += int(mentions or 0)
        m["mentions_24h_ago"] += int(prev or 0)
        m["upvotes"] += int(upvotes or 0)
        m["_filters"].add(source)
        if rank is not None and (m["rank"] is None or rank < m["rank"]):
            m["rank"] = int(rank)
            m["rank_prev"] = int(rank_prev) if rank_prev is not None else None

    hist = _mention_history(duck, _now() - timedelta(days=Z_WINDOW_DAYS))
    sentiment = _fresh_sentiment(duck)

    out: list[dict] = []
    for sym, m in merged.items():
        z = _mention_z(hist.get(sym, []), m["mentions"], m["mentions_24h_ago"])

        sent_by_src = sentiment.get(sym, {})
        weighted = [(s, max(n, 1)) for s, n, _ in sent_by_src.values()]
        sent = (
            sum(s * n for s, n in weighted) / sum(n for _, n in weighted)
            if weighted else None
        )

        velocity = (
            m["rank_prev"] - m["rank"]
            if m["rank"] is not None and m["rank_prev"] is not None else None
        )
        # Leaderboard order = z plus a bounded rank-velocity kicker (±1), so a
        # ticker rocketing up the board outranks an equal-z slow burner.
        spike = z + (max(-25, min(25, velocity)) / 25 if velocity is not None else 0.0)

        out.append(
            {
                "symbol": sym,
                "asset_class": _asset_class(m["_filters"]),
                "mentions": m["mentions"],
                "mentions_24h_ago": m["mentions_24h_ago"] or None,
                "mention_z": round(z, 2),
                "rank": m["rank"],
                "rank_velocity": velocity,
                "upvotes": m["upvotes"] or None,
                "sentiment": round(sent, 3) if sent is not None else None,
                "sentiment_sources": sorted(sent_by_src.keys()),
                "sources": 1 + len(sent_by_src),
                "divergence": z >= DIVERGENCE_Z and sent is not None and sent <= DIVERGENCE_SENT,
                "spike_score": round(spike, 3),
            }
        )

    out.sort(key=lambda r: r["spike_score"], reverse=True)
    return out[:limit]


def compute_gauge(duck: DuckStore) -> dict:
    """Market-wide Retail Risk-On/Off: mention-weighted bull/bear across the
    scored text sources (the direction) + total-chatter z (the intensity)."""
    rows, snapshot_ts = _apewisdom_latest(duck)
    total_mentions = chatter_z = None
    if rows:
        total_mentions = sum(int(r[2] or 0) for r in rows)
        totals_rows = duck.fetchall(
            "SELECT ts, SUM(mentions) FROM ts_retail WHERE source LIKE 'apewisdom:%' "
            "AND ts >= ? GROUP BY ts ORDER BY ts",
            [_now() - timedelta(days=Z_WINDOW_DAYS)],
        )
        totals = [float(r[1] or 0) for r in totals_rows]
        if len(totals) >= MIN_SNAPSHOTS_FOR_Z:
            base = totals[:-1]
            mean = sum(base) / len(base)
            std = math.sqrt(sum((v - mean) ** 2 for v in base) / len(base))
            if std > 0:
                chatter_z = round(_clamp((totals[-1] - mean) / std), 2)

    sentiment = _fresh_sentiment(duck)
    weighted: list[tuple[float, int]] = []
    for by_src in sentiment.values():
        for s, n, _ in by_src.values():
            weighted.append((s, max(n, 1)))
    sent = (
        sum(s * n for s, n in weighted) / sum(n for _, n in weighted)
        if weighted else None
    )

    return {
        "score": round(100 * sent) if sent is not None else None,
        "sentiment": round(sent, 3) if sent is not None else None,
        "chatter_z": chatter_z,
        "total_mentions": total_mentions,
        "scored_symbols": len(sentiment),
        "computed_at": snapshot_ts.isoformat() if snapshot_ts else None,
    }
