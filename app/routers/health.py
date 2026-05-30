"""Health check route — used by Kubernetes liveness/readiness probes."""

from __future__ import annotations

from fastapi import APIRouter

from app.clickhouse_client import ping
from app.models import HealthResponse

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    operation_id="healthCheck",
    summary="Liveness and readiness probe",
    description=(
        "Returns HTTP 200 with status='ok' when the service is running. "
        "The 'clickhouse' field reports whether ClickHouse is reachable. "
        "This endpoint does NOT require authentication and is intended for "
        "Kubernetes liveness/readiness probes."
    ),
    # Exclude from the LLM-facing OpenAPI tags by keeping it in a separate tag.
    include_in_schema=True,
)
def health_check() -> HealthResponse:
    """Check service liveness and ClickHouse connectivity."""
    # Call ping() with NO arguments so it reuses the process-wide cached
    # singleton client. Passing an explicit settings routes through
    # get_client(settings) -> _build_client, which constructs (and logs) a
    # brand-new client on every probe — liveness/readiness probes run every
    # few seconds, so that churns connections continuously.
    ch_ok = ping()
    return HealthResponse(
        status="ok",
        clickhouse="ok" if ch_ok else "error",
    )
