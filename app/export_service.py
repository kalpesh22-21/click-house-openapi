"""Answer-snapshot export service — mint, page, and hand out frozen query results.

A snapshot is the FULL result set of one validated query, materialized once to
the pluggable snapshot store (``EXPORT_STORE``: ``couchbase`` — the default — or
``s3``) and then served immutably:

- ``create_snapshot(sql)`` runs the caller's SQL through the EXACT SAME
  guardrail chain as ``service.run_query`` (validate_and_sanitize with
  ``inject_limit=False`` so the export is never truncated by the injected
  default LIMIT, then ``_enforce_query_guardrails``, then
  ``ensure_employee_access_fresh`` — same order, same argument flags), streams
  the rows under the EXPORT_* caps (result_overflow_mode='throw'), and persists
  three artifact families per snapshot:

      rows   — the paging source of truth: one gzip JSON Lines object at
               ``{EXPORT_S3_PREFIX}/{tenant_hash}/{snapshot_id}/rows.jsonl.gz``
               in S3 mode; 0-indexed JSON-array row-chunk docs
               ``snap::{tenant_hash}::{snapshot_id}::rows.NNNN`` (written DURING
               streaming, EXPORT_CB_CHUNK_ROWS rows/chunk) in Couchbase mode.
      parts  — 1-indexed CSV download part(s), identical split math in both
               modes (EXPORT_MAX_ROWS rows OR EXPORT_PART_MAX_BYTES per part,
               whichever trips first; each part carries a header row and is a
               standalone CSV): staged-file objects ``data.part-NNNN.csv`` in
               S3 mode, binary docs ``…::csv.NNNN`` in Couchbase mode.
      meta   — {snapshot_id, tenant_hash, columns: [{name, type}], row_count,
               created_at, expires_at, parts, sql_sha256} (+ ``chunk_rows`` in
               Couchbase mode): object ``meta.json`` in S3 mode, JSON doc
               ``…::meta`` in Couchbase mode.  Written LAST (commit point).
               NEVER exposed raw — only the snapshot_id may be logged; SQL text
               is retained privately for scope-change rebuilds; never returned or logged.

- ``page_snapshot(id, offset, limit)`` / ``download_urls(id)`` read the meta
  artifact, enforce tenant-hash equality (404 SNAPSHOT_NOT_FOUND on ANY
  owner mismatch — a wrong-tenant id is indistinguishable from an unknown id),
  check current grants (409 on a scope change), and
  app-side TTL (410 SNAPSHOT_EXPIRED), then serve the frozen rows / the
  download transport for the active store (presigned URLs in S3 mode; relative
  app paths + the GET .../download stream in Couchbase mode).

Budget enforcement is LOCAL and authoritative: EXPORT_TOTAL_MAX_ROWS /
EXPORT_TOTAL_MAX_BYTES are checked per row while streaming; on excess the
service aborts, deletes any partially uploaded objects/docs for the snapshot,
and raises ExportTooLargeError (HTTP 400, EXPORT_TOO_LARGE).  ClickHouse error
text is never parsed to detect overflow (the CH-side caps sit one row above the
row budget precisely so the local check always wins — see
clickhouse_client.export_settings).
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import hmac
import io
import json
import logging
import re
import tempfile
from threading import Lock
import uuid
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from app.clickhouse_client import (
    execute_query_rows_stream,
    export_settings,
    tenant_settings,
)
from app.config import Settings, get_settings
from app.employee_access import ensure_employee_access_fresh
from app.errors import (
    ClickHouseQueryError,
    ClickHouseUnavailableError,
    ExportTooLargeError,
    QueryValidationError,
    SnapshotExpiredError,
    SnapshotNotFoundError,
)
from app.export_client import S3SnapshotStore, SnapshotObjectMissingError
from app.export_store_couchbase import CouchbaseSnapshotStore
from app.security import validate_and_sanitize
from app.service import _enforce_query_guardrails, _compact_result

logger = logging.getLogger(__name__)

# Snapshot ids land inside store keys/doc-ids — accept only uuid-like ids so a
# caller-supplied id can never inject key-structure characters.  An id that
# fails the shape check is simply NOT FOUND (never a distinct 400 — that would
# leak which namespaces are probeable).
_SNAPSHOT_ID_RE = re.compile(r"^[0-9a-fA-F-]{32,64}$")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _iso_z(dt: datetime) -> str:
    """Serialize *dt* as ISO 8601 UTC with a trailing 'Z' (house wire format)."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso_z(value: str) -> datetime:
    """Parse an ISO 8601 'Z' timestamp back into an aware UTC datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _tenant_hash(tenant: dict[str, Any]) -> str:
    """Full sha256 hexdigest of the canonical JSON of the tenant settings dict.

    The dict is exactly what clickhouse_client.tenant_settings() returns for the
    request (the RLS identity carried on the query); canonical JSON (sorted
    keys, compact separators) makes the digest independent of key order.  Page
    and download recompute it from the CURRENT request and demand equality.
    """
    canonical = json.dumps(tenant, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_identity(settings: Settings) -> dict[str, Any]:
    from app.principal import get_current_principal, get_current_session_id
    principal = get_current_principal()
    scope = principal.catalog_scope if principal else None
    identity = {**tenant_settings(settings),
                "snapshot_owner_v2": {
                    "subject": principal.subject if principal else None,
                    "session": get_current_session_id(),
                    "schemas": sorted(scope.schemas) if scope and scope.schemas is not None else None,
                    "columns": sorted(scope.columns) if scope and scope.columns is not None else None,
                }}
    if settings.trino_enabled:
        from app.trino_client import request_identity
        identity = {**identity, "trino_identity": request_identity(settings)}
    return identity


def _snapshot_owner_identity(settings: Settings) -> dict[str, Any]:
    """Stable ownership excludes grants, but retains every query identity claim."""
    identity = _snapshot_identity(settings)
    owner = identity.pop("snapshot_owner_v2")
    identity["snapshot_owner_v3"] = {"subject": owner["subject"], "session": owner["session"]}
    return identity


def rebuild_snapshot(snapshot_id: str, settings: Settings, *, store: Any = None) -> dict[str, Any]:
    store = _resolve_store(settings, store)
    meta = _load_meta(store, settings, snapshot_id, allow_scope_change=True)
    sql = meta.get("source_sql")
    if not isinstance(sql, str) or not sql:
        raise SnapshotNotFoundError("Snapshot cannot be recreated.", "SNAPSHOT_NOT_FOUND")
    # create_snapshot re-runs all current authorization and SQL guardrails.
    return create_snapshot(sql, settings, store=store, refresh_of=snapshot_id)


def _snapshot_prefix(settings: Settings, tenant_hash: str, snapshot_id: str) -> str:
    """S3 key prefix for one snapshot's objects."""
    prefix = settings.export_s3_prefix.strip("/")
    return f"{prefix}/{tenant_hash}/{snapshot_id}" if prefix else f"{tenant_hash}/{snapshot_id}"


def _artifact_prefix(settings: Settings, tenant_hash: str, snapshot_id: str) -> str:
    """Store key/doc-id prefix owning every artifact of one snapshot.

    S3 mode: ``{EXPORT_S3_PREFIX}/{tenant_hash}/{snapshot_id}`` (joined with
    "/" per artifact).  Couchbase mode: ``snap::{tenant_hash}::{snapshot_id}::``.
    """
    if settings.export_store == "couchbase":
        return CouchbaseSnapshotStore.prefix(tenant_hash, snapshot_id)
    return _snapshot_prefix(settings, tenant_hash, snapshot_id)


def _meta_key(settings: Settings, tenant_hash: str, snapshot_id: str) -> str:
    """Key of the snapshot's meta artifact (the commit point)."""
    if settings.export_store == "couchbase":
        return CouchbaseSnapshotStore.meta_key(_artifact_prefix(settings, tenant_hash, snapshot_id))
    return f"{_snapshot_prefix(settings, tenant_hash, snapshot_id)}/meta.json"


def _clean_string_padding(value: Any) -> Any:
    """Strip trailing NUL padding from strings, including nested values.

    Identical presentation policy to the MCP result cleaner
    (mcp_server._clean_result_value): trailing NULs (FixedString padding) go,
    embedded NULs / whitespace / binary / map keys stay.
    """
    if isinstance(value, str):
        return value.rstrip("\x00")
    if isinstance(value, list):
        return [_clean_string_padding(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_clean_string_padding(item) for item in value)
    if isinstance(value, dict):
        return {key: _clean_string_padding(item) for key, item in value.items()}
    return value


def _wire_row(row: Iterable[Any]) -> list[Any]:
    """Encode one streamed row as its REST-wire JSON values.

    NUL-clean first, then jsonable_encoder — this matches what FastAPI/pydantic
    emits for /query rows today (datetime/date → ISO strings, Decimal → string,
    tuples → arrays), so a snapshot's rows are byte-identical in value to the
    rows the interactive path would have returned.
    """
    cleaned = _clean_string_padding(list(row))
    _compact_result([], [cleaned], 1)  # Same non-finite-value rejection as /query.
    return jsonable_encoder(cleaned, custom_encoder={Decimal: str})


def _csv_cell(value: Any) -> str:
    """Render one wire value as a canonical CSV cell (display formatting stays UI-side)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    # Arrays / maps / other composites: canonical compact JSON.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _csv_line(values: list[str]) -> str:
    """Serialize one CSV record exactly as written to the part file."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerow(values)
    return buf.getvalue()


class _JsonlGzRowsWriter:
    """S3-mode rows sink: stage rows as gzip JSON Lines on local disk.

    One line per row, a JSON array of wire values; the staged file is uploaded
    verbatim to ``rows.jsonl.gz`` after the stream completes (the paging source
    of truth).  Text-mode gzip, ``newline='\\n'`` — no platform translation.
    """

    def __init__(self, tmpdir: str) -> None:
        self.rows_path = f"{tmpdir}/rows.jsonl.gz"
        self._jf = None

    def write(self, wire_row: list[Any], line: str) -> None:
        if self._jf is None:
            self._jf = gzip.open(self.rows_path, "wt", encoding="utf-8", newline="\n")
        self._jf.write(line)

    def finish(self) -> None:
        if self._jf is None:
            # A zero-row snapshot never opened the JSONL stream lazily — still
            # stage an (empty) stream so paging reads always find the object.
            self._jf = gzip.open(self.rows_path, "wt", encoding="utf-8", newline="\n")
        self._jf.close()
        self._jf = None


class _ChunkedRowsDocsWriter:
    """Couchbase-mode rows sink: persist row-chunk JSON docs DURING streaming.

    Chunks are 0-indexed (``rows.0000``, ``rows.0001``, …) and hold up to
    EXPORT_CB_CHUNK_ROWS wire rows apiece, so no monolithic rows object is ever
    staged and paging fetches O(1) docs by ceil-math.  The chunk docs inherit
    the snapshot's per-doc expiry from the store.
    """

    def __init__(self, store: Any, prefix: str, chunk_rows: int) -> None:
        self._store = store
        self._prefix = prefix
        self._chunk_rows = chunk_rows
        self._buf: list[list[Any]] = []
        self._next_chunk = 0

    def write(self, wire_row: list[Any], line: str) -> None:
        # ``line`` is the S3 sink's pre-encoded JSONL text; the docs sink stores
        # the row itself and ignores line entirely.
        self._buf.append(wire_row)
        if len(self._buf) >= self._chunk_rows:
            self._flush()

    def _flush(self) -> None:
        key = CouchbaseSnapshotStore.rows_chunk_key(self._prefix, self._next_chunk)
        self._store.put_json(key, self._buf)
        self._next_chunk += 1
        self._buf = []

    def finish(self) -> None:
        if self._buf:
            self._flush()


class _Staging:
    """Local temp-file staging for one snapshot while rows stream in.

    Everything stays on local disk under a TemporaryDirectory until the stream
    COMPLETES; uploads happen only after that, so a budget abort or a mid-stream
    ClickHouse failure leaves zero orphan artifacts in the store (Couchbase-mode
    row chunks are the deliberate exception: they are streamed docs and are
    reaped by delete_prefix on abort / per-doc expiry otherwise).  Byte counters
    here are the authoritative budget metric: JSONL bytes are counted as
    written PRE-compression (a stable upper proxy for the stored size) and CSV
    bytes exactly.
    """

    def __init__(self, settings: Settings, columns: list[str], rows_writer_factory: Any) -> None:
        self._settings = settings
        self._columns = columns
        self._tmpdir = tempfile.TemporaryDirectory(prefix="chapi-export-")
        self.rows = rows_writer_factory(self._tmpdir.name)
        self.parts: list[dict[str, Any]] = []  # {path, name, rows, bytes} staged CSV parts
        self.total_rows = 0
        self.total_bytes = 0
        self._part_fh = None
        self._part_rows = 0
        self._part_bytes = 0
        self._finished = False

    def _start_part(self, part_no: int) -> None:
        name = f"data.part-{part_no:04d}.csv"
        path = f"{self._tmpdir.name}/{name}"
        # newline='' keeps the csv writer's own \r\n terminators untranslated.
        self._part_fh = open(path, "w", encoding="utf-8", newline="")
        header = _csv_line(self._columns)
        self._part_fh.write(header)
        self._part_bytes = len(header.encode("utf-8"))
        self._part_rows = 0
        self.parts.append({"path": path, "name": name, "rows": 0, "bytes": self._part_bytes})

    def _close_part(self) -> None:
        if self._part_fh is None:
            return
        self.parts[-1]["rows"] = self._part_rows
        self.parts[-1]["bytes"] = self._part_bytes
        self._part_fh.close()
        self._part_fh = None

    def write_row(self, wire_row: list[Any]) -> None:
        """Write one wire row to the rows sink and the current CSV part.

        Raises ExportTooLargeError BEFORE writing when the row would exceed a
        total budget — the snapshot is then aborted.
        """
        jsonl_line = json.dumps(wire_row, ensure_ascii=False, separators=(",", ":")) + "\n"
        jsonl_bytes = len(jsonl_line.encode("utf-8"))
        csv_line = _csv_line([_csv_cell(v) for v in wire_row])
        csv_bytes = len(csv_line.encode("utf-8"))

        if (
            self.total_rows + 1 > self._settings.export_total_max_rows
            or self.total_bytes + jsonl_bytes + csv_bytes > self._settings.export_total_max_bytes
        ):
            raise ExportTooLargeError(
                message=(
                    "This export exceeds the maximum snapshot size "
                    f"({self._settings.export_total_max_rows} rows / "
                    f"{self._settings.export_total_max_bytes // (1024 * 1024)} MB). "
                    "Narrow the result with a more selective WHERE clause or a "
                    "smaller LIMIT and export again."
                ),
                code="EXPORT_TOO_LARGE",
            )

        if self._part_fh is None or (
            self._part_rows >= self._settings.export_max_rows
            or self._part_bytes + csv_bytes > self._settings.export_part_max_bytes
        ):
            self._close_part()
            self._start_part(len(self.parts) + 1)

        self.rows.write(wire_row, jsonl_line)
        self._part_fh.write(csv_line)
        self._part_rows += 1
        self._part_bytes += csv_bytes
        self.total_rows += 1
        self.total_bytes += jsonl_bytes + csv_bytes

    def write_meta(self, meta: dict[str, Any]) -> str:
        """Stage meta.json locally and return its path (uploaded LAST by the caller)."""
        meta_path = f"{self._tmpdir.name}/meta.json"
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, separators=(",", ":"))
        return meta_path

    def finish(self) -> None:
        """Close open staged artifacts (part list is left truthful for meta)."""
        if self._finished:
            return
        self._close_part()
        self.rows.finish()
        self._finished = True

    def cleanup(self) -> None:
        try:
            self.finish()
        finally:
            self._tmpdir.cleanup()

    def __enter__(self) -> _Staging:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()


# ---------------------------------------------------------------------------
# Store resolution (injection seam: tests pass an in-memory fake, never boto3/CB)
# ---------------------------------------------------------------------------


_store_init_lock = Lock()


@lru_cache(maxsize=1)
def _cached_store() -> Any:
    """Process-wide store singleton for the ACTIVE backend (EXPORT_STORE).

    Both store modules import their SDK lazily, so building the selected store
    never loads the other backend's SDK.
    """
    settings = get_settings()
    if settings.export_store == "couchbase":
        return CouchbaseSnapshotStore(settings)
    return S3SnapshotStore(settings)


def _resolve_store(settings: Settings, store: Any) -> Any:
    if store is not None:
        return store
    # lru_cache permits duplicate constructor calls on concurrent misses.
    with _store_init_lock:
        return _cached_store()


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------


def create_snapshot(sql: str, settings: Settings | None = None, *, store: Any = None,
                    refresh_of: str | None = None) -> dict[str, Any]:
    """Reuse a snapshot for the same verified caller, session, scope and SQL."""
    from app.snapshot_idempotency import create_idempotent
    settings = settings or get_settings()
    return create_idempotent(sql, settings, _resolve_store(settings, store), refresh_of=refresh_of)


def _create_snapshot(
    sql: str,
    settings: Settings | None = None,
    *,
    store: Any = None,
    snapshot_id: str | None = None,
    expires_at: float | None = None,
) -> dict[str, Any]:
    """Mint an answer snapshot: run *sql* to completion and persist it.

    Guardrail chain is IDENTICAL to service.run_query (validation → shared
    guardrails → employee-access freshness), with inject_limit=False as the only
    deliberate difference: snapshots hold the FULL result set.

    Returns the create-response shape: {snapshot_id, columns, row_count,
    created_at, expires_at} with column NAMES only (matches runQuery's compact
    shape).
    """
    if settings is None:
        settings = get_settings()

    try:
        clean_sql = validate_and_sanitize(sql, settings.default_limit, inject_limit=False)
    except HTTPException as exc:
        raise QueryValidationError(
            message=exc.detail.get("error", "Query validation failed"),
            code=exc.detail.get("code", "QUERY_VALIDATION_ERROR"),
        ) from exc

    if settings.trino_enabled:
        from app.trino_client import normalize_logical_sql
        clean_sql = normalize_logical_sql(clean_sql)

    # Same shared guardrails run_query applies, same flags (REST callers are
    # unscoped — provenance triggers only for scratch references, exactly like
    # /query). Nothing about the export path weakens them.
    _enforce_query_guardrails(
        clean_sql, settings, caller="export_snapshot", include_provenance=False
    )

    # RLS freshness is frozen at MINT time: the row-policy set is refreshed here
    # (once) and never re-checked on page/download (those verify only the tenant
    # hash and the expiry clock).
    native_sql = clean_sql
    if settings.trino_enabled:
        from app.query_routing import direct_clickhouse_sql
        native_sql = direct_clickhouse_sql(clean_sql, settings)
    if native_sql is not None:
        ensure_employee_access_fresh(settings)

    tenant = _snapshot_owner_identity(settings)
    thash = _tenant_hash(tenant)
    store = _resolve_store(settings, store)
    store_mode = settings.export_store

    snapshot_id = snapshot_id or str(uuid.uuid4())
    created_at = datetime.now(timezone.utc)
    expires_at = (datetime.fromtimestamp(expires_at, timezone.utc) if expires_at is not None
                  else created_at + timedelta(seconds=settings.export_ttl_seconds))
    prefix = _artifact_prefix(settings, thash, snapshot_id)

    caps = export_settings(settings)
    commit_started = False
    try:
        if native_sql is None:
            from app.trino_client import execute_query_rows_stream as trino_stream
            stream = trino_stream(clean_sql, settings)
        else:
            stream = execute_query_rows_stream(native_sql, settings, caps=caps)
        with stream as rows:
            columns = rows.column_names
            column_types = rows.column_type_names
            if store_mode == "couchbase":
                def rows_writer_factory(tmpdir: str) -> Any:
                    return _ChunkedRowsDocsWriter(
                        store, prefix, settings.export_cb_chunk_rows
                    )
            else:
                rows_writer_factory = _JsonlGzRowsWriter
            with _Staging(settings, columns, rows_writer_factory) as staging:
                for row in rows:
                    staging.write_row(_wire_row(row))
                staging.finish()

                # Stream complete — now upload. meta goes LAST (commit point).
                parts_meta = []
                if store_mode == "couchbase":
                    for index, part in enumerate(staging.parts, start=1):
                        key = CouchbaseSnapshotStore.csv_part_key(prefix, index)
                        store.put_file(part["path"], key)
                        parts_meta.append(
                            {"key": key, "rows": part["rows"], "bytes": part["bytes"]}
                        )
                else:
                    jsonl_key = f"{prefix}/rows.jsonl.gz"
                    store.put_file(staging.rows.rows_path, jsonl_key)

                    for part in staging.parts:
                        key = f"{prefix}/{part['name']}"
                        store.put_file(part["path"], key)
                        parts_meta.append(
                            {"key": key, "rows": part["rows"], "bytes": part["bytes"]}
                        )

                meta = {
                    "snapshot_id": snapshot_id,
                    "tenant_hash": thash,
                    "access_hash": _tenant_hash(_snapshot_identity(settings)),
                    "source_sql": sql,
                    "columns": [
                        {"name": name, "type": ctype}
                        for name, ctype in zip(columns, column_types)
                    ],
                    "row_count": staging.total_rows,
                    "created_at": _iso_z(created_at),
                    "expires_at": _iso_z(expires_at),
                    "parts": parts_meta,
                    "sql_sha256": hashlib.sha256(clean_sql.encode("utf-8")).hexdigest(),
                }
                if store_mode == "couchbase":
                    meta["chunk_rows"] = settings.export_cb_chunk_rows
                    meta_key = CouchbaseSnapshotStore.meta_key(prefix)
                    commit_started = True
                    store.put_json(meta_key, meta)
                else:
                    meta_key = f"{prefix}/meta.json"
                    meta_path = staging.write_meta(meta)
                    commit_started = True
                    store.put_file(meta_path, meta_key)
    except HTTPException as exc:
        # ClickHouse query/connectivity failure from the stream boundary —
        # delete anything already uploaded, then translate to the service-layer
        # domain errors (same shapes run_query raises) for the router to map.
        # A timed-out metadata write may have committed. Never remove artifacts
        # after publication starts: an idempotent retry can recover that commit.
        if not commit_started:
            _best_effort_delete(store, prefix, snapshot_id)
        if exc.status_code == 502:
            raise ClickHouseUnavailableError(
                message=exc.detail.get("error", "ClickHouse unavailable"),
                code=exc.detail.get("code", "CLICKHOUSE_UNAVAILABLE"),
            ) from exc
        raise ClickHouseQueryError(
            message=exc.detail.get("error", "ClickHouse query error"),
            code=exc.detail.get("code", "CLICKHOUSE_QUERY_ERROR"),
        ) from exc
    except Exception:
        # Budget abort, storage failure, any mid-upload breakage: delete whatever
        # managed to upload so a half-written snapshot never becomes readable.
        # A timed-out metadata write may have committed. Never remove artifacts
        # after publication starts: an idempotent retry can recover that commit.
        if not commit_started:
            _best_effort_delete(store, prefix, snapshot_id)
        raise

    logger.info(
        "snapshot created id=%s rows=%d parts=%d",
        snapshot_id,
        staging.total_rows,
        len(parts_meta),
    )
    return {
        "snapshot_id": snapshot_id,
        "columns": columns,
        "row_count": staging.total_rows,
        "created_at": _iso_z(created_at),
        "expires_at": _iso_z(expires_at),
    }


def _best_effort_delete(store: Any, prefix: str, snapshot_id: str) -> None:
    try:
        store.delete_prefix(prefix)
    except Exception:  # noqa: BLE001 — cleanup must never mask the real error
        logger.warning(
            "snapshot cleanup failed id=%s (orphan objects expire via bucket lifecycle)",
            snapshot_id,
        )


def _load_meta(
    store: Any, settings: Settings, snapshot_id: str, *, allow_scope_change: bool = False,
) -> dict[str, Any]:
    """Read and authorize the meta artifact for *snapshot_id* from the CURRENT tenant.

    Raises SnapshotNotFoundError for: malformed id, missing doc/object, corrupt
    metadata, OR a tenant-hash mismatch — all four are deliberately the same wire
    error so existence is never probeable across tenants.
    """
    if not _SNAPSHOT_ID_RE.match(snapshot_id):
        raise SnapshotNotFoundError(
            message="Snapshot not found.", code="SNAPSHOT_NOT_FOUND"
        )
    thash = _tenant_hash(_snapshot_owner_identity(settings))
    meta_key = _meta_key(settings, thash, snapshot_id)
    try:
        raw = store.read_bytes(meta_key)
    except SnapshotObjectMissingError as exc:
        raise SnapshotNotFoundError(
            message="Snapshot not found.", code="SNAPSHOT_NOT_FOUND"
        ) from exc
    try:
        meta = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        logger.error("snapshot meta unreadable id=%s", snapshot_id)
        raise SnapshotNotFoundError(
            message="Snapshot not found.", code="SNAPSHOT_NOT_FOUND"
        ) from exc
    if not hmac.compare_digest(str(meta.get("tenant_hash", "")), thash):
        # Foreign-tenant id: identical wire error to a missing snapshot.
        raise SnapshotNotFoundError(
            message="Snapshot not found.", code="SNAPSHOT_NOT_FOUND"
        )
    if not allow_scope_change and not hmac.compare_digest(
        str(meta.get("access_hash", "")), _tenant_hash(_snapshot_identity(settings))
    ):
        from app.errors import SnapshotScopeChangedError
        raise SnapshotScopeChangedError(
            "Your access scope changed. This snapshot must be recreated.", "SNAPSHOT_SCOPE_CHANGED"
        )
    return meta


def _check_expiry(meta: dict[str, Any]) -> None:
    """410 SNAPSHOT_EXPIRED once the TTL clock runs out (app-side enforcement)."""
    try:
        expires_at = _parse_iso_z(str(meta["expires_at"]))
    except (KeyError, ValueError) as exc:
        logger.error("snapshot meta missing/invalid expires_at id=%s", meta.get("snapshot_id"))
        raise SnapshotNotFoundError(
            message="Snapshot not found.", code="SNAPSHOT_NOT_FOUND"
        ) from exc
    if datetime.now(timezone.utc) >= expires_at:
        raise SnapshotExpiredError(
            message="This snapshot has expired. Refresh the data to mint a new one.",
            code="SNAPSHOT_EXPIRED",
        )


def _iter_jsonl_rows(reader: Any) -> Iterator[list[Any]]:
    """Yield stored rows lazily from a rows.jsonl.gz object stream."""
    with gzip.GzipFile(fileobj=reader, mode="rb") as gz:
        wrapper = io.TextIOWrapper(gz, encoding="utf-8")
        for line in wrapper:
            line = line.rstrip("\n")
            if line:
                yield json.loads(line)


def _read_rows_window_cb(
    store: Any,
    settings: Settings,
    meta: dict[str, Any],
    snapshot_id: str,
    offset: int,
    limit: int,
) -> list[list[Any]]:
    """Couchbase-mode page read: fetch O(1) row-chunk docs and slice in memory.

    Chunk doc ids derive from row offsets (chunk = offset // chunk_rows), so a
    page never scans.  A missing chunk doc mirrors the S3 missing-payload rule:
    the meta outlived its rows (per-doc expiry raced the clock), so the snapshot
    is treated as EXPIRED — the UI offers Refresh, not a dead end.
    """
    thash = str(meta["tenant_hash"])
    row_count = int(meta.get("row_count", 0))
    chunk_rows = int(meta.get("chunk_rows") or settings.export_cb_chunk_rows)
    prefix = CouchbaseSnapshotStore.prefix(thash, snapshot_id)

    rows: list[list[Any]] = []
    idx = offset
    while len(rows) < limit and idx < row_count:
        chunk_no, within = divmod(idx, chunk_rows)
        key = CouchbaseSnapshotStore.rows_chunk_key(prefix, chunk_no)
        try:
            chunk = json.loads(store.read_bytes(key).decode("utf-8"))
        except SnapshotObjectMissingError as exc:
            raise SnapshotExpiredError(
                message="This snapshot has expired. Refresh the data to mint a new one.",
                code="SNAPSHOT_EXPIRED",
            ) from exc
        take = chunk[within : within + (limit - len(rows))]
        if not take:  # defensive: a short/empty trailing chunk must not loop
            break
        rows.extend(take)
        idx += len(take)
    return rows


def page_snapshot(
    snapshot_id: str,
    offset: int,
    limit: int,
    settings: Settings | None = None,
    *,
    store: Any = None,
) -> dict[str, Any]:
    """Return rows [offset, offset+limit) of a frozen snapshot.

    ``has_more`` is EXACT: ``offset + len(rows) < row_count``.  An offset at or
    past the end is a 200 with ``rows: []`` and ``has_more: false`` — never an
    error.  Rows are served exactly as stored at mint time, so a page never
    reflects data changes after mint.
    """
    if settings is None:
        settings = get_settings()
    store = _resolve_store(settings, store)

    meta = _load_meta(store, settings, snapshot_id)
    _check_expiry(meta)

    row_count = int(meta.get("row_count", 0))
    columns = [c.get("name", "") for c in meta.get("columns", [])]

    rows: list[list[Any]] = []
    if 0 <= offset < row_count and limit > 0:
        if settings.export_store == "couchbase":
            rows = _read_rows_window_cb(store, settings, meta, snapshot_id, offset, limit)
        else:
            rows_key = f"{_snapshot_prefix(settings, meta['tenant_hash'], snapshot_id)}/rows.jsonl.gz"
            try:
                reader = store.open_reader(rows_key)
            except SnapshotObjectMissingError as exc:
                # meta.json exists but the payload is gone (e.g. lifecycle reaped
                # it ahead of the clock) — from the caller's side the snapshot
                # cannot be served; treat as expired rather than not-found so the
                # UI offers the Refresh path instead of a dead end.
                raise SnapshotExpiredError(
                    message="This snapshot has expired. Refresh the data to mint a new one.",
                    code="SNAPSHOT_EXPIRED",
                ) from exc
            with reader:
                for index, stored_row in enumerate(_iter_jsonl_rows(reader)):
                    if index < offset:
                        continue
                    if len(rows) >= limit:
                        break
                    rows.append(stored_row)

    return {
        "columns": columns,
        "rows": rows,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < row_count,
        "row_count": row_count,
        "snapshot_id": snapshot_id,
        "expires_at": meta["expires_at"],
    }


def download_urls(
    snapshot_id: str,
    settings: Settings | None = None,
    *,
    store: Any = None,
) -> dict[str, Any]:
    """Return the download location for a snapshot's CSV parts, per store mode.

    S3 mode (byte-identical to CONTRACTS §1): ordered short-lived presigned
    URLs, each signed with ``ResponseContentDisposition``, ``expires_in``
    = EXPORT_PRESIGN_TTL_SECONDS.

    Couchbase mode (CONTRACTS §6.2): each file's ``url`` is a RELATIVE app path
    ``query/snapshots/{id}/download?part=N`` carrying per-request authz in place
    of the presign window, so ``expires_in`` is ``null``.

    Parts come back in order (part 1 first).  A zero-row snapshot has no parts
    and returns ``files: []``.
    """
    if settings is None:
        settings = get_settings()
    store = _resolve_store(settings, store)

    meta = _load_meta(store, settings, snapshot_id)
    _check_expiry(meta)

    if settings.export_store == "couchbase":
        files = [
            {
                "part": index,
                "url": f"query/snapshots/{snapshot_id}/download?part={index}",
                "rows": part["rows"],
                "filename": f"query-export-part-{index:04d}.csv",
            }
            for index, part in enumerate(meta.get("parts", []), start=1)
        ]
        return {"files": files, "expires_in": None}

    remaining = (_parse_iso_z(meta["expires_at"]) - datetime.now(timezone.utc)).total_seconds()
    expires_in = min(settings.export_presign_ttl_seconds, max(1, int(remaining)))
    files = []
    for index, part in enumerate(meta.get("parts", []), start=1):
        filename = f"query-export-part-{index:04d}.csv"
        url = store.presign_get(part["key"], filename=filename, expires_in=expires_in)
        files.append(
            {"part": index, "url": url, "rows": part["rows"], "filename": filename}
        )

    return {"files": files, "expires_in": expires_in}


def download_part(
    snapshot_id: str,
    part: int,
    settings: Settings | None = None,
    *,
    store: Any = None,
) -> tuple[Iterator[bytes], str]:
    """Return a (byte iterator, filename) streaming one stored CSV part.

    Backs GET /query/snapshots/{id}/download?part=N — the only download
    transport in Couchbase mode (CONTRACTS §6.2); also available in S3 mode.
    404/410/tenant semantics are identical to page_snapshot; an out-of-range
    part number is a 404 SNAPSHOT_NOT_FOUND, and a reaped part payload (meta
    still present) is a 410, mirroring the rows-payload policy.
    """
    if settings is None:
        settings = get_settings()
    store = _resolve_store(settings, store)

    meta = _load_meta(store, settings, snapshot_id)
    _check_expiry(meta)

    parts = meta.get("parts", [])
    if not 1 <= part <= len(parts):
        raise SnapshotNotFoundError(
            message="Snapshot not found.", code="SNAPSHOT_NOT_FOUND"
        )

    filename = f"query-export-part-{part:04d}.csv"
    try:
        reader = store.open_reader(parts[part - 1]["key"])
    except SnapshotObjectMissingError as exc:
        raise SnapshotExpiredError(
            message="This snapshot has expired. Refresh the data to mint a new one.",
            code="SNAPSHOT_EXPIRED",
        ) from exc

    def _iter_bytes() -> Iterator[bytes]:
        with reader:
            while True:
                buf = reader.read(64 * 1024)
                if not buf:
                    break
                yield buf

    return _iter_bytes(), filename
