"""Business logic layer — single code path shared by REST and MCP transports.

All six operations (list_databases, list_tables, get_table_schema, sample_rows,
run_query, explain_query) live here.  Neither the FastAPI routers nor the MCP
server should contain business logic — they are thin transport adapters that
call these functions and translate domain errors into transport-specific error
responses.

Key design decisions:
- No FastAPI or MCP imports.  Domain errors use app.errors, not HTTPException.
- All paths go through execute_query(), which applies readonly_settings() on
  every query, ensuring safety caps are applied regardless of transport.
- validate_and_sanitize() is called for every user-supplied SQL (run_query,
  explain_query).  Its HTTPException is caught here and re-raised as a
  transport-agnostic QueryValidationError.
- ALLOWED_DATABASES allowlist enforcement raises DatabaseNotAllowedError, not
  HTTPException, so the MCP transport can map it to a clear tool error.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

from app.clickhouse_client import execute_query
from app.config import Settings, get_settings
from app.errors import (
    ClickHouseQueryError,
    ClickHouseUnavailableError,
    DatabaseNotAllowedError,
    QueryValidationError,
    TableNotFoundError,
)
from app.security import validate_and_sanitize

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Matches a trailing LIMIT clause with an optional OFFSET clause, e.g.:
#   "LIMIT 100"  or  "LIMIT 100 OFFSET 20"
# When a caller-supplied limit overrides the query, only the LIMIT value is
# replaced; the OFFSET is preserved so paging queries are not silently broken.
# The max_result_rows setting acts as a backstop regardless of what LIMIT remains.
_LIMIT_TAIL_RE = re.compile(r"\bLIMIT\s+\d+(\s+OFFSET\s+\d+)?\s*$", re.IGNORECASE)


def _check_database_allowed(database: str, settings: Settings) -> None:
    """Raise DatabaseNotAllowedError if *database* is not in the allowlist."""
    allowed = settings.allowed_databases_list()
    if allowed is not None and database not in allowed:
        raise DatabaseNotAllowedError(
            message=(
                f"Access to database '{database}' is not permitted. "
                f"Allowed databases: {', '.join(allowed)}"
            ),
            code="DATABASE_NOT_ALLOWED",
        )


def _execute(
    sql: str,
    settings: Settings,
    parameters: dict[str, Any] | None = None,
) -> tuple[list[str], list[list[Any]]]:
    """Thin wrapper around execute_query that translates HTTPExceptions to domain errors.

    execute_query() currently raises HTTPException(400) for query errors and
    HTTPException(502) for connectivity errors.  We re-raise those as
    transport-agnostic domain errors so the MCP layer never sees HTTPException.
    """
    try:
        return execute_query(sql, settings, parameters)
    except HTTPException as exc:
        if exc.status_code == 502:
            raise ClickHouseUnavailableError(
                message=exc.detail.get("error", "ClickHouse unavailable"),
                code=exc.detail.get("code", "CLICKHOUSE_UNAVAILABLE"),
            ) from exc
        # 400 — query error
        raise ClickHouseQueryError(
            message=exc.detail.get("error", "ClickHouse query error"),
            code=exc.detail.get("code", "CLICKHOUSE_QUERY_ERROR"),
        ) from exc


def _compact_result(
    columns: list[str],
    rows: list[list[Any]],
    max_response_rows: int,
    truncated_override: bool | None = None,
) -> dict[str, Any]:
    """Return the compact {columns, rows, row_count, truncated} shape."""
    truncated = len(rows) > max_response_rows
    if truncated:
        rows = rows[:max_response_rows]
    if truncated_override is not None:
        truncated = truncated_override
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------


def list_databases(settings: Settings | None = None) -> list[dict[str, str]]:
    """Return all databases accessible through this API.

    Filters results against the ALLOWED_DATABASES allowlist so only
    permitted databases are returned.

    Returns a list of dicts, each with a single 'name' key.
    """
    if settings is None:
        settings = get_settings()

    _, rows = _execute("SELECT name FROM system.databases ORDER BY name", settings)

    allowed = settings.allowed_databases_list()
    return [
        {"name": name}
        for (name,) in rows
        if allowed is None or name in allowed
    ]


def list_tables(
    database: str,
    settings: Settings | None = None,
) -> list[dict[str, str]]:
    """Return all tables in *database*.

    Raises:
        DatabaseNotAllowedError: if *database* is not in the allowlist.
    """
    if settings is None:
        settings = get_settings()

    _check_database_allowed(database, settings)

    sql = (
        "SELECT database, name, engine "
        "FROM system.tables "
        "WHERE database = {db:String} "
        "ORDER BY name"
    )
    _, rows = _execute(sql, settings, parameters={"db": database})

    return [
        {"database": row[0], "name": row[1], "engine": row[2]}
        for row in rows
    ]


def get_table_schema(
    database: str,
    table: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Return column schema for *database*.*table*.

    Returns a dict with keys: database, table, columns (list of {name, type, comment}).

    Raises:
        DatabaseNotAllowedError: if *database* is not in the allowlist.
        TableNotFoundError:      if the table has no columns (does not exist).
    """
    if settings is None:
        settings = get_settings()

    _check_database_allowed(database, settings)

    sql = (
        "SELECT name, type, comment "
        "FROM system.columns "
        "WHERE database = {db:String} AND table = {tbl:String} "
        "ORDER BY position"
    )
    _, rows = _execute(sql, settings, parameters={"db": database, "tbl": table})

    if not rows:
        raise TableNotFoundError(
            message=(
                f"Table '{database}.{table}' not found or has no columns. "
                "Check the database and table names with listTables."
            ),
            code="TABLE_NOT_FOUND",
        )

    columns = [
        {"name": row[0], "type": row[1], "comment": row[2] or ""}
        for row in rows
    ]
    return {"database": database, "table": table, "columns": columns}


def sample_rows(
    database: str,
    table: str,
    limit: int = 5,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Return up to *limit* rows from *database*.*table*.

    *limit* is capped at 50 regardless of the caller's value.

    Returns the compact {columns, rows, row_count, truncated=False} shape.

    Raises:
        DatabaseNotAllowedError: if *database* is not in the allowlist.
    """
    if settings is None:
        settings = get_settings()

    _check_database_allowed(database, settings)

    # Cap at 50 regardless of caller input.
    effective_limit = max(1, min(limit, 50))

    # Database and table names are allowlist-checked above; use backtick quoting
    # to handle names with special characters.
    safe_db = database.replace("`", "``")
    safe_table = table.replace("`", "``")
    sql = f"SELECT * FROM `{safe_db}`.`{safe_table}` LIMIT {effective_limit}"

    columns, rows = _execute(sql, settings)

    return _compact_result(columns, rows, settings.max_response_rows, truncated_override=False)


def run_query(
    sql: str,
    limit: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Validate, optionally limit, and execute *sql*.

    SQL is passed through validate_and_sanitize() before execution.  An
    explicit *limit* overrides the injected default (capped at max_response_rows).

    Returns the compact {columns, rows, row_count, truncated} shape.

    Raises:
        QueryValidationError:    if SQL fails the security guardrails.
        ClickHouseQueryError:    if ClickHouse returns a query-level error.
        ClickHouseUnavailableError: if ClickHouse is unreachable.
    """
    if settings is None:
        settings = get_settings()

    try:
        clean_sql = validate_and_sanitize(sql, settings.default_limit)
    except HTTPException as exc:
        raise QueryValidationError(
            message=exc.detail.get("error", "Query validation failed"),
            code=exc.detail.get("code", "QUERY_VALIDATION_ERROR"),
        ) from exc

    # Apply caller-supplied limit override.
    # The substitution pattern preserves a trailing OFFSET clause (captured in
    # group 1) so that paging queries are not silently broken.
    if limit is not None:
        effective_limit = min(limit, settings.max_response_rows)
        clean_sql = _LIMIT_TAIL_RE.sub(
            lambda m: f"LIMIT {effective_limit}{m.group(1) or ''}",
            clean_sql,
        )

    columns, rows = _execute(clean_sql, settings)

    return _compact_result(columns, rows, settings.max_response_rows)


def explain_query(
    sql: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Wrap *sql* in EXPLAIN and return the query plan.

    The inner SQL is still validated by the guardrails.  truncated is always
    False because EXPLAIN output is tiny.

    Returns the compact {columns, rows, row_count, truncated=False} shape.

    Raises:
        QueryValidationError:    if the inner SQL fails the security guardrails.
        ClickHouseQueryError:    if ClickHouse returns a query-level error.
        ClickHouseUnavailableError: if ClickHouse is unreachable.
    """
    if settings is None:
        settings = get_settings()

    try:
        inner_sql = validate_and_sanitize(sql, settings.default_limit)
    except HTTPException as exc:
        raise QueryValidationError(
            message=exc.detail.get("error", "Query validation failed"),
            code=exc.detail.get("code", "QUERY_VALIDATION_ERROR"),
        ) from exc

    explain_sql = f"EXPLAIN {inner_sql}"
    columns, rows = _execute(explain_sql, settings)

    return _compact_result(columns, rows, settings.max_response_rows, truncated_override=False)
