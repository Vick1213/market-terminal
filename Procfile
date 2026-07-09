# Run both processes with: honcho start   (install: `uv tool install honcho`)
# No --reload: keeps the API a single process so Ctrl+C / closing honcho kills it
# cleanly (--reload orphans a worker subprocess that keeps running the scheduler + ntfy).
# For hot-reload during dev, run manually: cd apps/api && uv run uvicorn app.main:app --reload
api: cd apps/api && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
web: cd apps/web && pnpm dev
