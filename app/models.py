"""Pydantic request/response schemas for the ClickHouse REST API."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class DatabaseInfo(BaseModel):
    """A single database entry returned by listDatabases."""

    name: str = Field(..., description="Database name")


class TableInfo(BaseModel):
    """A single table entry returned by listTables."""

    database: str = Field(..., description="Owning database name")
    name: str = Field(..., description="Table name")
    engine: str = Field(..., description="Backend label for catalog tables; physical engine for scratch tables")


class ColumnInfo(BaseModel):
    """A single column entry returned by getTableSchema."""

    name: str = Field(..., description="Column name")
    type: str = Field(..., description="ClickHouse data type")
    comment: str = Field("", description="Column comment, if any")
    context: dict[str, list[dict[str, Any]]] | None = None


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


class SourceFreshness(BaseModel):
    table: str
    kind: Literal["live", "ingested", "unknown"]
    data_as_of: str | None = None


class ResultFreshness(BaseModel):
    mode: Literal["live", "ingested", "mixed", "unknown"]
    sources: list[SourceFreshness]


class QueryResponse(BaseModel):
    """Compact query result — column-oriented to minimise token usage."""

    retrieved_at: str | None = None
    data_as_of: str | None = None
    freshness: ResultFreshness | None = None

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
    trino: str | None = Field(None, description="Trino reachability when enabled")


# ---------------------------------------------------------------------------
# Answer snapshots (app/routers/export.py — registered only when EXPORT_ENABLED)
# ---------------------------------------------------------------------------


class SnapshotCreateRequest(BaseModel):
    """Request body for createAnswerSnapshot."""

    sql: str = Field(
        ...,
        description=(
            "Read-only SELECT/WITH statement to snapshot. The FULL result set is "
            "materialized — no default LIMIT is injected on this path."
        ),
    )

    refresh_of: str | None = Field(
        None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        description="Previous owned snapshot ID to replace. Repeating the same refresh reuses its replacement; omit for normal creation.",
    )


class SnapshotCreateResponse(BaseModel):
    """Response for createAnswerSnapshot (HTTP 201)."""

    snapshot_id: str = Field(..., description="Server-minted snapshot UUID")
    columns: list[str] = Field(..., description="Ordered column names (compact shape)")
    row_count: int = Field(..., description="Exact total rows in the snapshot")
    created_at: str = Field(..., description="Mint time, ISO 8601 UTC (Z)")
    expires_at: str = Field(..., description="Expiry time, ISO 8601 UTC (Z)")


class SnapshotPageResponse(BaseModel):
    """Response for pageAnswerSnapshot."""

    columns: list[str] = Field(..., description="Ordered column names")
    rows: list[list[Any]] = Field(..., description="Row data as a list of lists")
    limit: int = Field(..., description="Page size applied")
    offset: int = Field(..., description="Row offset applied")
    has_more: bool = Field(
        ..., description="EXACT: offset + len(rows) < row_count"
    )
    row_count: int = Field(..., description="Exact total rows in the snapshot")
    snapshot_id: str = Field(..., description="Snapshot this page came from")
    expires_at: str = Field(..., description="Snapshot expiry, ISO 8601 UTC (Z)")


class SnapshotDownloadFile(BaseModel):
    """One CSV part of a snapshot download."""

    part: int = Field(..., description="1-indexed part number (in order)")
    url: str = Field(
        ...,
        description=(
            "S3 mode: short-lived presigned GET URL. Couchbase mode: relative app "
            "path query/snapshots/{id}/download?part=N (app-streamed bytes)."
        ),
    )
    rows: int = Field(..., description="Data rows in this part")
    filename: str = Field(..., description="Download filename the URL forces")


class SnapshotDownloadUrlsResponse(BaseModel):
    """Response for getAnswerSnapshotDownloadUrls."""

    files: list[SnapshotDownloadFile] = Field(
        ..., description="Ordered CSV parts; empty for a zero-row snapshot"
    )
    expires_in: Optional[int] = Field(
        None,
        description=(
            "URL lifetime in seconds (S3 presign window). null in Couchbase mode — "
            "per-request authz replaces the presign window."
        ),
    )
