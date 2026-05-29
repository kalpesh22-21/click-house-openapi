"""Pydantic request/response schemas for the ClickHouse REST API."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class DatabaseInfo(BaseModel):
    """A single database entry returned by listDatabases."""

    name: str = Field(..., description="Database name")


class TableInfo(BaseModel):
    """A single table entry returned by listTables."""

    database: str = Field(..., description="Owning database name")
    name: str = Field(..., description="Table name")
    engine: str = Field(..., description="ClickHouse table engine (e.g. MergeTree)")


class ColumnInfo(BaseModel):
    """A single column entry returned by getTableSchema."""

    name: str = Field(..., description="Column name")
    type: str = Field(..., description="ClickHouse data type")
    comment: str = Field("", description="Column comment, if any")


class SchemaResponse(BaseModel):
    """Response for getTableSchema."""

    database: str
    table: str
    columns: list[ColumnInfo]


class QueryRequest(BaseModel):
    """Request body for runQuery."""

    sql: str = Field(..., description="Read-only SQL statement (SELECT / WITH / SHOW / DESCRIBE)")
    limit: Optional[int] = Field(
        None,
        ge=1,
        le=10_000,
        description=(
            "Optional LIMIT override. Capped at MAX_RESPONSE_ROWS server-side regardless."
        ),
    )


class ExplainRequest(BaseModel):
    """Request body for explainQuery."""

    sql: str = Field(..., description="SQL statement to pass through EXPLAIN")


class QueryResponse(BaseModel):
    """Compact query result — column-oriented to minimise token usage."""

    columns: list[str] = Field(..., description="Ordered list of column names")
    rows: list[list[Any]] = Field(..., description="Row data as a list of lists")
    row_count: int = Field(..., description="Number of rows in this response")
    truncated: bool = Field(
        ...,
        description=(
            "True when the result was capped at MAX_RESPONSE_ROWS. "
            "Narrow your query with a more specific WHERE clause or a smaller LIMIT."
        ),
    )


class ErrorDetail(BaseModel):
    """Standard error envelope returned on 4xx / 5xx."""

    error: str = Field(..., description="Human-readable error message")
    code: str = Field(..., description="Machine-readable error code")


class HealthResponse(BaseModel):
    """Liveness / readiness probe response."""

    status: str = Field("ok", description="Always 'ok' when the service is healthy")
    clickhouse: str = Field(..., description="'ok' if ClickHouse is reachable, else 'error'")
