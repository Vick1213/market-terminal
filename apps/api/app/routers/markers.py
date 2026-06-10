"""/api/markers — persistent chart annotations ("mark data that stays").

A marker is pinned to a chart (chart_key) at a time/price with an optional
note. Stored in SQLite app state; every chart with the same chart_key shows
the same markers across reloads and sessions.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api", tags=["markers"])


class MarkerIn(BaseModel):
    chart_key: str = Field(min_length=1, max_length=200)
    t: int  # unix seconds
    price: float | None = None
    series_id: str | None = None
    text: str = Field(default="", max_length=500)


class Marker(MarkerIn):
    id: str


class MarkersResponse(BaseModel):
    markers: list[Marker]


@router.get("/markers", response_model=MarkersResponse)
async def list_markers(request: Request, chart_key: str = Query()) -> MarkersResponse:
    sqlite = request.app.state.sqlite
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(
        None,
        sqlite.fetchall,
        "SELECT id, chart_key, t, price, series_id, text FROM chart_markers "
        "WHERE chart_key = ? ORDER BY t",
        [chart_key],
    )
    return MarkersResponse(
        markers=[
            Marker(
                id=r["id"], chart_key=r["chart_key"], t=r["t"],
                price=r["price"], series_id=r["series_id"], text=r["text"] or "",
            )
            for r in rows
        ]
    )


@router.post("/markers", response_model=Marker)
async def create_marker(request: Request, marker: MarkerIn) -> Marker:
    sqlite = request.app.state.sqlite
    loop = asyncio.get_running_loop()
    marker_id = uuid.uuid4().hex[:12]
    await loop.run_in_executor(
        None,
        lambda: sqlite.execute(
            "INSERT INTO chart_markers (id, chart_key, t, price, series_id, text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [marker_id, marker.chart_key, marker.t, marker.price, marker.series_id, marker.text],
        ),
    )
    return Marker(id=marker_id, **marker.model_dump())


@router.delete("/markers/{marker_id}")
async def delete_marker(request: Request, marker_id: str) -> dict:
    sqlite = request.app.state.sqlite
    loop = asyncio.get_running_loop()
    row = await loop.run_in_executor(
        None, sqlite.fetchone, "SELECT id FROM chart_markers WHERE id = ?", [marker_id]
    )
    if row is None:
        raise HTTPException(status_code=404, detail="marker not found")
    await loop.run_in_executor(
        None, lambda: sqlite.execute("DELETE FROM chart_markers WHERE id = ?", [marker_id])
    )
    return {"deleted": marker_id}
