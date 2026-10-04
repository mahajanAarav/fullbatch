#!/usr/bin/env bash
# Start everything for local development: database, API (port 8000) and frontend (port 5173).
# Run from anywhere:  ./dev.sh      Stop with Ctrl+C.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/backend/.venv/bin/python"

[ -x "$PY" ] || { echo "Missing backend/.venv. Create it first (see README)."; exit 1; }
[ -d "$ROOT/frontend/node_modules" ] || (cd "$ROOT/frontend" && npm install)

echo "Starting local database..."
"$PY" "$ROOT/backend/scripts/dev_db.py" | grep -E "tables are current" || true

export DATABASE_URL="postgresql+psycopg://postgres:@/postgres?host=$ROOT/backend/.devdb"
export FRONTEND_URL="http://localhost:5173"
export PUBLIC_API_URL="http://localhost:8000"
export DEV_LOGIN=1   # local-only sign-in, because PayPal login needs a public https address

cleanup() { kill "$API_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "Starting API on http://localhost:8000 ..."
(cd "$ROOT/backend" && exec "$PY" -m uvicorn app.main:app --port 8000) &
API_PID=$!

echo "Starting frontend on http://localhost:5173 ..."
cd "$ROOT/frontend" && npm run dev
