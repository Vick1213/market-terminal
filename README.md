# Market Terminal

A local-first, private market-intelligence terminal — Next.js dashboard + FastAPI backend + local
models, running entirely on your machine. Your keys, your data, your positions: nothing is sent to a
vendor you didn't choose.

📋 **Design & phase history:** [`PLAN.md`](./PLAN.md) · **Product roadmap:** [`PRODUCT.md`](./PRODUCT.md)
· **Source detail & citations:** [`RESEARCH-APPENDIX.md`](./RESEARCH-APPENDIX.md)

## What it does

**Market intelligence.** ~30 dashboard panels over free data sources: macro and rates, cross-asset
rotation, positioning (COT/TFF, NAAIM, short interest), volatility complex, breadth, news with local
FinBERT sentiment, EDGAR filings (8-K, 13D, insider), Fed speeches, Treasury supply, prediction
markets, and a narrative-vs-money divergence engine.

**Research harness.** A leakage-safe ML pipeline — point-in-time feature matrices, purged/embargoed
walk-forward CV, PBO and deflated-Sharpe gating — used to *falsify* signal claims as much as to find
them. Several of this repo's more interesting results are negative ones, recorded in `PLAN.md` rather
than quietly dropped.

**Trading bots (paper by default).** Two sleeves — a swing sleeve driven by an LLM strategist and an
intraday sleeve running hedged brackets — against Alpaca paper or IBKR. Both ship **disabled**, with a
kill switch, code-enforced guardrails, and end-of-day self-grading that scores past decisions against
realized outcomes.

## Safety posture

This repo trades money, so the defaults are deliberately conservative:

- **Every bot and every scheduled ML job is default-OFF.** Nothing runs autonomously until you opt in
  via an explicit `MARKET_*` environment variable.
- **Paper trading is the default path.** Live trading is hard-gated (see `PRODUCT.md` M5).
- **The swing sleeve is cash-only** and must never buy on margin.
- **Guardrails are code, not prompts** — order checks live in `app/trading/guardrails.py`, outside the
  LLM's reach.
- **Claims are gated before they reach the bots.** New signals go through the validation harness,
  then shadow mode, then graded against live outcomes — only then do they touch sizing.

## Stack

| | |
|---|---|
| Frontend | Next.js 15 · react-grid-layout · TanStack Query · Zustand · WebSocket |
| Backend | FastAPI · APScheduler · WebSocket fan-out · httpx/aiolimiter/tenacity/diskcache ingest |
| Storage | DuckDB (time series, ML panels) · SQLite WAL (app state) |
| Models | FinBERT sentiment · local or hosted LLM for briefs/strategist · LightGBM research harness · Kronos OHLCV forecasts |
| Brokers | Alpaca (paper + live-capable) · IBKR Client Portal (reads) |
| Desktop | Tauri shell wrapping the web UI, FastAPI bundled as a sidecar |

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

Or run both at once: `uv tool install honcho && honcho start`.

- <http://localhost:3000> — dashboard
- <http://127.0.0.1:8000/api/health> — status snapshot
- <http://127.0.0.1:8000/docs> — OpenAPI docs

Most panels work with no keys at all. Optional keys (FRED, FMP, Finnhub, Alpaca, …) go in
`apps/api/.env` as `MARKET_*` variables and unlock the sources that need them; anything without a key
degrades gracefully instead of failing.

### Regenerate API types

With the backend running: `pnpm gen:types` → `packages/shared/src/api-types.ts`.

## Layout

```
apps/
  api/        FastAPI backend — ingest, scheduler, ML, trading (see apps/api/README.md)
    app/ml/       research harness: features, labels, CV, model zoo, vol estimators
    app/trading/  bot sleeves, brokers, guardrails, reviews
    app/edge/     strategist LLM + its read-only tool registry
  web/        Next.js dashboard (src/components/panels)
  desktop/    Tauri shell
packages/
  shared/     shared TS types (+ generated OpenAPI types)
data/         DuckDB/SQLite/HTTP cache (gitignored, created at runtime)
docs/         data-licensing audit and design notes
```

## A note on the research in here

Backtested edge is guilty until proven innocent. The harness exists because this project has already
produced at least one confident false positive — a macro signal that looked strong on revised data and
evaporated on point-in-time vintages. Findings in `PLAN.md` carry their falsification history,
including the ones that didn't survive. Treat any performance number here as a hypothesis with a date
on it, not a result.

**This is personal software for personal research. Nothing here is investment advice.**
