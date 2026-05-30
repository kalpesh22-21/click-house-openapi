"""Query execution routes: runQuery, explainQuery.

These are thin HTTP adapters.  All business logic lives in app.service.
Domain errors from service.py are mapped to HTTP status codes here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.auth import require_api_key
from app.config import Settings, get_settings
from app.errors import (
    ClickHouseQueryError,
    ClickHouseUnavailableError,
    QueryValidationError,
)
from app.models import ExplainRequest, QueryRequest, QueryResponse
from app.service import explain_query as svc_explain_query, run_query as svc_run_query

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Query Execution"])


def _map_query_error(exc: Exception) -> HTTPException:
    """Map a domain query error to an HTTPException with the correct status code."""
    if isinstance(exc, QueryValidationError):
        return HTTPException(
            status_code=400,
            detail={"error": exc.message, "code": exc.code},
        )
    if isinstance(exc, ClickHouseQueryError):
        return HTTPException(
            status_code=400,
            detail={"error": exc.message, "code": exc.code},
        )
    if isinstance(exc, ClickHouseUnavailableError):
        return HTTPException(
            status_code=502,
            detail={"error": exc.message, "code": exc.code},
        )
    # Unexpected — re-raise so the global handler picks it up.
    raise exc  # type: ignore[misc]


@router.post(
    "/query",
    response_model=QueryResponse,
    operation_id="runQuery",
    summary="Execute a read-only SQL query",
    description=(
        "Executes a read-only SQL statement (SELECT, WITH, SHOW, DESCRIBE) against ClickHouse "
        "and returns results in a compact column-oriented format to minimise token usage. "
        "The server enforces read-only mode, execution time limits, and row caps regardless "
        "of what the SQL contains. "
        "If 'truncated' is true in the response, the result was capped at the server's "
        "MAX_RESPONSE_ROWS limit — add a more selective WHERE clause or reduce your LIMIT. "
        "Always call getTableSchema before writing a query against an unfamiliar table. "
        "If you get a query error, call explainQuery first to validate your SQL."
    ),
    dependencies=[Depends(require_api_key)],
)
def run_query(
    body: QueryRequest,
    settings: Settings = Depends(get_settings),
) -> QueryResponse:
    """Validate, optionally limit, and execute *body.sql*."""
    try:
        result = svc_run_query(body.sql, body.limit, settings)
    except (QueryValidationError, ClickHouseQueryError, ClickHouseUnavailableError) as exc:
        raise _map_query_error(exc) from exc

    return QueryResponse(
        columns=result["columns"],
        rows=result["rows"],
        row_count=result["row_count"],
        truncated=result["truncated"],
    )


@router.post(
    "/query/explain",
    response_model=QueryResponse,
    operation_id="explainQuery",
    summary="EXPLAIN a query without executing it",
    description=(
        "Runs EXPLAIN on the provided SQL statement and returns the query execution plan. "
        "Use this to validate your SQL syntax and check the query plan BEFORE calling runQuery, "
        "especially for complex queries or when runQuery returns an error. "
        "If EXPLAIN succeeds, the query is syntactically valid and safe to run. "
        "EXPLAIN does not read actual data rows, so it is fast and cheap."
    ),
    dependencies=[Depends(require_api_key)],
)
def explain_query(
    body: ExplainRequest,
    settings: Settings = Depends(get_settings),
) -> QueryResponse:
    """Wrap *body.sql* in EXPLAIN and return the plan."""
    try:
        result = svc_explain_query(body.sql, settings)
    except (QueryValidationError, ClickHouseQueryError, ClickHouseUnavailableError) as exc:
        raise _map_query_error(exc) from exc

    return QueryResponse(
        columns=result["columns"],
        rows=result["rows"],
        row_count=result["row_count"],
        truncated=result["truncated"],
    )
