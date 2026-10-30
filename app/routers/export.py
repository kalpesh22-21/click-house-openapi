"""Answer-snapshot export routes: createAnswerSnapshot, pageAnswerSnapshot,
getAnswerSnapshotDownloadUrls, downloadAnswerSnapshotPart.

Registered in app.main ONLY when EXPORT_ENABLED=true.  These are thin HTTP
adapters over app.export_service (business logic lives there); like the other
routers they are sync ``def`` handlers — FastAPI runs them in the threadpool,
which is what the synchronous streaming/boto3/couchbase code requires.  Auth is
the existing per-request JWT (require_principal); requests require the existing scoped session token, while
snapshot ownership includes verified caller, session and access scope.

Every domain error is mapped here to the house error envelope
``{"detail": {"error": ..., "code": ...}}`` via HTTPException, exactly like
app/routers/query.py.
"""

from __future__ import annotations

import logging
from functools import lru_cache, partial
from threading import BoundedSemaphore

import anyio

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.auth import require_principal
from app.config import Settings, get_settings
from app.snapshot_idempotency import SnapshotIdempotencyError
from app.errors import (
    CartesianJoinError,
    ClickHouseQueryError,
    ClickHouseUnavailableError,
    ColumnScopeError,
    DatabaseNotAllowedError,
    ExportTooLargeError,
    ParseFailedError,
    QueryValidationError,
    SnapshotExpiredError,
    SnapshotNotFoundError,
    SnapshotScopeChangedError,
    TableNotAllowedError,
    TableNotFoundError,
)
from app.export_service import (
    create_snapshot as svc_create_snapshot,
    rebuild_snapshot as svc_rebuild_snapshot,
    download_part as svc_download_part,
    download_urls as svc_download_urls,
    page_snapshot as svc_page_snapshot,
)
from app.models import (
    SnapshotCreateRequest,
    SnapshotCreateResponse,
    SnapshotDownloadUrlsResponse,
    SnapshotPageResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/snapshots", tags=["Answer Snapshots"])

# Domain error type → HTTP status for the export routes.  The detail envelope
# keeps {"error", "code"} so callers can branch on machine codes.
_EXPORT_ERROR_STATUS: tuple[tuple[type, int], ...] = (
    (ExportTooLargeError, 400),
    (QueryValidationError, 400),
    (CartesianJoinError, 400),
    (ParseFailedError, 400),
    (ClickHouseQueryError, 400),
    (SnapshotNotFoundError, 404),
    (SnapshotScopeChangedError, 409),
    (TableNotFoundError, 404),
    (SnapshotExpiredError, 410),
    (ColumnScopeError, 403),
    (DatabaseNotAllowedError, 403),
    (TableNotAllowedError, 403),
    # EmployeeAccessError subclasses ClickHouseUnavailableError and lands here.
    (ClickHouseUnavailableError, 502),
)


def _map_export_error(exc: Exception) -> HTTPException:
    """Map a domain error from the export service to the house error envelope."""
    if isinstance(exc, SnapshotIdempotencyError):
        return HTTPException(409, detail={"error": exc.message, "code": exc.code},
                             headers={"Retry-After": "2"} if exc.code == "SNAPSHOT_IN_PROGRESS" else None)
    for error_type, status_code in _EXPORT_ERROR_STATUS:
        if isinstance(exc, error_type):
            return HTTPException(
                status_code=status_code,
                detail={"error": exc.message, "code": exc.code},
            )
    # MISSING_TENANT_CLAIM etc. are already HTTPException(403) with the right
    # envelope — let them pass through untouched.
    if isinstance(exc, HTTPException):
        return exc
    # Unexpected — re-raise so the global handler picks it up.
    raise exc  # type: ignore[misc]


@lru_cache(maxsize=1)
def _creation_slots(limit: int) -> BoundedSemaphore:
    # Admission happens on the event loop, before occupying a shared worker.
    # A worker's configured limit is fixed for its lifetime.
    return BoundedSemaphore(limit)


@router.post(
    "",
    response_model=SnapshotCreateResponse,
    status_code=201,
    operation_id="createAnswerSnapshot",
    summary="Materialize a full query result as an answer snapshot",
    description=(
        "Identical SQL in the same verified caller/session and access scope automatically "
        "reuses its snapshot until expiry. Pending creation returns 409 SNAPSHOT_IN_PROGRESS. "
        "Replays return the same 201 response; no idempotency header is needed. "
        "For explicit refresh, provide refresh_of with the previous owned snapshot ID. "
        "Runs the SQL through the same guardrails as runQuery and materializes the FULL "
        "result set (no default LIMIT is injected on this path) to object storage, returning "
        "the snapshot handle, exact row count, and expiry. Page it with "
        "GET /query/snapshots/{snapshot_id}/page and download CSV parts via "
        "GET /query/snapshots/{snapshot_id}/download-urls. If the result exceeds the "
        "export budget the request fails with 400 EXPORT_TOO_LARGE — narrow the query "
        "with a more selective WHERE clause or a smaller LIMIT."
    ),
    dependencies=[Depends(require_principal)],
)
async def create_snapshot(
    body: SnapshotCreateRequest,
    settings: Settings = Depends(get_settings),
) -> SnapshotCreateResponse:
    """Mint a snapshot from *body.sql* (201 on success)."""
    slots = _creation_slots(settings.export_max_concurrent_creates)
    if not slots.acquire(blocking=False):
        raise HTTPException(
            429, detail={"error": "Snapshot creation is busy. Retry shortly.", "code": "EXPORT_BUSY"},
            headers={"Retry-After": "1"},
        )
    try:
        options = {"refresh_of": body.refresh_of} if body.refresh_of is not None else {}
        if body.column_formats:
            options["column_formats"] = body.column_formats
        result = await anyio.to_thread.run_sync(partial(svc_create_snapshot, body.sql, settings, **options))
    except Exception as exc:
        raise _map_export_error(exc) from exc
    finally:
        slots.release()

    return SnapshotCreateResponse(**result)


@router.post(
    "/{snapshot_id}/rebuild", response_model=SnapshotCreateResponse, status_code=201,
    dependencies=[Depends(require_principal)],
)
async def rebuild_snapshot(snapshot_id: str, settings: Settings = Depends(get_settings)):
    """Revalidate and recreate an owned snapshot using its private stored SQL."""
    slots = _creation_slots(settings.export_max_concurrent_creates)
    if not slots.acquire(blocking=False):
        raise HTTPException(429, detail={"error": "Snapshot creation is busy.", "code": "EXPORT_BUSY"},
                            headers={"Retry-After": "1"})
    try:
        return await anyio.to_thread.run_sync(partial(svc_rebuild_snapshot, snapshot_id, settings))
    except Exception as exc:
        raise _map_export_error(exc) from exc
    finally:
        slots.release()


@router.get(
    "/{snapshot_id}/page",
    response_model=SnapshotPageResponse,
    operation_id="pageAnswerSnapshot",
    summary="Page a frozen answer snapshot",
    description=(
        "Returns rows [offset, offset+limit) of the snapshot minted by createAnswerSnapshot. "
        "has_more is exact: offset + len(rows) < row_count. An offset at or past the end "
        "returns 200 with rows=[] and has_more=false. 404 SNAPSHOT_NOT_FOUND covers unknown "
        "AND foreign-tenant ids (indistinguishable by design); 410 SNAPSHOT_EXPIRED once the "
        "snapshot TTL has elapsed."
    ),
    dependencies=[Depends(require_principal)],
)
def page_snapshot(
    snapshot_id: str,
    offset: int = Query(0, ge=0, description="Row offset (0-based)"),
    limit: int = Query(100, ge=1, le=1000, description="Page size (1–1000, default 100)"),
    settings: Settings = Depends(get_settings),
) -> SnapshotPageResponse:
    """Serve one page of the frozen snapshot rows."""
    try:
        result = svc_page_snapshot(snapshot_id, offset, limit, settings)
    except Exception as exc:
        raise _map_export_error(exc) from exc

    return SnapshotPageResponse(**result)


@router.get(
    "/{snapshot_id}/download-urls",
    response_model=SnapshotDownloadUrlsResponse,
    operation_id="getAnswerSnapshotDownloadUrls",
    summary="Get the download location for a snapshot's ordered CSV parts",
    description=(
        "S3 store mode: returns the ordered list of short-lived presigned part URLs "
        "(each signed with an attachment filename), valid for expires_in seconds. "
        "Couchbase store mode: each url is a RELATIVE app path "
        "query/snapshots/{id}/download?part=N (stream via GET on it) and expires_in "
        "is null — per-request authz replaces the presign window. One part → single "
        "download; multiple parts → download them in part order. Same 404/410 "
        "semantics as the page route."
    ),
    dependencies=[Depends(require_principal)],
)
def snapshot_download_urls(
    snapshot_id: str,
    settings: Settings = Depends(get_settings),
) -> SnapshotDownloadUrlsResponse:
    """Hand out the snapshot's CSV part URLs for the active store mode."""
    try:
        result = svc_download_urls(snapshot_id, settings)
    except Exception as exc:
        raise _map_export_error(exc) from exc

    return SnapshotDownloadUrlsResponse(**result)


@router.get(
    "/{snapshot_id}/download",
    operation_id="downloadAnswerSnapshotPart",
    summary="Stream one CSV part of a snapshot",
    description=(
        "Streams the stored bytes of CSV part N (1-indexed) as text/csv with a "
        "Content-Disposition attachment filename. This is the download transport "
        "when EXPORT_STORE=couchbase (download-urls urls point here). An "
        "out-of-range part returns 404 SNAPSHOT_NOT_FOUND; 404/410 semantics are "
        "otherwise identical to the page route."
    ),
    dependencies=[Depends(require_principal)],
)
def snapshot_download_part(
    snapshot_id: str,
    part: int = Query(..., description="1-indexed CSV part number"),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Stream CSV part *part* of the snapshot (text/csv)."""
    try:
        byte_iter, filename = svc_download_part(snapshot_id, part, settings)
    except Exception as exc:
        raise _map_export_error(exc) from exc

    return StreamingResponse(
        byte_iter,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
