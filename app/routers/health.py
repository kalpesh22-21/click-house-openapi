"""Health check route — used by Kubernetes liveness/readiness probes."""

from __future__ import annotations

from fastapi import APIRouter

from app.clickhouse_client import ping
from app.config import get_settings
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
    settings = get_settings()
    ch_ok = ping(settings)
    return HealthResponse(
        status="ok",
        clickhouse="ok" if ch_ok else "error",
    )
