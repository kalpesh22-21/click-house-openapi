"""Query execution routes: runQuery, explainQuery."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.auth import require_api_key
from app.clickhouse_client import execute_query
from app.config import Settings, get_settings
from app.models import ExplainRequest, QueryRequest, QueryResponse
from app.security import validate_and_sanitize

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Query Execution"])


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
    """Validate, optionally limit, and execute *body.sql*.

    Results are truncated at MAX_RESPONSE_ROWS.  The ``truncated`` flag tells
    the caller whether data was cut off.
    """
    sql = validate_and_sanitize(body.sql, settings.default_limit)

    # If the caller supplied an explicit limit, re-apply it (the sanitiser may
    # have already injected one; we override with the caller's preference when
    # it is more restrictive).
    if body.limit is not None:
        effective_limit = min(body.limit, settings.max_response_rows)
        # Replace any existing injected limit with the caller's value.
        # Simple append approach: wrap as subquery is fragile; instead rely on
        # the fact that validate_and_sanitize only appended a LIMIT when there
        # was none — so if body.limit is set we can safely replace the tail.
        import re as _re
        sql = _re.sub(
            r"\bLIMIT\s+\d+\s*$",
            f"LIMIT {effective_limit}",
            sql,
            flags=_re.IGNORECASE,
        )

    columns, rows = execute_query(sql, settings)

    truncated = len(rows) > settings.max_response_rows
    if truncated:
        rows = rows[: settings.max_response_rows]

    return QueryResponse(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
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
    """Wrap *body.sql* in EXPLAIN and return the plan.

    The inner SQL is still validated by the guardrails so that the user cannot
    sneak a mutation inside an EXPLAIN wrapper.
    """
    # Validate the inner SQL first (strip its injected LIMIT — EXPLAIN doesn't need it).
    inner_sql = validate_and_sanitize(body.sql, settings.default_limit)

    explain_sql = f"EXPLAIN {inner_sql}"

    columns, rows = execute_query(explain_sql, settings)

    return QueryResponse(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=False,
    )
