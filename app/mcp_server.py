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
  OIDC_JWKS_URL / OIDC_PUBLIC_KEY       required for HTTP transport (JWT verify)
  CLICKHOUSE_*    (see config.py)

Tool catalogue
--------------
  listDatabases       — discover which databases are available
  listTables          — list tables inside a database
  getTableSchema      — get column names, types, comments for a table
                        (optional `columns` narrows the column array; the
                         base sections always ride complete)
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
import json
import logging
import sys
from typing import Annotated, Any, Optional

import anyio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.auth_jwt import JWTAuthError, validate_token
from app.config import get_settings
from app.principal import current_principal, current_scope, current_session_id
from app.corpus.export import build_blueprints_export, build_knowledge_export
from app.errors import (
    ArgumentValidationError,
    CartesianJoinError,
    ClickHouseQueryError,
    ClickHouseUnavailableError,
    ColumnScopeError,
    DatabaseNotAllowedError,
    ParseFailedError,
    QueryValidationError,
    TableNotAllowedError,
    TableNotFoundError,
)
from app.semantic_catalog.export import build_catalog_export
from app.session_binding import sid_hash_matches
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
            "Fix the SQL and try again (explainQuery runs the same validation, so "
            "it will not diagnose this — correct the statement first)."
        )
    if isinstance(exc, ArgumentValidationError):
        # A malformed tool ARGUMENT (e.g. getTableSchema's `columns` list) —
        # deliberately NOT routed through the QueryValidationError branch, whose
        # message would tell the model to go fix SQL it never sent. The message
        # is an author-controlled string naming the constraint that was broken.
        return ToolError(f"[{exc.code}] {exc.message}")
    if isinstance(exc, DatabaseNotAllowedError):
        return ToolError(
            f"[{exc.code}] {exc.message}. "
            "Call listDatabases to see which databases are available."
        )
    if isinstance(exc, TableNotAllowedError):
        # The message already names the table and points at listTables — forward
        # it verbatim (an author-controlled string; the table name is catalog
        # metadata, not PII, per D25).
        return ToolError(f"[{exc.code}] {exc.message}")
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
    if isinstance(exc, ColumnScopeError):
        # Forward the service-layer message verbatim: it is an author-controlled
        # string that NAMES the out-of-scope columns (catalog metadata, not PII /
        # cell values per D25), so the model can see WHICH columns it lacks.
        return ToolError(f"[{exc.code}] {exc.message}")
    if isinstance(exc, CartesianJoinError):
        # Forward the service-layer message verbatim: it is an author-controlled
        # string that NAMES the two offending base tables (catalog metadata, not
        # PII / cell values per D25) and tells the model how to fix it (add ON /
        # USING, or wrap a constant side in a subquery), so it can self-correct.
        return ToolError(f"[{exc.code}] {exc.message}")
    if isinstance(exc, ParseFailedError):
        return ToolError(
            f"[{exc.code}] The query could not be parsed for column-scope verification "
            "and was rejected without execution. "
            "Simplify or restructure the SQL so it parses — explainQuery enforces "
            "the same verification and will not bypass it."
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
        "Return the column schema for the specified table, merged with the curated Semantic "
        "Catalog (description, synonyms, units, known values, grain, primary key, join keys, "
        "measures, rules, and disambiguation guidance) when the table is catalogued. "
        "ALWAYS call this before writing a SELECT query against an unfamiliar table. "
        "Knowing the schema tells you exact column names, types (e.g. UInt64, Nullable(String), "
        "DateTime64(3)), and descriptive comments — preventing type-mismatch errors and helping "
        "you write correct WHERE clauses and aggregations. "
        "Columns and catalog entries outside your permitted access scope are omitted. "
        "Use the optional 'columns' argument to fetch the FULL documentation for specific "
        "columns when a schema preview showed them as name/type only: pass the exact column "
        "names, spelled as the schema spells them (matching is case-sensitive). The table-level "
        "sections (description, grain, rules, ambiguities, join keys, measures, temporal, "
        "primary key) are always returned in full — 'columns' narrows the column list only. "
        "Omit 'columns' (or pass an empty list) to get every column. Requested names that are "
        "not available to you are omitted and echoed back under 'columns_not_found'."
    ),
)
def get_table_schema(
    database: Annotated[str, Field(description="The database containing the table")],
    table: Annotated[str, Field(description="The table to describe")],
    columns: Annotated[
        Optional[list[str]],
        Field(
            description=(
                "Optional: return only these columns, with their full documentation. "
                "Exact, case-sensitive column names as spelled in the schema. Omit or "
                "pass an empty list for all columns. Maximum 64 names."
            ),
            max_length=64,
        ),
    ] = None,
) -> dict[str, Any]:
    """Return the merged, scope-filtered column schema for the given table (D83/D84).

    *columns* (M3) narrows the returned column array only — every base section
    always rides complete, and the narrowing is applied after scope/projection
    enforcement, so it can never surface a column an unnarrowed call would hide.
    """
    try:
        return svc_get_table_schema(database, table, columns=columns)
    except Exception as exc:
        raise _domain_to_tool_error(exc) from exc


@mcp.tool(
    name="sampleRows",
    description=(
        "Return a small sample of raw rows from the specified table so you can inspect "
        "actual data values, formats, and nullability before writing analytical queries. "
        "Default sample size is 5 rows; maximum is 50. "
        "Call getTableSchema first to know the column names, then use sampleRows to "
        "understand real data distributions. "
        "If the table has any column outside your permitted access scope, the call is rejected "
        "outright (no partial row projection) — use runQuery with an explicit in-scope column "
        "list instead."
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
        "Execute a read-only SQL query (SELECT or WITH) against ClickHouse "
        "and return results in a compact {columns, rows, row_count, truncated} format. "
        "The server enforces read-only mode, execution time limits, and row caps. "
        "If 'truncated' is true, the result was capped at the server MAX_RESPONSE_ROWS limit "
        "— narrow your query with a more selective WHERE clause or reduce your LIMIT. "
        "Always call getTableSchema before writing a query against an unfamiliar table. "
        "SHOW and DESCRIBE are rejected here — use listTables and getTableSchema for "
        "metadata instead. "
        "explainQuery can validate a well-formed, in-scope query's plan before you run it, "
        "but it enforces the same scope/session/scratch guardrails — it will not bypass a "
        "validation, column-scope, or scratch-session rejection. "
        "INSERT, UPDATE, DELETE, DDL, and external table functions are blocked."
    ),
)
def run_query(
    sql: Annotated[
        str,
        Field(
            description=(
                "Read-only SELECT or WITH statement "
                "(not SHOW / DESCRIBE — use listTables / getTableSchema)"
            ),
        ),
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
        "especially for complex queries. "
        "If EXPLAIN succeeds the query is syntactically valid and safe to run. "
        "explainQuery enforces the SAME scope, session, and scratch-isolation guardrails as "
        "runQuery — it is NOT an escape hatch: a query that fails those gates (column-scope, "
        "scratch-session, or provenance-parse) is rejected here too, so do not retry a "
        "gate-rejected query through explainQuery. "
        "EXPLAIN is fast and cheap — it does not read actual data rows. "
        "THE WHOLE PLAN ARRIVES AS ONE ROW: rows[0][0] is the plan tree as indented, "
        "newline-separated text, so row_count is 1 however many lines the plan has."
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

# Header name for the session identifier, sent by the agent backend per-request.
# The trusted agent backend is expected to always include this header so that
# scratch-table session isolation (D64) applies.
#
# If the header is absent, current_session_id is set to None.  None does NOT
# allow cross-session scratch access: for a scoped caller, the provenance
# extractor (app/sqlparse/provenance.py::_validate_scratch_name) FAILS CLOSED on
# any scratch.* reference when session_id is None — a scratch table whose owning
# session is unknown can never be proven to belong to the caller (D64,
# auth-hardening Slice 1).  This is what closes the "omit the X-Session-Id header
# to read another session's scratch" bypass: the sid_hash binding check below
# only fires when the header is *present*, so the extractor's None-fail-closed is
# the defense for the header-absent case.  (The stdio/local-trust path —
# current_scope is None — skips the extractor entirely and is a separate,
# intentionally-trusted transport.)
SESSION_ID_HEADER = "x-session-id"

# Static service-API-key header accepted on the scope-independent export routes
# only (see JWTAuthMiddleware). It is an EITHER-OR alternative to a Bearer JWT.
SERVICE_KEY_HEADER = "x-service-key"


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


def _session_id_from_scope(scope: dict[str, Any]) -> Optional[str]:
    """Extract the X-Session-Id header from an ASGI scope, or None if absent/empty."""
    target = SESSION_ID_HEADER.encode("latin-1")
    for name, value in scope.get("headers", []):
        if name == target:
            decoded = value.decode("latin-1").strip()
            return decoded or None
    return None


def _service_key_from_scope(scope: dict[str, Any]) -> Optional[str]:
    """Extract the X-Service-Key header from an ASGI scope, or None if absent/empty."""
    target = SERVICE_KEY_HEADER.encode("latin-1")
    for name, value in scope.get("headers", []):
        if name == target:
            decoded = value.decode("latin-1").strip()
            return decoded or None
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
    async task, so ``current_principal`` set here is directly visible to FastMCP
    sync tools (which run inline in the event loop, not on a threadpool).

    Default-deny: every request is rejected unless it carries a valid token,
    except an explicit public-path allowlist (default: /health for k8s probes).
    Statelessness is preserved — the token is verified per request (only JWKS is
    cached), so any replica can serve any request.
    """

    # OAuth Protected Resource Metadata (RFC 9728) is public discovery data — a
    # client fetches it *before* it has a token, so any path under this prefix is
    # always served without auth, regardless of the configured public_paths.
    _OAUTH_PRM_PREFIX = "/.well-known/oauth-protected-resource"

    # Scope-independent export routes that additionally accept a static service
    # API key (X-Service-Key) as an EITHER-OR alternative to a user JWT. These
    # read no principal/scope/session, so a service-key request needs no binding.
    # The service-key bypass is strictly limited to this set.
    _EXPORT_PATHS = ("/catalog/export", "/blueprints/export", "/knowledge/export")

    def __init__(
        self,
        app,
        settings,
        # All three health routes are unauthenticated so k8s probes (and manual
        # checks) reach them without a token: /health/live (liveness),
        # /health/ready (readiness), and the legacy combined /health. Matching is
        # exact (see __call__), so each split path must be listed explicitly.
        public_paths: tuple[str, ...] = ("/health", "/health/live", "/health/ready"),
    ) -> None:
        self.app = app
        self.settings = settings
        self._public_paths = frozenset(p.rstrip("/") or "/" for p in public_paths)
        self._export_paths = frozenset(
            p.rstrip("/") or "/" for p in self._EXPORT_PATHS
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = (scope.get("path", "") or "/").rstrip("/") or "/"
        if path in self._public_paths or path.startswith(self._OAUTH_PRM_PREFIX):
            await self.app(scope, receive, send)
            return

        # Static service-API-key bypass — EXPORT PATHS ONLY, and only when a
        # non-empty key is configured (empty ⇒ feature OFF / fail-closed: an empty
        # configured key must never match, so a missing/empty X-Service-Key can't
        # authenticate). A matching key lets a trusted system hydrator fetch the
        # scope-independent export routes WITHOUT a user JWT; the export handlers
        # read no principal/scope/session, so no contextvars are bound here.
        # On absent/mismatched key we do NOT reject — we fall through to the
        # existing JWT path unchanged, so a valid user JWT still authenticates
        # (EITHER-OR). hmac.compare_digest keeps the compare constant-time.
        #
        # The `path in self._export_paths` membership is exact (both sides
        # rstrip('/')-normalized), and its safety depends on NOTHING being mounted
        # under any of the export paths (e.g. /catalog/export/*): the trailing-slash
        # laxity is only sound because no such sub-route exists, so a normalized
        # match can only ever hit the intended export handler.
        #
        # Compare on BYTES, not str: the header is decoded latin-1, so an attacker
        # can deliver any byte 0x80–0xFF, and hmac.compare_digest raises TypeError
        # on non-ASCII str args — a remotely-triggerable 500 at the auth boundary.
        # Both sides always encode cleanly to utf-8, so the compare is total (and
        # still constant-time). A non-ASCII header simply won't match an ASCII key.
        service_key = self.settings.mcp_service_key
        if path in self._export_paths and service_key:
            presented = _service_key_from_scope(scope)
            if presented is not None and hmac.compare_digest(
                presented.encode("utf-8"), service_key.encode("utf-8")
            ):
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

        # scope ← JWT claim (principal.column_scope)
        # session_id ← X-Session-Id request header (not a JWT claim)
        # The trusted agent backend is expected to always send X-Session-Id;
        # absent header → None → extractor skips scratch-table validation.
        session_id_value = _session_id_from_scope(scope)

        # --- X-Session-Id ↔ sid_hash binding (auth-hardening Slice 1) ---
        # If the request carries a session id, it MUST hash to the token's
        # 'sid_hash' claim; otherwise a caller holding one valid JWT could set
        # X-Session-Id to another user's session and read their scratch/PII (the
        # scratch gate downstream trusts this header). Reject BEFORE binding the
        # session into the context / running any tool. Fail-closed (D63/D64):
        # a present header with a missing/mismatched claim is rejected. A
        # session-less request (no header) is unaffected. Gated by
        # require_sid_binding for the mint-then-enforce transition.
        if session_id_value is not None and self.settings.require_sid_binding:
            claim = principal.claims.get("sid_hash")
            if not isinstance(claim, str) or not claim or not sid_hash_matches(
                session_id_value, claim
            ):
                logger.info(
                    "MCP auth rejected: path=%s status=403 code=SESSION_BINDING_MISMATCH "
                    "sub=%s — X-Session-Id does not match the token's sid_hash claim",
                    path,
                    principal.subject,
                )
                await _send_json_response(
                    send,
                    403,
                    {
                        "error": "Session binding mismatch.",
                        "code": "SESSION_BINDING_MISMATCH",
                    },
                    www_authenticate=_www_authenticate(
                        self.settings,
                        error="insufficient_scope",
                        description="X-Session-Id does not match the token's session binding.",
                    ),
                )
                return

        ctx_token = current_principal.set(principal)
        scope_token = current_scope.set(principal.column_scope)
        session_token = current_session_id.set(session_id_value)
        try:
            await self.app(scope, receive, send)
        finally:
            current_principal.reset(ctx_token)
            current_scope.reset(scope_token)
            current_session_id.reset(session_token)


# ---------------------------------------------------------------------------
# Health endpoints (HTTP transport only)
# ---------------------------------------------------------------------------
# Split by concern so a slow or unreachable ClickHouse can NEVER cycle the MCP
# pod and hard-drop live MCP client connections:
#
#   /health/live   liveness  — process-alive ONLY, no ClickHouse I/O. A ClickHouse
#                              blip must not restart the server, because a restart
#                              hard-drops every in-flight MCP stream on the pod.
#   /health/ready  readiness — reports ClickHouse reachability; an unreachable
#                              backend makes the pod NotReady (pulled from the
#                              Service) but does NOT restart it. Existing streams
#                              on the pod survive; only new routing stops.
#   /health        legacy    — full status, always 200, kept for manual checks /
#                              REST parity with app/routers/health.py.
#
# CRITICAL (why this ever mattered): the ClickHouse ping is BLOCKING (sync
# urllib3). These routes run on the uvicorn event loop, so calling ping() inline
# freezes the loop for the whole round-trip — stalling every concurrent MCP
# request on the pod and, when ClickHouse is slow (e.g. behind CHProxy with a
# stale pooled socket forcing a TLS reconnect), blowing past the probe
# timeoutSeconds so k8s marks the pod NotReady / restarts it and clients see a
# disconnect. We therefore run ping() in a worker thread AND cap it with a cancel
# scope so the route always answers well inside the probe timeout.

# Upper bound on the readiness ping before we report "error". Kept below the
# readiness probe's timeoutSeconds (3s) so the HTTP response always beats the
# probe deadline even when the backend is hung.
_HEALTH_PING_TIMEOUT_S = 2.0


async def _clickhouse_ok() -> bool:
    """Return ClickHouse reachability without ever blocking the event loop.

    ping() is synchronous blocking I/O, so it runs in a worker thread. A cancel
    scope caps the wait at ``_HEALTH_PING_TIMEOUT_S``: if the backend is hung
    (e.g. a blackholed keep-alive socket behind CHProxy), the scope fires, we
    report False, and the orphaned worker thread is abandoned to unwind on its
    own socket timeout (``abandon_on_cancel=True``) instead of holding up the
    probe response.
    """
    from app.clickhouse_client import ping

    ok = False
    with anyio.move_on_after(_HEALTH_PING_TIMEOUT_S):
        # ping() takes NO arguments so it reuses the process-wide cached singleton
        # client; passing settings would rebuild (and re-log) a client per probe.
        ok = await anyio.to_thread.run_sync(ping, abandon_on_cancel=True)
    return ok


@mcp.custom_route("/health/live", methods=["GET"])
async def health_live(request: Request) -> JSONResponse:
    """Liveness: the process is up and serving. Deliberately no ClickHouse check —
    a backend outage must never restart the MCP server."""
    return JSONResponse({"status": "ok", "transport": "http"})


@mcp.custom_route("/health/ready", methods=["GET"])
async def health_ready(request: Request) -> JSONResponse:
    """Readiness: report ClickHouse reachability; 503 when the backend is down.

    A 503 makes k8s pull this pod from the Service until ClickHouse recovers,
    WITHOUT restarting it, so in-flight MCP streams are not hard-dropped.
    """
    ch_ok = await _clickhouse_ok()
    return JSONResponse(
        {
            "status": "ok" if ch_ok else "unavailable",
            "clickhouse": "ok" if ch_ok else "error",
            "transport": "http",
            # Advertise the scratch-write side-channel contract version so a
            # runtime can assert the capability at startup (contract §Q6).
            "scratch_api": "v1",
        },
        status_code=200 if ch_ok else 503,
    )


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Legacy combined health endpoint (manual checks / REST parity).

    Always 200 with ClickHouse status in the body, for backward compatibility
    with anything still probing /health. Kubernetes liveness/readiness now use the
    split /health/live and /health/ready routes above. Like those, the ClickHouse
    ping runs off the event loop so this never blocks the MCP server.

    Does NOT require authentication.
    """
    ch_ok = await _clickhouse_ok()
    return JSONResponse(
        {
            "status": "ok",
            "clickhouse": "ok" if ch_ok else "error",
            "transport": "http",
            # Advertise the scratch-write side-channel contract version so a
            # runtime can assert the capability at startup (contract §Q6).
            "scratch_api": "v1",
        }
    )


# ---------------------------------------------------------------------------
# Catalog-export side-channel (Wave 1a: trusted-runtime bootstrap) — HTTP only
#
# This is a PRIVILEGED, NON-TOOL route (like the scratch routes below): it is NOT
# registered with @mcp.tool, so the agent LLM never sees it and cannot call it
# (invariant #5). The AGENT reaches this MCP HTTP app via its mcp_url, so the
# bootstrap endpoint MUST live here (not on the separate REST app) or the fetch
# 404s in every real split-deployment topology.
#
# It rides on the same MCP app behind JWTAuthMiddleware, so a valid Bearer JWT is
# required (default-deny: not in the middleware's public_paths). It is
# scope-INDEPENDENT and session-INDEPENDENT by design: it returns the FULL catalog
# (every table, every column) for any authenticated principal, and never consults
# the caller's column scope or X-Session-Id. That is safe because this is
# trusted-runtime bootstrap data consumed by the agent process, NOT model-facing
# data (contrast getTableSchema, which scope-filters columns).
# ---------------------------------------------------------------------------

@mcp.custom_route("/catalog/export", methods=["GET"])
async def catalog_export_route(request: Request) -> JSONResponse:
    """Return the full, scope-independent semantic catalog export (D-Wave1a).

    Auth is enforced by JWTAuthMiddleware (a valid Bearer token is required; the
    path is default-deny). The body is ``build_catalog_export()`` verbatim: the
    ``catalog_sha`` content fingerprint plus every catalogued table's full overlay.
    No column-scope filtering is applied — every valid principal gets the identical
    payload.
    """
    return JSONResponse(build_catalog_export())


# ---------------------------------------------------------------------------
# Corpus-export side-channels (Phase 1: MCP-owned blueprint + knowledge corpus)
#
# Like /catalog/export above, these are PRIVILEGED, NON-TOOL routes: they are NOT
# registered with @mcp.tool, so the agent LLM never sees them and cannot call them
# (invariant #5). The AGENT reaches this MCP HTTP app via its mcp_url, so these
# bootstrap endpoints MUST live here (not on the separate REST app). They ride the
# same MCP app behind JWTAuthMiddleware, so a valid Bearer JWT is required
# (default-deny: not in the middleware's public_paths). They are
# scope-INDEPENDENT and session-INDEPENDENT: every authenticated principal gets
# the identical full corpus. Blueprints and knowledge are versioned independently
# (separate SHAs), so they are served by two distinct routes.
# ---------------------------------------------------------------------------

@mcp.custom_route("/blueprints/export", methods=["GET"])
async def blueprints_export_route(request: Request) -> JSONResponse:
    """Return the full, scope-independent blueprint corpus export (Phase 1).

    Auth is enforced by JWTAuthMiddleware (a valid Bearer token is required; the
    path is default-deny). The body is ``build_blueprints_export()`` verbatim: the
    ``blueprints_sha`` content fingerprint plus every blueprint entry (each stamped
    with the constant ``source: "mcp"`` / ``verified: true``). No column-scope
    filtering is applied — every valid principal gets the identical payload.
    """
    return JSONResponse(build_blueprints_export())


@mcp.custom_route("/knowledge/export", methods=["GET"])
async def knowledge_export_route(request: Request) -> JSONResponse:
    """Return the full, scope-independent knowledge corpus export (Phase 1).

    Auth is enforced by JWTAuthMiddleware (a valid Bearer token is required; the
    path is default-deny). The body is ``build_knowledge_export()`` verbatim: the
    ``knowledge_sha`` content fingerprint plus every knowledge entry (each stamped
    with the constant ``source: "mcp"`` / ``verified: true``). No column-scope
    filtering is applied — every valid principal gets the identical payload.
    """
    return JSONResponse(build_knowledge_export())


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
# Scratch-write side-channel (table-intermediate Slice 1) — HTTP transport only
#
# These are PRIVILEGED, NON-TOOL routes (D19).  They are NOT registered with
# @mcp.tool — the agent LLM never sees them and cannot call them (invariant #5).
# They ride as custom HTTP routes on the same MCP app, so JWTAuthMiddleware
# validates the JWT and (D92) binds `current_session_id` from X-Session-Id BEFORE
# a handler runs.  The scratch table name derives ONLY from that bound session_id
# (never the request body), so a caller can only write to its OWN scratch
# namespace (isolation invariant #2).  The ClickHouse write itself runs under a
# distinct, server-side scratch-only credential (invariant #1) via
# app.scratch_ingest.  Row cells are native-bulk-inserted as DATA (invariant #4).
# ---------------------------------------------------------------------------

from app.scratch_ingest import (  # noqa: E402
    ScratchWriteError,
    drop as scratch_drop,
    materialize as scratch_materialize,
)
from app.upload_ingest import (  # noqa: E402
    UploadMappingError,
    UploadParseError,
    UploadTooLargeError,
    apply_mapping,
    parse_upload,
)


def _scratch_error(status_code: int, code: str, message: str) -> JSONResponse:
    """Consistent JSON error shape for the scratch routes."""
    return JSONResponse({"error": message, "code": code}, status_code=status_code)


@mcp.custom_route("/scratch/v1/materialize", methods=["POST"])
async def scratch_materialize_route(request: Request) -> JSONResponse:
    """Create a session-scoped scratch table and native-bulk-load the rows.

    Auth: JWTAuthMiddleware (JWT validated + X-Session-Id ↔ sid_hash bound, D92).
    The table name is derived from the bound session_id ONLY — a body-supplied
    table/session is ignored.  Returns {"table", "row_count"}.
    """
    session_id = current_session_id.get()
    if not session_id:
        return _scratch_error(
            400,
            "SCRATCH_SESSION_MISSING",
            "X-Session-Id is required to materialize a scratch table.",
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _scratch_error(
            400, "SCRATCH_MATERIALIZE_REJECTED", "Request body must be valid JSON."
        )
    if not isinstance(body, dict):
        return _scratch_error(
            400, "SCRATCH_MATERIALIZE_REJECTED", "Request body must be a JSON object."
        )
    columns = body.get("columns")
    rows = body.get("rows")
    settings_now = get_settings()
    try:
        # ClickHouse I/O is blocking; run it off the event loop so the MCP app
        # stays responsive.  session_id is passed explicitly (not re-read from the
        # context inside the thread).
        result = await anyio.to_thread.run_sync(
            scratch_materialize, session_id, columns, rows, settings_now
        )
    except ScratchWriteError as exc:
        return _scratch_error(exc.status_code, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001
        logger.exception("scratch materialize failed: %s", exc)
        return _scratch_error(
            500, "SCRATCH_MATERIALIZE_REJECTED", "An internal error occurred."
        )
    return JSONResponse(result)


@mcp.custom_route("/scratch/v1/drop", methods=["POST"])
async def scratch_drop_route(request: Request) -> JSONResponse:
    """Best-effort drop of a scratch table the caller's session owns.

    A cross-session drop (table prefix != s_<bound-session>_) is rejected 403
    SCRATCH_SESSION_VIOLATION.  The TTL is the real cleanup guarantee.
    """
    session_id = current_session_id.get()
    if not session_id:
        return _scratch_error(
            400, "SCRATCH_SESSION_MISSING", "X-Session-Id is required to drop a scratch table."
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _scratch_error(
            400, "SCRATCH_MATERIALIZE_REJECTED", "Request body must be valid JSON."
        )
    table = body.get("table") if isinstance(body, dict) else None
    settings_now = get_settings()
    try:
        result = await anyio.to_thread.run_sync(
            scratch_drop, session_id, table, settings_now
        )
    except ScratchWriteError as exc:
        return _scratch_error(exc.status_code, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001
        logger.exception("scratch drop failed: %s", exc)
        return _scratch_error(500, "SCRATCH_MATERIALIZE_REJECTED", "An internal error occurred.")
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# External-dataset upload front door (UI Slice 4) — HTTP transport only.
#
# The parse + column-mapping half in front of the built scratch materialize
# back-half.  Same posture as the scratch routes above: PRIVILEGED, NON-TOOL
# custom routes (the agent LLM never sees them), behind JWTAuthMiddleware so the
# session is D92-bound from X-Session-Id BEFORE a handler runs.  The materialized
# table name derives ONLY from that bound session_id (never the body), so a caller
# can only write to its OWN scratch namespace.  Both routes are multipart:
#   POST /scratch/v1/analyze  — parse + preview (NO materialize)
#   POST /scratch/v1/upload   — parse + rename + materialize (authoritative)
# A byte cap is enforced BEFORE the whole body is buffered (Content-Length
# pre-check + per-part max_part_size + a bounded read), and again on the read
# bytes; the row cap (scratch_max_rows) is enforced during parse and inside
# materialize.
# ---------------------------------------------------------------------------

# Slack over the raw byte cap to allow for the multipart boundary + part headers
# when comparing against the request's declared Content-Length.
_MULTIPART_OVERHEAD_BYTES = 64 * 1024


async def _read_upload_part(request: Request, max_bytes: int) -> tuple[bytes, str, Any]:
    """Read the multipart body under a strict byte cap: (file bytes, filename, form).

    Resource-exhaustion hardening (UI Slice 4 §3/§7). The cap is enforced in five
    layers so an authenticated caller cannot make the server buffer/spool an
    oversized body BEFORE the 413, regardless of Content-Length honesty or how the
    body is split into parts:
      1. A declared ``Content-Length`` over the hard limit is rejected WITHOUT
         reading the stream at all (honest-client fast path).
      2. A bounded ASGI receive-wrapper caps TOTAL ingested bytes (RAM AND disk) in
         one place, so a chunked / lying-Content-Length body trips DURING form
         parsing — before a huge file part finishes spooling to disk, and before a
         multiplicity of text fields fills RAM (the pre-check can't fire without a
         Content-Length).
      3. ``max_files=2, max_fields=4`` cap the PART COUNT — only ``file`` +
         ``mapping`` are ever legitimate, so ~999 text fields (Starlette's default
         ``max_fields``) can never accumulate in FormData.
      4. ``max_part_size=64 KiB`` caps the non-file ``mapping`` field (a 512-column
         mapping is ≤ ~30 KiB); the file part streams/spools and is NOT truncated
         by this.
      5. The file part is pulled into RAM with a BOUNDED read of at most
         ``max_bytes + 1`` bytes, then length-checked.

    Raises ``UploadTooLargeError`` (→ 413 UPLOAD_TOO_LARGE) when a cap is exceeded,
    or ``UploadParseError`` (→ 400) for a non-multipart / missing-file body.
    """
    hard_limit = max_bytes + _MULTIPART_OVERHEAD_BYTES

    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > hard_limit:
        raise UploadTooLargeError("Uploaded body exceeds the maximum allowed size.")

    # Layer 2: bounded receive-wrapper — the total-bytes backstop.  It counts every
    # ASGI body chunk as `form()` pulls it and raises the moment the cumulative
    # ingested size crosses the hard limit, so nothing (RAM or disk spool) grows
    # past it even when Content-Length is absent/lying or the body is chunked.
    total = 0
    orig_receive = request.receive

    async def _bounded_receive() -> Any:
        nonlocal total
        message = await orig_receive()
        total += len(message.get("body", b""))
        if total > hard_limit:
            raise UploadTooLargeError("Uploaded body exceeds the maximum allowed size.")
        return message

    request = Request(request.scope, _bounded_receive)

    try:
        form = await request.form(max_files=2, max_fields=4, max_part_size=64 * 1024)
    except UploadTooLargeError:
        raise  # our backstop tripped inside form parsing — keep it a 413
    except (MultiPartException, StarletteHTTPException) as exc:
        # Starlette wraps a max_part_size / max_files / max_fields violation into
        # HTTPException(400); surface a resource-cap violation as the byte-cap 413
        # rather than a generic parse 400.  The substring checks are pinned to
        # starlette==0.50.0.
        detail = str(getattr(exc, "detail", "") or exc)
        if "exceeded maximum size" in detail or "Too many" in detail:
            raise UploadTooLargeError(
                "Uploaded file exceeds the maximum allowed size."
            ) from exc
        raise UploadParseError("Request must be multipart/form-data.") from exc
    except Exception as exc:  # noqa: BLE001
        raise UploadParseError("Request must be multipart/form-data.") from exc
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise UploadParseError("Missing 'file' upload part.")
    # Bounded read: never pull more than max_bytes + 1 into RAM even though the
    # spooled file part is not covered by max_part_size.
    content = await upload.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise UploadTooLargeError("Uploaded file exceeds the maximum allowed size.")
    filename = getattr(upload, "filename", "") or ""
    return content, filename, form


@mcp.custom_route("/scratch/v1/analyze", methods=["POST"])
async def scratch_analyze_route(request: Request) -> JSONResponse:
    """Parse an uploaded CSV/XLSX and return a preview — NO materialize.

    Returns ``{"columns": [{name,type}], "row_count": N, "sample_rows": [...]}``
    so the browser can render the column-mapping UI.  Fails 413 SCRATCH_TOO_LARGE
    if the parsed row count exceeds scratch_max_rows (before any mapping UI is
    shown), and 413 UPLOAD_TOO_LARGE if the file is over the byte cap.
    """
    session_id = current_session_id.get()
    if not session_id:
        return _scratch_error(
            400, "SCRATCH_SESSION_MISSING", "X-Session-Id is required to analyze an upload."
        )
    settings_now = get_settings()
    try:
        content, filename, _ = await _read_upload_part(
            request, settings_now.upload_max_bytes
        )
    except UploadTooLargeError as exc:
        return _scratch_error(413, "UPLOAD_TOO_LARGE", exc.message)
    except UploadParseError as exc:
        return _scratch_error(400, "UPLOAD_PARSE_ERROR", exc.message)
    try:
        # Parsing (esp. openpyxl) is blocking CPU work; run it off the event loop.
        columns, rows = await anyio.to_thread.run_sync(
            parse_upload, content, filename, settings_now.scratch_max_rows
        )
    except ScratchWriteError as exc:  # ScratchTooLargeError → 413 SCRATCH_TOO_LARGE
        return _scratch_error(exc.status_code, exc.code, exc.message)
    except UploadParseError as exc:
        return _scratch_error(400, "UPLOAD_PARSE_ERROR", exc.message)
    except Exception as exc:  # noqa: BLE001
        logger.exception("upload analyze failed: %s", exc)
        return _scratch_error(500, "UPLOAD_PARSE_ERROR", "An internal error occurred.")
    return JSONResponse(
        {
            "columns": columns,
            "row_count": len(rows),
            "sample_rows": rows[:10],
        }
    )


@mcp.custom_route("/scratch/v1/upload", methods=["POST"])
async def scratch_upload_route(request: Request) -> JSONResponse:
    """Parse + rename + materialize an uploaded CSV/XLSX (authoritative).

    Re-parses the file server-side (Option B — stateless), applies the column
    mapping, and materializes a session-scoped scratch table.  Returns the
    materialize shape verbatim: ``{"table": "scratch.s_<sid>_bp_<uuid>",
    "row_count": N}``.
    """
    session_id = current_session_id.get()
    if not session_id:
        return _scratch_error(
            400, "SCRATCH_SESSION_MISSING", "X-Session-Id is required to upload a scratch table."
        )
    settings_now = get_settings()
    try:
        content, filename, form = await _read_upload_part(
            request, settings_now.upload_max_bytes
        )
    except UploadTooLargeError as exc:
        return _scratch_error(413, "UPLOAD_TOO_LARGE", exc.message)
    except UploadParseError as exc:
        return _scratch_error(400, "UPLOAD_PARSE_ERROR", exc.message)
    mapping_raw = form.get("mapping")
    try:
        mapping = json.loads(mapping_raw) if mapping_raw else {}
    except (TypeError, ValueError):
        return _scratch_error(
            400, "UPLOAD_MAPPING_INVALID", "The 'mapping' field must be valid JSON."
        )
    if not isinstance(mapping, dict):
        return _scratch_error(
            400, "UPLOAD_MAPPING_INVALID", "The 'mapping' field must be a JSON object."
        )

    def _do_upload() -> dict[str, Any]:
        columns, rows = parse_upload(content, filename, settings_now.scratch_max_rows)
        columns, rows = apply_mapping(columns, rows, mapping)
        # The table name derives ONLY from the D92-bound session_id, never the body.
        return scratch_materialize(session_id, columns, rows, settings_now)

    try:
        # Blocking parse + ClickHouse DDL/insert — run off the event loop.
        result = await anyio.to_thread.run_sync(_do_upload)
    except ScratchWriteError as exc:  # incl. ScratchTooLargeError (413 SCRATCH_TOO_LARGE)
        return _scratch_error(exc.status_code, exc.code, exc.message)
    except UploadParseError as exc:
        return _scratch_error(400, "UPLOAD_PARSE_ERROR", exc.message)
    except UploadMappingError as exc:
        return _scratch_error(400, "UPLOAD_MAPPING_INVALID", exc.message)
    except Exception as exc:  # noqa: BLE001
        logger.exception("upload failed: %s", exc)
        return _scratch_error(500, "SCRATCH_MATERIALIZE_REJECTED", "An internal error occurred.")
    return JSONResponse(result)


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
