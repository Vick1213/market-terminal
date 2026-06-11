"""Health / status endpoints — the live REST round-trip for Phase 0.

GET /api/health returns a snapshot the frontend HeartbeatPanel renders: app
metadata, resolved data paths, DuckDB table row counts, watchlist size,
scheduler state, and current WS client count.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str
    time: str
    data_dir: str
    scheduler_running: bool
    ws_clients: int
    watchlist_count: int
    duckdb_tables: dict[str, int]


@router.get("/health/sources")
async def health_sources(request: Request) -> dict:
    """Source-health watchdog snapshot: one row per outbound host with its
    status (ok / degraded / dead), failure streak, and last success/error."""
    registry = request.app.state.source_health
    sources = registry.snapshot()
    worst = "ok"
    for s in sources:
        if s["status"] == "dead":
            worst = "dead"
            break
        if s["status"] == "degraded":
            worst = "degraded"
    return {"status": worst, "sources": sources}


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    app = request.app
    settings = app.state.settings
    duck = app.state.duck
    sqlite = app.state.sqlite
    scheduler = app.state.scheduler
    hub = app.state.hub

    row = sqlite.fetchone("SELECT COUNT(*) AS c FROM watchlist")
    watchlist_count = int(row["c"]) if row else 0

    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=settings.version,
        time=datetime.now(timezone.utc).isoformat(),
        data_dir=str(settings.data_dir),
        scheduler_running=bool(getattr(scheduler, "running", False)),
        ws_clients=hub.client_count(),
        watchlist_count=watchlist_count,
        duckdb_tables=duck.table_counts(),
    )
