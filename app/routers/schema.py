"""Schema-discovery routes: listDatabases, listTables, getTableSchema, sampleRows."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import require_api_key
from app.clickhouse_client import execute_query
from app.config import Settings, get_settings
from app.models import ColumnInfo, DatabaseInfo, SchemaResponse, TableInfo, QueryResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Schema Discovery"])


def _check_database_allowed(database: str, settings: Settings) -> None:
    """Raise 403 if *database* is not in the configured allowlist."""
    allowed = settings.allowed_databases_list()
    if allowed is not None and database not in allowed:
        raise HTTPException(
            status_code=403,
            detail={
                "error": (
                    f"Access to database '{database}' is not permitted. "
                    f"Allowed databases: {', '.join(allowed)}"
                ),
                "code": "DATABASE_NOT_ALLOWED",
            },
        )


@router.get(
    "/databases",
    response_model=list[DatabaseInfo],
    operation_id="listDatabases",
    summary="List available databases",
    description=(
        "Returns the list of ClickHouse databases this API is configured to expose. "
        "Call this first to discover which databases are available before listing tables. "
        "Results respect the ALLOWED_DATABASES server allowlist — databases outside that "
        "list are never returned."
    ),
    dependencies=[Depends(require_api_key)],
)
def list_databases(settings: Settings = Depends(get_settings)) -> list[DatabaseInfo]:
    """List all databases accessible through this API.

    Filters results against the ALLOWED_DATABASES allowlist so the LLM only
    sees databases it is permitted to query.
    """
    _, rows = execute_query("SELECT name FROM system.databases ORDER BY name", settings)

    allowed = settings.allowed_databases_list()
    results: list[DatabaseInfo] = []
    for (name,) in rows:
        if allowed is None or name in allowed:
            results.append(DatabaseInfo(name=name))
    return results


@router.get(
    "/tables",
    response_model=list[TableInfo],
    operation_id="listTables",
    summary="List tables in a database",
    description=(
        "Returns all tables (and their storage engines) inside the specified database. "
        "Use the 'database' parameter to select which database to inspect. "
        "Call listDatabases first if you are unsure which databases exist."
    ),
    dependencies=[Depends(require_api_key)],
)
def list_tables(
    database: str = Query(..., description="The database to list tables from"),
    settings: Settings = Depends(get_settings),
) -> list[TableInfo]:
    """List all tables in *database*."""
    _check_database_allowed(database, settings)

    sql = (
        "SELECT database, name, engine "
        "FROM system.tables "
        "WHERE database = {db:String} "
        "ORDER BY name"
    )
    # Route through execute_query so that readonly_settings() is applied
    # consistently (readonly + max_execution_time + max_result_rows +
    # result_overflow_mode + max_rows_to_read).
    _, rows = execute_query(sql, settings, parameters={"db": database})

    return [
        TableInfo(database=row[0], name=row[1], engine=row[2])
        for row in rows
    ]


@router.get(
    "/tables/schema",
    response_model=SchemaResponse,
    operation_id="getTableSchema",
    summary="Get column schema for a table",
    description=(
        "Returns the full column schema (name, data type, comment) for the specified table. "
        "ALWAYS call this endpoint before writing a SELECT query against an unfamiliar table — "
        "it tells you the exact column names, their ClickHouse types (e.g. UInt64, Nullable(String), "
        "DateTime64(3)), and any descriptive comments. Knowing the schema prevents type mismatch "
        "errors and helps you write correct WHERE clauses and aggregations."
    ),
    dependencies=[Depends(require_api_key)],
)
def get_table_schema(
    database: str = Query(..., description="The database containing the table"),
    table: str = Query(..., description="The table to describe"),
    settings: Settings = Depends(get_settings),
) -> SchemaResponse:
    """Return column names, types, and comments for *database*.*table*."""
    _check_database_allowed(database, settings)

    sql = (
        "SELECT name, type, comment "
        "FROM system.columns "
        "WHERE database = {db:String} AND table = {tbl:String} "
        "ORDER BY position"
    )

    # Route through execute_query so that readonly_settings() is applied
    # consistently across all query paths.
    _, rows = execute_query(sql, settings, parameters={"db": database, "tbl": table})

    if not rows:
        raise HTTPException(
            status_code=404,
            detail={
                "error": (
                    f"Table '{database}.{table}' not found or has no columns. "
                    "Check the database and table names with listTables."
                ),
                "code": "TABLE_NOT_FOUND",
            },
        )

    columns = [
        ColumnInfo(name=row[0], type=row[1], comment=row[2] or "")
        for row in rows
    ]
    return SchemaResponse(database=database, table=table, columns=columns)


@router.get(
    "/tables/sample",
    response_model=QueryResponse,
    operation_id="sampleRows",
    summary="Return a sample of rows from a table",
    description=(
        "Returns a small number of raw rows from the specified table so you can inspect "
        "actual data values, formats, and nullability before writing analytical queries. "
        "Default sample size is 5 rows; maximum is 50. "
        "Use this after getTableSchema to understand the data shape."
    ),
    dependencies=[Depends(require_api_key)],
)
def sample_rows(
    database: str = Query(..., description="The database containing the table"),
    table: str = Query(..., description="The table to sample"),
    limit: int = Query(5, ge=1, le=50, description="Number of sample rows (1–50, default 5)"),
    settings: Settings = Depends(get_settings),
) -> QueryResponse:
    """Return up to *limit* rows from *database*.*table*."""
    _check_database_allowed(database, settings)

    # We construct the table reference carefully — database and table names come
    # from validated allowlist-checked parameters, not raw user SQL input.
    # Use backtick quoting for identifiers to handle names with special characters.
    safe_db = database.replace("`", "``")
    safe_table = table.replace("`", "``")
    sql = f"SELECT * FROM `{safe_db}`.`{safe_table}` LIMIT {limit}"

    columns, rows = execute_query(sql, settings)

    return QueryResponse(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=False,  # sample always returns exactly what was asked
    )
