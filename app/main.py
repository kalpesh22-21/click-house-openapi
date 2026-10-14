"""FastAPI application entry point for the ClickHouse REST API."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.employee_access import EmployeeAccessError
from app.error_logging import configure_logging, log_failure
from app.errors import (
    ArgumentValidationError,
    CartesianJoinError,
    ClickHouseAPIError,
    ClickHouseQueryError,
    ClickHouseUnavailableError,
    ColumnScopeError,
    DatabaseNotAllowedError,
    ParseFailedError,
    QueryValidationError,
    TableNotAllowedError,
    TableNotFoundError,
)
from app.routers import health, query, schema

# ---------------------------------------------------------------------------
# Logging — use a default level at import time; the lifespan reconfigures
# once settings are loaded.  This avoids a hard import-time get_settings()
# call that would crash if required config (e.g. OIDC) is absent (stdio MCP uses
# a different entrypoint, but we still want main.py to be importable by tooling).
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown logic.

    On startup we:
      1. Check that OIDC/JWT auth is configured — the REST API always requires
         auth.  Missing config would leave the service unable to verify any
         token, so we log a CRITICAL message and exit rather than serve routes
         that can never authenticate.
      2. Eagerly create the ClickHouse client so connection errors surface
         immediately rather than on the first request.
    """
    from app.clickhouse_client import get_client

    settings = get_settings()

    # Reconfigure logging now that we have the real log level from settings.
    configure_logging(settings.log_level)

    # Fail closed: the REST API has no unauthenticated mode.  If OIDC/JWT auth
    # is not configured, refuse to serve rather than allow open access.
    if not settings.auth_configured():
        logger.critical(
            "REST API requires OIDC config. Set OIDC_JWKS_URL, OIDC_ISSUER, and "
            "OIDC_AUDIENCE to enable JWT validation, then restart."
        )
        sys.exit(1)

    # Fail closed (A-M1): validate the Semantic Catalog's RLS projection at
    # startup so a missing/malformed RLS table (which would leak client_code /
    # proc_center / version) refuses to boot rather than surfacing at first
    # request. get_semantic_catalog() runs _validate_rls_projection() and caches.
    from app.semantic_catalog.loader import (
        CatalogValidationError,
        get_semantic_catalog,
    )

    try:
        get_semantic_catalog()
    except CatalogValidationError as exc:
        logger.critical("Semantic Catalog RLS validation failed: %s", exc)
        sys.exit(1)

    if settings.trino_enabled:
        logger.info("Experimental Trino backend selected; metadata comes from the semantic catalog.")
    else:
        logger.info("Initialising ClickHouse client...")
        try:
            get_client()
            logger.info("ClickHouse client initialised successfully.")
        except Exception as exc:  # noqa: BLE001
            log_failure(logger, "startup.clickhouse", exc, code="CLICKHOUSE_UNAVAILABLE")
            # Don't crash — the health probe will report degraded status.

    yield

    logger.info("Shutting down.")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ClickHouse Query API",
    version="1.0.0",
    description=(
        "A read-only REST API that exposes ClickHouse schema discovery and query execution "
        "endpoints designed for consumption by a ChatGPT Custom GPT Action. "
        "All routes require a Bearer token. SQL is validated server-side — only SELECT, WITH, "
        "EXPLAIN, SHOW, DESCRIBE, and DESC statements are permitted. "
        "Workflow for the LLM: "
        "(1) listDatabases → (2) listTables → (3) getTableSchema → (4) optionally sampleRows → "
        "(5) explainQuery to validate → (6) runQuery."
    ),
    lifespan=lifespan,
    # Disable the default /docs redirect so only /openapi.json is used by GPT Actions.
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


# Map each transport-agnostic domain error to its HTTP status.  Registering one
# handler on the base class catches every subclass (Starlette walks the MRO), so
# ANY route that lets a domain error propagate — not just /query — returns a
# clean status instead of a 500.  (The /query and some schema routes still map
# these explicitly; this is the backstop for the rest.)
_DOMAIN_ERROR_STATUS: dict[type, int] = {
    QueryValidationError: 400,
    ArgumentValidationError: 400,
    CartesianJoinError: 400,
    ClickHouseQueryError: 400,
    # ADR-0002/H3: scratch-session isolation now fires on REST too (a scratch
    # reference on a scope-less REST request fails closed). These two never fired
    # on REST before (scope was always None), so they were absent from this map and
    # would have surfaced as a generic 500. Map them to clean 4xx:
    #   ColumnScopeError → 403 (SCRATCH_SESSION_VIOLATION / COLUMN_SCOPE_VIOLATION)
    #   ParseFailedError → 400 (PARSE_FAILED_CLOSED)
    ColumnScopeError: 403,
    ParseFailedError: 400,
    DatabaseNotAllowedError: 403,
    TableNotAllowedError: 403,
    TableNotFoundError: 404,
    ClickHouseUnavailableError: 502,
    # EmployeeAccessError subclasses ClickHouseUnavailableError, but the handler
    # looks up the EXACT type — register it so a fail-closed access-provisioning
    # error returns 502 (matching the run_query route) instead of 500.
    EmployeeAccessError: 502,
}


@app.exception_handler(ClickHouseAPIError)
async def domain_error_handler(request: Request, exc: ClickHouseAPIError) -> JSONResponse:
    """Translate a domain error to its HTTP status (default 500 if unmapped)."""
    status_code = _DOMAIN_ERROR_STATUS.get(type(exc), 500)
    log_failure(logger, "rest.domain", exc, status=status_code)
    return JSONResponse(
        status_code=status_code,
        content={"error": exc.message, "code": exc.code},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler that prevents raw stack traces from leaking to clients."""
    log_failure(logger, "rest.unhandled", exc, status=500)
    return JSONResponse(
        status_code=500,
        content={"error": "An internal server error occurred.", "code": "INTERNAL_ERROR"},
    )


@app.exception_handler(StarletteHTTPException)
async def logged_http_error(request: Request, exc: StarletteHTTPException):
    # Routes convert domain errors to HTTPException; those bypass the domain
    # handler. Preserve FastAPI's response and authentication headers.
    code = exc.detail.get("code", "HTTP_ERROR") if isinstance(exc.detail, dict) else "HTTP_ERROR"
    log_failure(logger, "rest.http", exc, code=code, status=exc.status_code)
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def logged_validation_error(request: Request, exc: RequestValidationError):
    log_failure(logger, "rest.validation", exc, code="REQUEST_VALIDATION_ERROR", status=422)
    return await request_validation_exception_handler(request, exc)


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(health.router)
app.include_router(schema.router)
app.include_router(query.router)


# ---------------------------------------------------------------------------
# Custom OpenAPI — inject servers block for GPT Actions
# ---------------------------------------------------------------------------


def custom_openapi() -> dict[str, Any]:
    """Return an OpenAPI schema with the public base URL in the servers block.

    ChatGPT Custom GPT Actions require a servers entry with the public HTTPS
    URL so the GPT knows where to send requests.
    """
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    schema["servers"] = [
        {
            "url": get_settings().public_base_url,
            "description": "Production — set PUBLIC_BASE_URL env var to your HTTPS endpoint",
        }
    ]

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]
