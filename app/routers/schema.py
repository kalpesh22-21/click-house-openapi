"""Schema-discovery routes: listDatabases, listTables, getTableSchema, sampleRows.

These are thin HTTP adapters.  All business logic lives in app.service.
Domain errors from service.py are mapped to HTTP status codes here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import require_principal
from app.config import Settings, get_settings
from app.errors import DatabaseNotAllowedError, TableNotFoundError
from app.models import ColumnInfo, DatabaseInfo, SchemaResponse, TableInfo, QueryResponse
from app.service import (
    get_table_schema as svc_get_table_schema,
    list_databases as svc_list_databases,
    list_tables as svc_list_tables,
    sample_rows as svc_sample_rows,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Schema Discovery"])


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
    dependencies=[Depends(require_principal)],
)
def list_databases(settings: Settings = Depends(get_settings)) -> list[DatabaseInfo]:
    """List all databases accessible through this API."""
    results = svc_list_databases(settings)
    return [DatabaseInfo(name=r["name"]) for r in results]


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
    dependencies=[Depends(require_principal)],
)
def list_tables(
    database: str = Query(..., description="The database to list tables from"),
    settings: Settings = Depends(get_settings),
) -> list[TableInfo]:
    """List all tables in *database*."""
    try:
        results = svc_list_tables(database, settings)
    except DatabaseNotAllowedError as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": exc.message, "code": exc.code},
        ) from exc
    return [
        TableInfo(database=r["database"], name=r["name"], engine=r["engine"])
        for r in results
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
    dependencies=[Depends(require_principal)],
)
def get_table_schema(
    database: str = Query(..., description="The database containing the table"),
    table: str = Query(..., description="The table to describe"),
    settings: Settings = Depends(get_settings),
) -> SchemaResponse:
    """Return column names, types, and comments for *database*.*table*."""
    try:
        result = svc_get_table_schema(database, table, settings)
    except DatabaseNotAllowedError as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": exc.message, "code": exc.code},
        ) from exc
    except TableNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": exc.message, "code": exc.code},
        ) from exc

    return SchemaResponse(
        database=result["database"],
        table=result["table"],
        columns=[
            ColumnInfo(name=c["name"], type=c["type"], comment=c["comment"])
            for c in result["columns"]
        ],
    )


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
    dependencies=[Depends(require_principal)],
)
def sample_rows(
    database: str = Query(..., description="The database containing the table"),
    table: str = Query(..., description="The table to sample"),
    limit: int = Query(5, ge=1, le=50, description="Number of sample rows (1–50, default 5)"),
    settings: Settings = Depends(get_settings),
) -> QueryResponse:
    """Return up to *limit* rows from *database*.*table*."""
    try:
        result = svc_sample_rows(database, table, limit, settings)
    except DatabaseNotAllowedError as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": exc.message, "code": exc.code},
        ) from exc

    return QueryResponse(
        columns=result["columns"],
        rows=result["rows"],
        row_count=result["row_count"],
        truncated=result["truncated"],
    )
