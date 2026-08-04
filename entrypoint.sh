#!/bin/sh
# ---------------------------------------------------------------------------
# Container entrypoint — selects REST API or MCP server based on MODE env var.
#
# MODE=api  (default): start the REST/FastAPI server via uvicorn.
# MODE=mcp            : start the MCP server.
#   MCP_TRANSPORT=stdio   — stdio transport (local subprocess; no HTTP port).
#   MCP_TRANSPORT=http    — streamable-HTTP transport (Bearer auth enforced).
# MODE=token          : start the token-issuing service (OIDC-style IdP) that
#                       mints per-user JWTs and publishes JWKS for the API.
#
# All env vars are read at runtime; the image is the same for all modes.
# ---------------------------------------------------------------------------

set -e

MODE="${MODE:-api}"
MCP_TRANSPORT="${MCP_TRANSPORT:-stdio}"

# uvicorn requires a lowercase log level, but the app normalises LOG_LEVEL to
# uppercase (INFO/DEBUG/...).  Lowercase it here so both agree.
UVICORN_LOG_LEVEL="$(printf '%s' "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')"

case "$MODE" in
  api)
    echo "Starting REST API server (uvicorn) on port ${APP_PORT:-8000}"
    exec uvicorn app.main:app \
      --host 0.0.0.0 \
      --port "${APP_PORT:-8000}" \
      --log-level "${UVICORN_LOG_LEVEL}"
    ;;
  mcp)
    echo "Starting MCP server (transport=${MCP_TRANSPORT})"
    exec python -m app.mcp_server --transport "${MCP_TRANSPORT}"
    ;;
  token)
    echo "Starting Token service (uvicorn) on port ${APP_PORT:-8000}"
    exec uvicorn app.token_service:create_app --factory \
      --host 0.0.0.0 \
      --port "${APP_PORT:-8000}" \
      --log-level "${UVICORN_LOG_LEVEL}"
    ;;
  *)
    echo "ERROR: Unknown MODE='${MODE}'. Valid values: api, mcp, token" >&2
    exit 1
    ;;
esac
