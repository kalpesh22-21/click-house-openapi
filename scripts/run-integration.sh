#!/usr/bin/env bash
# Run the integration test suite against a local docker-compose stack.
#
# Usage:
#   bash scripts/run-integration.sh [--no-teardown]
#
# Behaviour:
#   1. Brings the stack up (docker compose up -d --build).
#   2. Polls GET /health on the REST API until it returns 200 (or times out).
#   3. Runs the integration suite with RUN_INTEGRATION=1.
#   4. Tears down the stack (unless --no-teardown is passed).
#   5. Exits with the pytest exit code.

set -euo pipefail

API_BASE="${IT_API_BASE:-http://localhost:18080}"
MAX_WAIT_SECONDS=60
TEARDOWN=true

for arg in "$@"; do
  case $arg in
    --no-teardown) TEARDOWN=false ;;
  esac
done

# Move to repo root (this script lives in scripts/)
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> Starting docker-compose stack..."
docker compose up -d --build

echo "==> Waiting for REST /health at $API_BASE (up to ${MAX_WAIT_SECONDS}s)..."
elapsed=0
until curl -sf "$API_BASE/health" > /dev/null 2>&1; do
  if [ "$elapsed" -ge "$MAX_WAIT_SECONDS" ]; then
    echo "ERROR: REST /health not reachable after ${MAX_WAIT_SECONDS}s." >&2
    docker compose logs --tail=30
    exit 1
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done
echo "==> Stack is healthy (waited ${elapsed}s)."

echo "==> Running integration tests..."
set +e
RUN_INTEGRATION=1 .venv/bin/python -m pytest tests/integration -v
PYTEST_EXIT=$?
set -e

if [ "$TEARDOWN" = true ]; then
  echo "==> Tearing down stack..."
  docker compose down
fi

echo "==> Done (pytest exit code: $PYTEST_EXIT)."
exit "$PYTEST_EXIT"
