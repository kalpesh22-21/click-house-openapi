"""MCP (Model Context Protocol) server for the ClickHouse API.

Exposes the same six operations as the REST API as MCP tools, using the
identical guardrail and execution path via app.service.  The same security
invariants hold regardless of transport:

  - All user-supplied SQL passes through validate_and_sanitize() before
    execution.
  - readonly_settings() is applied on every query path via execute_query().
  - ALLOWED_DATABASES allowlist is enforced on every schema operation.

Transports
----------
stdio (default, MCP_TRANSPORT=stdio):
    Launched as a local subprocess by an MCP client (e.g. Claude Desktop).
    No authentication is required — the local subprocess trust model applies.
    Run:  python -m app.mcp_server

streamable-HTTP (MCP_TRANSPORT=http):
    A Starlette application is served by uvicorn.  Every request to the MCP
    endpoint requires a valid Bearer token matching the API_KEY environment
    variable (same token as the REST API).  A GET /health endpoint is exposed
    without authentication for Kubernetes probes.
    Run:  MCP_TRANSPORT=http python -m app.mcp_server

Configuration (env vars)
------------------------
  MCP_TRANSPORT   stdio | http          default: stdio
  MCP_PORT        integer               default: 8000
  MCP_PATH        string                default: /mcp
  API_KEY         string (required)     Bearer token for HTTP transport
  CLICKHOUSE_*    (see config.py)

Tool catalogue
--------------
  listDatabases       — discover which databases are available
  listTables          — list tables inside a database
  getTableSchema      — get column names, types, comments for a table
  sampleRows          — fetch a small sample of real rows from a table
  runQuery            — execute a validated read-only SQL query
  explainQuery        — EXPLAIN a query without executing it

LLM usage pattern:
  listDatabases → listTables → getTableSchema → (optionally sampleRows) →
  explainQuery (validate SQL) → runQuery
"""

from __future__ import annotations

import argparse
import hmac
import logging
import sys
from typing import Annotated, Any, Optional

import anyio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import get_settings
from app.errors import (
    ClickHouseQueryError,
    ClickHouseUnavailableError,
    DatabaseNotAllowedError,
    QueryValidationError,
    TableNotFoundError,
)
from app.service import (
    explain_query as svc_explain_query,
    get_table_schema as svc_get_table_schema,
    list_databases as svc_list_databases,
    list_tables as svc_list_tables,
    run_query as svc_run_query,
    sample_rows as svc_sample_rows,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

settings = get_settings()

mcp = FastMCP(
    name="ClickHouse API",
    instructions=(
        "This MCP server gives you read-only access to ClickHouse databases. "
        "Recommended workflow: "
        "(1) Call listDatabases to discover available databases. "
        "(2) Call listTables to see tables in a database. "
        "(3) Call getTableSchema BEFORE writing any SELECT query — this gives you "
        "exact column names, ClickHouse types, and comments. "
        "(4) Optionally call sampleRows to inspect real data values. "
        "(5) Call explainQuery to validate your SQL before running it. "
        "(6) Call runQuery to execute the validated query. "
        "All queries are read-only.  INSERT/UPDATE/DELETE/DDL are blocked. "
        "External table functions (url, s3, file, etc.) are blocked."
    ),
    host="0.0.0.0",
    port=settings.mcp_port,
    streamable_http_path=settings.mcp_path,
    log_level=settings.log_level,
)


# ---------------------------------------------------------------------------
# Error translation helpers
# ---------------------------------------------------------------------------

def _domain_to_tool_error(exc: Exception) -> ToolError:
    """Translate a domain error from service.py into a ToolError for MCP clients.

    ToolError messages are returned to the LLM as tool error content so the
    model can self-correct (e.g. fix bad SQL, choose an allowed database).
    """
    if isinstance(exc, QueryValidationError):
        return ToolError(
            f"[{exc.code}] SQL validation failed: {exc.message}. "
            "Fix the SQL and try again, or call explainQuery to diagnose."
        )
    if isinstance(exc, DatabaseNotAllowedError):
        return ToolError(
            f"[{exc.code}] {exc.message}. "
            "Call listDatabases to see which databases are available."
        )
    if isinstance(exc, TableNotFoundError):
        return ToolError(
            f"[{exc.code}] {exc.message}. "
            "Call listTables to verify the table name."
        )
    if isinstance(exc, ClickHouseQueryError):
        return ToolError(
            f"[{exc.code}] ClickHouse query error: {exc.message}. "
            "Call explainQuery to diagnose the SQL."
        )
    if isinstance(exc, ClickHouseUnavailableError):
        return ToolError(
            f"[{exc.code}] {exc.message}. "
            "The ClickHouse server is temporarily unreachable."
        )
    # Log full detail server-side so operators can investigate, but return a
    # generic message to the LLM to avoid leaking internal stack traces or
    # implementation details to clients.
    logger.exception("Unexpected error in MCP tool: %s", exc)
    return ToolError("An internal error occurred.")


# ---------------------------------------------------------------------------
# MCP tools
#
# Notes on type annotations:
#   - structured_output=False was required with pydantic 2.7.x to bypass output
#     model creation for complex return types (list[dict], dict[str, Any]).
#     After upgrading to pydantic>=2.11, FastMCP handles these types natively and
#     structured_output=False is no longer needed — it has been removed.
#   - Parameter annotations use Annotated[<type>, Field(description=...)] so
#     pydantic can build the input schema; bare string metadata in Annotated is
#     not accepted by pydantic v2.
# ---------------------------------------------------------------------------

@mcp.tool(
    name="listDatabases",
    description=(
        "Return the list of ClickHouse databases this server is configured to expose. "
        "Results respect the ALLOWED_DATABASES server allowlist — databases outside that "
        "list are never returned. "
        "Call this first to discover which databases are available before listing tables."
    ),
)
def list_databases() -> list[dict[str, Any]]:
    """List all databases accessible through this MCP server."""
    try:
        return svc_list_databases()
    except Exception as exc:
        raise _domain_to_tool_error(exc) from exc


@mcp.tool(
    name="listTables",
    description=(
        "Return all tables (and their storage engines) inside the specified database. "
        "Call listDatabases first if you are unsure which databases are available. "
        "The 'database' parameter must be a database name returned by listDatabases."
    ),
)
def list_tables(
    database: Annotated[str, Field(description="The database to list tables from")],
) -> list[dict[str, Any]]:
    """List all tables in the specified database."""
    try:
        return svc_list_tables(database)
    except Exception as exc:
        raise _domain_to_tool_error(exc) from exc


@mcp.tool(
    name="getTableSchema",
    description=(
        "Return the full column schema (name, ClickHouse data type, comment) for the specified table. "
        "ALWAYS call this before writing a SELECT query against an unfamiliar table. "
        "Knowing the schema tells you exact column names, types (e.g. UInt64, Nullable(String), "
        "DateTime64(3)), and descriptive comments — preventing type-mismatch errors and helping "
        "you write correct WHERE clauses and aggregations."
    ),
)
def get_table_schema(
    database: Annotated[str, Field(description="The database containing the table")],
    table: Annotated[str, Field(description="The table to describe")],
) -> dict[str, Any]:
    """Return column names, types, and comments for the given table."""
    try:
        return svc_get_table_schema(database, table)
    except Exception as exc:
        raise _domain_to_tool_error(exc) from exc


@mcp.tool(
    name="sampleRows",
    description=(
        "Return a small sample of raw rows from the specified table so you can inspect "
        "actual data values, formats, and nullability before writing analytical queries. "
        "Default sample size is 5 rows; maximum is 50. "
        "Call getTableSchema first to know the column names, then use sampleRows to "
        "understand real data distributions."
    ),
)
def sample_rows(
    database: Annotated[str, Field(description="The database containing the table")],
    table: Annotated[str, Field(description="The table to sample")],
    limit: Annotated[
        int, Field(description="Number of sample rows to return (1–50, default 5)", ge=1, le=50)
    ] = 5,
) -> dict[str, Any]:
    """Return up to *limit* rows from the specified table (capped at 50)."""
    try:
        return svc_sample_rows(database, table, limit)
    except Exception as exc:
        raise _domain_to_tool_error(exc) from exc


@mcp.tool(
    name="runQuery",
    description=(
        "Execute a read-only SQL query (SELECT, WITH, SHOW, DESCRIBE) against ClickHouse "
        "and return results in a compact {columns, rows, row_count, truncated} format. "
        "The server enforces read-only mode, execution time limits, and row caps. "
        "If 'truncated' is true, the result was capped at the server MAX_RESPONSE_ROWS limit "
        "— narrow your query with a more selective WHERE clause or reduce your LIMIT. "
        "Always call getTableSchema before writing a query against an unfamiliar table. "
        "If you get a validation error, call explainQuery first to diagnose your SQL. "
        "INSERT, UPDATE, DELETE, DDL, and external table functions are blocked."
    ),
)
def run_query(
    sql: Annotated[
        str, Field(description="Read-only SQL statement (SELECT / WITH / SHOW / DESCRIBE)")
    ],
    limit: Annotated[
        Optional[int],
        Field(
            description=(
                "Optional row limit (1–10000). "
                "Capped at the server MAX_RESPONSE_ROWS regardless."
            ),
            ge=1,
            le=10_000,
        ),
    ] = None,
) -> dict[str, Any]:
    """Validate, optionally limit, and execute the SQL query."""
    try:
        return svc_run_query(sql, limit)
    except Exception as exc:
        raise _domain_to_tool_error(exc) from exc


@mcp.tool(
    name="explainQuery",
    description=(
        "Run EXPLAIN on a SQL statement and return the query execution plan without executing it. "
        "Use this to validate SQL syntax and inspect the query plan BEFORE calling runQuery, "
        "especially for complex queries or when runQuery returns an error. "
        "If EXPLAIN succeeds the query is syntactically valid and safe to run. "
        "EXPLAIN is fast and cheap — it does not read actual data rows."
    ),
)
def explain_query(
    sql: Annotated[str, Field(description="SQL statement to pass through EXPLAIN")],
) -> dict[str, Any]:
    """Wrap *sql* in EXPLAIN and return the query plan."""
    try:
        return svc_explain_query(sql)
    except Exception as exc:
        raise _domain_to_tool_error(exc) from exc


# ---------------------------------------------------------------------------
# HTTP transport: Bearer auth middleware
# ---------------------------------------------------------------------------

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that enforces Bearer token auth on the MCP path.

    Requests to the MCP endpoint without a valid ``Authorization: Bearer <API_KEY>``
    header are rejected with 401.  The /health path is exempt so Kubernetes
    probes work without credentials.
    """

    def __init__(self, app, api_key: str, mcp_path: str) -> None:
        super().__init__(app)
        self._api_key = api_key
        self._mcp_path = mcp_path.rstrip("/")

    async def dispatch(self, request: Request, call_next) -> Response:
        # Exempt paths that don't start with the MCP mount path.
        if not request.url.path.startswith(self._mcp_path):
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")

        # Distinguish MISSING_AUTH (no Bearer prefix) from INVALID_AUTH (wrong
        # token) so callers get actionable error codes.  Both checks are
        # delegated to _check_bearer_token — the same tested function.
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"error": "Missing Authorization header.", "code": "MISSING_AUTH"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        if not _check_bearer_token(auth_header, self._api_key):
            return JSONResponse(
                {"error": "Invalid API key.", "code": "INVALID_AUTH"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)


def _check_bearer_token(authorization: str | None, api_key: str) -> bool:
    """Return True if the Authorization header carries the correct Bearer token.

    This is a pure function (no request object) that can be unit-tested directly.
    It uses hmac.compare_digest to prevent timing side-channel attacks.

    An empty *provided* token always returns False — hmac.compare_digest(b"", b"")
    would return True, so we guard explicitly against empty tokens before the
    constant-time comparison.  The caller (_run_http) is responsible for ensuring
    api_key is non-empty before this function is invoked; this function is safe
    in isolation regardless.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return False
    provided = authorization[len("Bearer "):]
    # Explicit empty-token guard: reject blank credentials regardless of the
    # server-side api_key value.  hmac.compare_digest(b"", b"") == True, so
    # without this guard an empty Bearer token against an empty api_key would
    # incorrectly pass.
    if not provided:
        return False
    return hmac.compare_digest(
        provided.encode("utf-8"),
        api_key.encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# Health endpoint (HTTP transport only)
# ---------------------------------------------------------------------------

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Lightweight health endpoint for Kubernetes liveness/readiness probes.

    Does NOT require authentication.  Returns 200 with ClickHouse reachability
    status.
    """
    from app.clickhouse_client import ping

    # Call ping() with NO arguments so it reuses the process-wide cached
    # singleton client. Passing an explicit settings routes through
    # get_client(settings) -> _build_client, which constructs (and logs) a
    # brand-new client on every probe — liveness/readiness probes run every
    # few seconds, so that churns connections continuously. (Mirrors the REST
    # /health route in app/routers/health.py.)
    ch_ok = ping()
    return JSONResponse(
        {
            "status": "ok",
            "clickhouse": "ok" if ch_ok else "error",
            "transport": "http",
        }
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments, with env-var defaults from settings."""
    parser = argparse.ArgumentParser(description="ClickHouse MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=settings.mcp_transport,
        help="Transport to use: 'stdio' (default) or 'http' (streamable-HTTP with Bearer auth)",
    )
    return parser.parse_args()


async def _run_http() -> None:
    """Build and serve the streamable-HTTP MCP app with Bearer auth middleware."""
    # Fail closed: if the API key is somehow empty at runtime (e.g. a non-pydantic
    # code path that bypasses the validator), refuse to serve rather than allow
    # unauthenticated access.  The pydantic validator in config.py is the primary
    # defence; this guard is a second line of defence specific to HTTP mode.
    if not settings.api_key or not settings.api_key.strip():
        logger.critical(
            "MCP HTTP transport requires a non-empty API_KEY. "
            "Set API_KEY to a real secret and restart."
        )
        sys.exit(1)

    starlette_app = mcp.streamable_http_app()

    # Wrap with Bearer auth middleware so every request to the MCP path requires
    # a valid API_KEY in the Authorization header.
    authed_app = BearerAuthMiddleware(
        starlette_app,
        api_key=settings.api_key,
        mcp_path=settings.mcp_path,
    )

    logger.info(
        "Starting MCP HTTP server on 0.0.0.0:%d, mount_path=%s",
        settings.mcp_port,
        settings.mcp_path,
    )

    config = uvicorn.Config(
        authed_app,
        host="0.0.0.0",
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()


def main() -> None:
    """Entry point for ``python -m app.mcp_server`` and the MCP_TRANSPORT env var."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    args = _parse_args()
    transport = args.transport

    if transport == "stdio":
        logger.info("Starting MCP server (stdio transport)")
        mcp.run(transport="stdio")
    elif transport == "http":
        anyio.run(_run_http)
    else:
        logger.error("Unknown transport: %s", transport)
        sys.exit(1)


if __name__ == "__main__":
    main()
