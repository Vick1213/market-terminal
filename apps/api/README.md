# Market Terminal — API (FastAPI)

Single-process backend: REST + WebSocket + APScheduler + DuckDB/SQLite + the
shared rate-limited ingest layer.

## Run (dev)

```bash
cd apps/api
uv sync                 # creates .venv from pyproject (Python 3.12 via uv)
uv run uvicorn app.main:app --reload --port 8000
```

Then:

- REST: <http://127.0.0.1:8000/api/health>
- Docs: <http://127.0.0.1:8000/docs>
- WS:   `ws://127.0.0.1:8000/ws/heartbeat` (emits a tick every 5s)

## Layout

```
app/
  config.py        settings + resolved paths + global User-Agent
  db/              DuckStore (time-series), SqliteStore (app state), schema
  ingest/http.py   httpx + aiolimiter + tenacity + diskcache (conditional GET)
  scheduler/jobs.py  APScheduler jobs (Phase 0: heartbeat broadcast)
  ws/hub.py        topic-based WebSocket fan-out (ConnectionManager)
  routers/         health (REST) + ws (WebSocket)
  main.py          app factory + lifespan wiring
```
