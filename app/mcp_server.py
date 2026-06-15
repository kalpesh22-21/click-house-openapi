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
    endpoint requires a valid OIDC/JWT Bearer token, validated per request
    against the IdP's JWKS (see app/auth_jwt.py).  Two endpoints are exposed
    without authentication: GET /health for Kubernetes probes, and
    GET /.well-known/oauth-protected-resource (RFC 9728 Protected Resource
    Metadata) so OAuth-capable MCP clients can discover the authorization server
    and obtain a token via authorization-code + PKCE (see docs/oauth.md).
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
import json
import logging
import sys
from typing import Annotated, Any, Optional

import anyio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.auth_jwt import JWTAuthError, validate_token
from app.config import get_settings
from app.principal import current_principal
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
    # Stateless HTTP: do not keep per-process MCP session state. Each request is
    # self-contained, so any replica can serve any request. This is required when
    # running >1 replica behind a round-robin load balancer without session
    # affinity — otherwise a follow-up request carrying an mcp-session-id minted
    # on pod A gets routed to pod B, which returns 404 (unknown session).
    stateless_http=True,
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

def _bearer_from_scope(scope: dict[str, Any]) -> Optional[str]:
    """Extract the Bearer token from an ASGI scope, or None if absent/malformed."""
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            header = value.decode("latin-1")
            if header.startswith("Bearer "):
                token = header[len("Bearer "):].strip()
                return token or None
            return None
    return None


async def _send_json_response(
    send,
    status_code: int,
    payload: dict[str, Any],
    *,
    www_authenticate: Optional[str] = None,
) -> None:
    """Emit a complete JSON ASGI response (used for auth rejections).

    *www_authenticate*, when given, is the full ``WWW-Authenticate`` header value
    (e.g. ``Bearer resource_metadata="…"``) sent verbatim.
    """
    body = json.dumps(payload).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if www_authenticate:
        headers.append((b"www-authenticate", www_authenticate.encode("latin-1")))
    await send({"type": "http.response.start", "status": status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _www_authenticate(
    settings,
    *,
    error: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """Build an RFC 6750 / RFC 9728 ``WWW-Authenticate: Bearer`` challenge.

    Always advertises ``resource_metadata`` so an MCP client that hits a 401/403
    can discover the authorization server and start the OAuth flow (RFC 9728
    §5.1) instead of needing a hand-minted token.
    """
    params: list[str] = []
    if error:
        params.append(f'error="{error}"')
    if description:
        # Strip quotes/newlines so the header stays well-formed (auth messages are
        # already generic — no token detail leaks here).
        safe = description.replace('"', "'").replace("\n", " ").replace("\r", " ")
        params.append(f'error_description="{safe}"')
    params.append(f'resource_metadata="{settings.protected_resource_metadata_url()}"')
    return "Bearer " + ", ".join(params)


class JWTAuthMiddleware:
    """Pure-ASGI middleware: validate the JWT and bind the principal per request.

    Why pure-ASGI rather than BaseHTTPMiddleware: BaseHTTPMiddleware runs the
    downstream app in a separate task, so a ContextVar set in it does NOT reach
    the MCP tool functions.  A pure-ASGI middleware awaits the app in the SAME
    context, so ``current_principal`` set here propagates into the tool call (and
    into the anyio threadpool that runs the sync tools).

    Default-deny: every request is rejected unless it carries a valid token,
    except an explicit public-path allowlist (default: /health for k8s probes).
    Statelessness is preserved — the token is verified per request (only JWKS is
    cached), so any replica can serve any request.
    """

    # OAuth Protected Resource Metadata (RFC 9728) is public discovery data — a
    # client fetches it *before* it has a token, so any path under this prefix is
    # always served without auth, regardless of the configured public_paths.
    _OAUTH_PRM_PREFIX = "/.well-known/oauth-protected-resource"

    def __init__(self, app, settings, public_paths: tuple[str, ...] = ("/health",)) -> None:
        self.app = app
        self.settings = settings
        self._public_paths = frozenset(p.rstrip("/") or "/" for p in public_paths)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = (scope.get("path", "") or "/").rstrip("/") or "/"
        if path in self._public_paths or path.startswith(self._OAUTH_PRM_PREFIX):
            await self.app(scope, receive, send)
            return

        token = _bearer_from_scope(scope)
        if token is None:
            # Logged so an operator can confirm the server received the request and
            # answered with a Bearer challenge — the signal an OAuth client (e.g.
            # ChatGPT) needs to (re)start the flow. Without this, a token-expiry
            # round-trip leaves no server-side trace at the default INFO level.
            logger.info(
                "MCP auth rejected: path=%s status=401 code=MISSING_AUTH — "
                "sent WWW-Authenticate challenge for re-auth",
                path,
            )
            await _send_json_response(
                send,
                401,
                {"error": "Missing Authorization header.", "code": "MISSING_AUTH"},
                # No error code on a *missing* token: RFC 6750 §3.1 says a request
                # with no credentials SHOULD NOT carry one (reserve error codes for
                # a token that was sent but rejected). The resource_metadata pointer
                # is what lets the client start the flow.
                www_authenticate=_www_authenticate(self.settings),
            )
            return

        try:
            principal = validate_token(token, self.settings)
        except JWTAuthError as exc:
            # Map the auth failure to an OAuth error code so a spec-aware client
            # knows whether to refresh its token (invalid_token) or that the token
            # lacks the required tenant claim (insufficient_scope). 503 (JWKS
            # unreachable) is a server fault, not a challenge, so no error code.
            oauth_error = {401: "invalid_token", 403: "insufficient_scope"}.get(
                exc.status_code
            )
            # An expired/invalid token is the routine re-auth trigger; log it (INFO,
            # not WARNING — tokens expire on a normal cadence) so the 401+challenge
            # is observable. If a client reports "couldn't connect" yet this line is
            # present with status=401 oauth_error=invalid_token, the server did its
            # job and the failure is downstream (the client's refresh / IdP step).
            logger.info(
                "MCP auth rejected: path=%s status=%d code=%s oauth_error=%s — "
                "sent WWW-Authenticate challenge for re-auth",
                path,
                exc.status_code,
                exc.code,
                oauth_error,
            )
            await _send_json_response(
                send,
                exc.status_code,
                {"error": exc.message, "code": exc.code},
                www_authenticate=_www_authenticate(
                    self.settings, error=oauth_error, description=exc.message
                ),
            )
            return

        ctx_token = current_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            current_principal.reset(ctx_token)


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
# OAuth 2.0 Protected Resource Metadata (RFC 9728) — HTTP transport only
#
# These endpoints let interactive MCP clients discover the authorization server
# and run the authorization-code + PKCE flow themselves, instead of an operator
# hand-minting a JWT. They are PUBLIC (no auth): a client fetches them *before*
# it has a token. The actual login/token issuance happens at the external IdP
# advertised in `authorization_servers` (see docs/oauth.md).
#
# RFC 9728 §3.1 locates the metadata for resource `https://host/mcp` at
# `https://host/.well-known/oauth-protected-resource/mcp`, so we serve both the
# path-suffixed form clients derive and the bare root form for compatibility.
# ---------------------------------------------------------------------------

@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource(request: Request) -> JSONResponse:
    """Return this server's OAuth 2.0 Protected Resource Metadata (RFC 9728).

    Reads settings fresh (not the import-time module global) so the advertised
    issuer/resource always reflect current config.
    """
    return JSONResponse(get_settings().protected_resource_metadata())


# Also register the RFC 9728 path-suffixed variant (…/oauth-protected-resource/mcp)
# that MCP clients construct from the resource path, unless the mount path is the
# root (which would collide with the route above).
if settings.mcp_path not in ("", "/"):
    mcp.custom_route(
        f"/.well-known/oauth-protected-resource{settings.mcp_path}",
        methods=["GET"],
    )(oauth_protected_resource)


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
    """Build and serve the streamable-HTTP MCP app with JWT auth middleware."""
    # Fail closed: a network-exposed transport must not start without a way to
    # verify tokens.  Require the OIDC config (JWKS URL + issuer + audience)
    # before serving; otherwise every request would be unauthenticable.
    if not settings.auth_configured():
        logger.critical(
            "MCP HTTP transport requires OIDC config. Set OIDC_JWKS_URL, "
            "OIDC_ISSUER, and OIDC_AUDIENCE to enable JWT validation, then restart."
        )
        sys.exit(1)

    starlette_app = mcp.streamable_http_app()

    # Wrap with the JWT auth middleware so every request requires a valid token.
    # /health is exempted by default so Kubernetes liveness/readiness probes work
    # without credentials.
    authed_app = JWTAuthMiddleware(starlette_app, settings=settings)

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
