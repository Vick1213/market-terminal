# Market Terminal

A local-first, private, **free-data** market-intelligence terminal — Next.js dashboard + FastAPI backend + local sentiment models, all running on your machine.

📋 **Design:** [`PLAN.md`](./PLAN.md) · **Source detail & 2026 citations:** [`RESEARCH-APPENDIX.md`](./RESEARCH-APPENDIX.md)

> **Status: Phase 0 — runnable skeleton.** REST + WebSocket + scheduler + DuckDB/SQLite + ingest layer wired end to end, with a Next.js dashboard grid and a live System panel. Panels (a)–(f) land in Phases 1–6.

## Stack

| | |
|---|---|
| Frontend | Next.js 15 · react-grid-layout · TanStack Query · Zustand · WebSocket |
| Backend | FastAPI · APScheduler · WebSocket fan-out · httpx/aiolimiter/tenacity/diskcache ingest |
| Storage | DuckDB (time-series) · SQLite WAL (app state) |
| Models | FinBERT (bulk) + local LLM (aspect/brief) — Phase 1 |

## Prerequisites

`node` ≥ 20, `pnpm` ≥ 9, `uv` (Python is managed by uv; backend pinned to 3.12).

## Run

```bash
# 1. Backend (terminal A)
cd apps/api
uv sync
uv run uvicorn app.main:app --reload --port 8000

# 2. Frontend (terminal B)
pnpm install            # from repo root (installs the whole workspace)
pnpm dev:web            # -> http://localhost:3000
```

Or run both at once with honcho: `uv tool install honcho && honcho start`.

### Verify Phase 0
- <http://127.0.0.1:8000/api/health> → JSON status snapshot (live REST round-trip)
- <http://localhost:3000> → dashboard; the **System** panel shows REST health + a live WS tick counter incrementing every 5s (live WS round-trip)
- <http://127.0.0.1:8000/docs> → OpenAPI docs

### Regenerate API types (optional)
With the backend running: `pnpm gen:types` → `packages/shared/src/api-types.ts`.

## Layout

```
apps/
  api/        FastAPI backend (see apps/api/README.md)
  web/        Next.js dashboard
packages/
  shared/     shared TS types (+ generated OpenAPI types)
scripts/      gen-types.sh
deploy/        launchd template for run-at-login
data/         DuckDB/SQLite/HTTP cache (gitignored, created at runtime)
```
