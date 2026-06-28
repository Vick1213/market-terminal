"""GET /api/macro — composite Risk-On/Off score, sub-buckets, regime + dials
backing Panel (c). Live refreshes arrive on WS topic "macro"; this endpoint
serves the full snapshot (the WS frame only carries score/regime)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api", tags=["macro"])

# Headline dials shown in the panel, with display labels.
DIAL_SERIES: list[tuple[str, str]] = [
    ("VIXCLS", "VIX"),
    ("BAMLH0A0HYM2", "HY OAS"),
    ("NFCI", "NFCI"),
    ("T10Y2Y", "2s10s"),
    ("DFII10", "10y real"),
    ("DTWEXBGS", "Broad USD"),
    ("NAAIM_EXPOSURE", "NAAIM"),
    ("PC_TOTAL", "Put/Call"),
    ("FINRA_SHORT_RATIO", "Short vol %"),
    # Phase 16 (§11) additions — bond vol, rates decomposition, recession signal.
    ("MOVE", "MOVE"),
    ("THREEFYTP10", "Term prem"),
    ("CFNAIMA3", "CFNAI 3mo"),
]


class MacroDial(BaseModel):
    series_id: str
    label: str
    value: float
    ts: str


class LiquidityBlock(BaseModel):
    """Daily Fed net liquidity = WALCL − TGA − RRP, all in $bn (Phase 8)."""

    as_of: str
    net_bn: float
    d5_bn: float | None    # change vs 5 daily prints ago
    d20_bn: float | None   # change vs 20 daily prints ago (~1 month)
    walcl_bn: float | None
    tga_bn: float | None
    rrp_bn: float | None


class MacroHistoryPoint(BaseModel):
    ts: str
    score: float


class MacroResponse(BaseModel):
    score: float | None
    regime: str
    computed_at: str | None
    buckets: list[dict]  # composite detail (weights, z, contributions, components)
    dials: dict[str, dict]  # regime-classifier votes
    headline: list[MacroDial]
    history: list[MacroHistoryPoint]
    liquidity: LiquidityBlock | None = None


@router.get("/macro", response_model=MacroResponse)
async def macro(
    request: Request,
    history_limit: int = Query(default=180, le=1000),
) -> MacroResponse:
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    def _q():
        latest = duck.fetchone(
            "SELECT ts, score, regime, detail FROM macro_composite ORDER BY ts DESC LIMIT 1"
        )
        hist = duck.fetchall(
            "SELECT ts, score FROM macro_composite ORDER BY ts DESC LIMIT ?",
            [history_limit],
        )
        # Skip stale series (e.g. a dead source still serving old files).
        cutoff = (datetime.now(timezone.utc) - timedelta(days=45)).replace(tzinfo=None)
        dials = []
        for sid, label in DIAL_SERIES:
            row = duck.fetchone(
                "SELECT ts, value FROM ts_macro WHERE series_id = ? AND value IS NOT NULL "
                "AND ts >= ? ORDER BY ts DESC LIMIT 1",
                [sid, cutoff],
            )
            if row:
                dials.append((sid, label, row[1], row[0]))
        net = duck.fetchall(
            "SELECT ts, value FROM ts_macro WHERE series_id = 'NET_LIQUIDITY' "
            "AND value IS NOT NULL ORDER BY ts DESC LIMIT 21"
        )
        liq = None
        if net:
            comp = {}
            for sid in ("WALCL", "TGA_CLOSE", "RRP_ON"):
                r = duck.fetchone(
                    "SELECT value FROM ts_macro WHERE series_id = ? "
                    "AND value IS NOT NULL ORDER BY ts DESC LIMIT 1",
                    [sid],
                )
                comp[sid] = r[0] if r else None
            liq = LiquidityBlock(
                as_of=str(net[0][0])[:10],
                net_bn=round(float(net[0][1]), 1),
                d5_bn=round(float(net[0][1] - net[5][1]), 1) if len(net) > 5 else None,
                d20_bn=round(float(net[0][1] - net[20][1]), 1) if len(net) > 20 else None,
                walcl_bn=round(comp["WALCL"] / 1000.0, 1) if comp["WALCL"] is not None else None,
                tga_bn=round(comp["TGA_CLOSE"], 1) if comp["TGA_CLOSE"] is not None else None,
                rrp_bn=round(comp["RRP_ON"], 1) if comp["RRP_ON"] is not None else None,
            )
        return latest, hist, dials, liq

    latest, hist, dial_rows, liquidity = await loop.run_in_executor(None, _q)

    if latest is None:
        return MacroResponse(
            score=None, regime="warming-up", computed_at=None,
            buckets=[], dials={}, headline=[], history=[], liquidity=liquidity,
        )

    detail = json.loads(latest[3]) if latest[3] else {}
    return MacroResponse(
        score=latest[1],
        regime=latest[2],
        computed_at=detail.get("computed_at"),
        buckets=detail.get("buckets", []),
        dials=detail.get("dials", {}),
        headline=[
            MacroDial(series_id=sid, label=label, value=val, ts=ts.isoformat())
            for sid, label, val, ts in dial_rows
        ],
        history=[
            MacroHistoryPoint(ts=r[0].isoformat(), score=r[1]) for r in reversed(hist)
        ],
        liquidity=liquidity,
    )
