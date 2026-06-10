"""GET /api/corr — Correlation Cookbook snapshot (Panel f): regime banner,
12 rule-cards with HOLDS/WATCH/BROKEN status, stress radar, 90d heatmap.
GET /api/corr/{card_id} computes the drill-down on demand (rolling-corr
history, rebased legs, lead-lag profile). Recompute pushes on WS topic "corr"."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.corr.cards import CARDS_BY_ID
from app.corr.engine import compute_card_history

router = APIRouter(prefix="/api", tags=["corr"])


class CorrResponse(BaseModel):
    computed_at: str | None
    regime: str
    regime_score: float | None
    cards: list[dict]
    stress: dict
    heatmap: dict


@router.get("/corr", response_model=CorrResponse)
async def corr(request: Request) -> CorrResponse:
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()

    row = await loop.run_in_executor(
        None,
        duck.fetchone,
        "SELECT detail FROM corr_snapshots WHERE card_id = '_meta' ORDER BY ts DESC LIMIT 1",
    )
    if row is None or not row[0]:
        return CorrResponse(
            computed_at=None, regime="warming-up", regime_score=None,
            cards=[], stress={"on": False, "lights": []},
            heatmap={"labels": [], "matrix": []},
        )
    detail = json.loads(row[0])
    return CorrResponse(
        computed_at=detail.get("computed_at"),
        regime=detail.get("regime", "unknown"),
        regime_score=detail.get("regime_score"),
        cards=detail.get("cards", []),
        stress=detail.get("stress", {"on": False, "lights": []}),
        heatmap=detail.get("heatmap", {"labels": [], "matrix": []}),
    )


@router.get("/corr/{card_id}")
async def corr_card(request: Request, card_id: str) -> dict:
    if card_id not in CARDS_BY_ID:
        raise HTTPException(status_code=404, detail=f"unknown card: {card_id}")
    duck = request.app.state.duck
    loop = asyncio.get_running_loop()
    out = await loop.run_in_executor(None, compute_card_history, duck, card_id)
    if out is None:
        raise HTTPException(status_code=404, detail=f"unknown card: {card_id}")
    return out
