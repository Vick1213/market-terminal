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

## Kronos OHLCV forecasts

`GET /api/forecast?symbol=SPY&horizon=30` projects future daily candles with
[Kronos](https://github.com/shiyu-coder/Kronos) (arXiv:2508.02739, AAAI 2026) —
a foundation model pre-trained on 12B+ K-lines from 45 exchanges. Kronos-small
(24.7M params) runs locally on CPU/MPS; weights download from HuggingFace on
the first call. Output is a *sampled scenario* (generative model), not a point
forecast — treat it as "what a plausible path looks like". Inference core is
vendored (MIT) under `app/forecast/kronos/`; knobs live in `config.py`
(`MARKET_FORECAST_MODEL_ID` etc.). Smoke test:

```bash
uv run python scripts/smoke_forecast.py            # real weights + real bars
uv run python scripts/smoke_forecast.py --offline  # air-gapped integration run
```

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
