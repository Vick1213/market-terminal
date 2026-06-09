"""WebSocket endpoint — the live WS round-trip for Phase 0.

A client connects to /ws/{topic} (default topic ``heartbeat``), receives a
welcome frame, then receives every message broadcast to that topic by the
scheduler/ingestors. We read in a loop only to detect disconnects.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.ws.hub import hub

log = logging.getLogger("market.ws")

router = APIRouter()


@router.websocket("/ws/{topic}")
async def ws_topic(ws: WebSocket, topic: str) -> None:
    await hub.connect(ws, topic)
    log.info("ws connect topic=%s clients=%s", topic, hub.client_count())
    try:
        await ws.send_json({"type": "welcome", "topic": topic})
        while True:
            # We don't expect inbound messages; this await unblocks on disconnect.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(ws, topic)
        log.info("ws disconnect topic=%s clients=%s", topic, hub.client_count())
