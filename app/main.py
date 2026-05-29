"""FastAPI application entry point for the ClickHouse REST API."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.routers import health, query, schema

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # noqa: RUF029
    """Startup / shutdown logic.

    On startup we eagerly create the ClickHouse client so that connection
    errors surface immediately rather than on the first request.
    """
    from app.clickhouse_client import get_client

    logger.info("Initialising ClickHouse client...")
    try:
        get_client(settings)
        logger.info("ClickHouse client initialised successfully.")
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to initialise ClickHouse client: %s", exc)
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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler that prevents raw stack traces from leaking to clients."""
    logger.exception("Unhandled exception for %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "An internal server error occurred.", "code": "INTERNAL_ERROR"},
    )


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
            "url": settings.public_base_url,
            "description": "Production — set PUBLIC_BASE_URL env var to your HTTPS endpoint",
        }
    ]

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]
