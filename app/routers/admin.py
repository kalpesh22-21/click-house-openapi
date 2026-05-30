"""Admin CSV ingestion router.

Exposes two endpoints protected by the same Bearer token auth used by the
read-only LLM API.  All business logic lives in app.admin_ingest
(transport-agnostic).
"""

from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile

from app.admin_ingest import analyze, ingest
from app.auth import require_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin Ingest"])

# Maximum allowed CSV upload size (200 MB).
MAX_CSV_BYTES = 200 * 1024 * 1024


# ---------------------------------------------------------------------------
# POST /admin/analyze
# ---------------------------------------------------------------------------


@router.post(
    "/analyze",
    summary="Analyze a CSV file against a ClickHouse table",
    description=(
        "Connect with user-supplied credentials, inspect the target database and table, "
        "parse the uploaded CSV, infer ClickHouse types per column, and return a schema "
        "comparison report."
    ),
    dependencies=[Depends(require_api_key)],
)
async def analyze_endpoint(
    host: Annotated[str, Form(description="ClickHouse host")],
    port: Annotated[int, Form(description="ClickHouse port")],
    user: Annotated[str, Form(description="ClickHouse username")],
    password: Annotated[str, Form(description="ClickHouse password")],
    secure: Annotated[bool, Form(description="Use TLS")],
    database: Annotated[str, Form(description="Target database name")],
    table: Annotated[str, Form(description="Target table name")],
    file: UploadFile,
) -> dict:
    """Analyze *file* (CSV) against *database*.*table* and return a schema report."""
    file_content = await file.read()
    if len(file_content) > MAX_CSV_BYTES:
        raise HTTPException(
            status_code=413,
            detail="CSV file exceeds maximum allowed size (200 MB).",
        )
    try:
        result = analyze(
            host=host,
            port=port,
            user=user,
            password=password,
            secure=secure,
            database=database,
            table=table,
            file_content=file_content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in /admin/analyze")
        raise HTTPException(
            status_code=500, detail=f"Internal error: {exc}"
        ) from exc

    return result


# ---------------------------------------------------------------------------
# POST /admin/ingest
# ---------------------------------------------------------------------------


@router.post(
    "/ingest",
    summary="Ingest a CSV file into a ClickHouse table",
    description=(
        "Upload a CSV file and insert its rows into the target ClickHouse table. "
        "mode='create' creates a new table (fails if exists), 'wipe' drops and recreates, "
        "'append' inserts only rows newer than the current watermark."
    ),
    dependencies=[Depends(require_api_key)],
)
async def ingest_endpoint(
    host: Annotated[str, Form(description="ClickHouse host")],
    port: Annotated[int, Form(description="ClickHouse port")],
    user: Annotated[str, Form(description="ClickHouse username")],
    password: Annotated[str, Form(description="ClickHouse password")],
    secure: Annotated[bool, Form(description="Use TLS")],
    database: Annotated[str, Form(description="Target database name")],
    table: Annotated[str, Form(description="Target table name")],
    file: UploadFile,
    mode: Annotated[str, Form(description="One of: create | wipe | append")],
    columns: Annotated[
        Optional[str],
        Form(
            description=(
                "JSON array of [{\"name\": ..., \"type\": ...}] — required for create/wipe, "
                "optional for append (existing schema is used)."
            )
        ),
    ] = None,
    order_by: Annotated[
        Optional[str],
        Form(description="JSON array of column names for ORDER BY (create/wipe only)"),
    ] = None,
    timestamp_column: Annotated[
        Optional[str],
        Form(description="Column name used as append watermark (required for append mode)"),
    ] = None,
    batch_size: Annotated[
        int,
        Form(description="Number of rows per insert batch (default 50000, minimum 1)"),
    ] = 50_000,
) -> dict:
    """Ingest *file* (CSV) into *database*.*table* using the requested *mode*."""
    file_content = await file.read()
    if len(file_content) > MAX_CSV_BYTES:
        raise HTTPException(
            status_code=413,
            detail="CSV file exceeds maximum allowed size (200 MB).",
        )
    try:
        result = ingest(
            host=host,
            port=port,
            user=user,
            password=password,
            secure=secure,
            database=database,
            table=table,
            file_content=file_content,
            mode=mode,
            columns_json=columns,
            order_by_json=order_by,
            timestamp_column=timestamp_column,
            batch_size=batch_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in /admin/ingest")
        raise HTTPException(
            status_code=500, detail=f"Internal error: {exc}"
        ) from exc

    return result
