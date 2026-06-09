#!/usr/bin/env bash
# Generate TypeScript types from the live FastAPI OpenAPI schema.
# Requires the backend running on :8000 (uv run uvicorn app.main:app).
set -euo pipefail

cd "$(dirname "$0")/.."
OUT="packages/shared/src/api-types.ts"

echo "Fetching OpenAPI schema from http://127.0.0.1:8000/openapi.json ..."
pnpm exec openapi-typescript http://127.0.0.1:8000/openapi.json -o "$OUT"
echo "Wrote $OUT"
echo "Re-export it from packages/shared/src/index.ts to consume in the web app."
