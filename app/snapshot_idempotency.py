"""Store-backed snapshot creation claims shared by all API replicas.

A pending claim is never stolen before expiry: a crashed/uncertain creator may
have executed SQL already. Completed snapshot metadata is the commit point,
so a lost HTTP response or claim-status write does not cause re-execution.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid

from app.errors import ClickHouseAPIError, QueryValidationError
from app.export_client import SnapshotObjectMissingError
from app.principal import get_current_principal, get_current_session_id

logger = logging.getLogger(__name__)


class SnapshotIdempotencyError(ClickHouseAPIError):
    """409: creation still running or a prior failed creation."""


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def create_idempotent(sql, settings, store, *, refresh_of=None):
    from app import export_service as service

    principal = get_current_principal()
    if principal is None:
        raise QueryValidationError("Snapshot idempotency requires an authenticated caller.",
                                   "SNAPSHOT_IDENTITY_REQUIRED")
    owner = _digest({"identity": service._snapshot_identity(settings), "subject": principal.subject})
    scope = principal.catalog_scope
    # SQL is the entire current request body. Keep exact text: normalizing SQL
    # could change literal semantics. Include both schema and column grants.
    fingerprint = _digest({
        "version": 3, "sql": sql, "session": get_current_session_id(),
        "refresh_of": refresh_of,
        "schemas": sorted(scope.schemas) if scope and scope.schemas is not None else None,
        "columns": sorted(scope.columns) if scope and scope.columns is not None else None,
    })
    record_key = (f"snap-idem::{owner}::{fingerprint}" if settings.export_store == "couchbase"
                  else f"{settings.export_s3_prefix.strip('/') + '/' if settings.export_s3_prefix.strip('/') else ''}_idempotency/{owner}/{fingerprint}.json")

    for _ in range(4):
        now = time.time()
        try:
            record, version = store.read_idempotency(record_key)
        except SnapshotObjectMissingError:
            record, version = None, None
        if record is not None and record["expires_at"] > now:
            # Check the commit point before status: the creator can crash after
            # committing metadata but before marking the claim completed.
            try:
                meta = service._load_meta(store, settings, record["snapshot_id"])
            except service.SnapshotNotFoundError:
                meta = None
            if meta is not None:
                service._check_expiry(meta)
                return {"snapshot_id": meta["snapshot_id"],
                        "columns": [c["name"] for c in meta["columns"]],
                        "row_count": meta["row_count"], "created_at": meta["created_at"],
                        "expires_at": meta["expires_at"]}
            if record["status"] == "completed":
                raise service.SnapshotExpiredError("This snapshot is no longer available. Retry after the retention window expires.",
                                                   "SNAPSHOT_EXPIRED")
            if record["status"] == "failed":
                raise SnapshotIdempotencyError("The previous snapshot creation failed. Retry after the retention window expires or in a new session.",
                                               "SNAPSHOT_CREATION_FAILED")
            raise SnapshotIdempotencyError("Snapshot creation is still pending. Retry the same request shortly.",
                                           "SNAPSHOT_IN_PROGRESS")

        if refresh_of is not None:
            # Authorize the source before executing a fresh query. Expired but
            # retained metadata is sufficient; never permit cross-session refresh.
            service._load_meta(store, settings, refresh_of, allow_scope_change=True)

        claim = {"version": 1, "fingerprint": fingerprint, "snapshot_id": str(uuid.uuid4()),
                 "status": "pending", "expires_at": now + settings.export_ttl_seconds}
        owned_version = store.write_idempotency(record_key, claim, version=version)
        if owned_version is not None:
            break
    else:
        raise SnapshotIdempotencyError("Snapshot creation is contended. Retry the same request shortly.",
                                       "SNAPSHOT_IN_PROGRESS")

    try:
        response = service._create_snapshot(sql, settings, store=store,
                                            snapshot_id=claim["snapshot_id"],
                                            expires_at=claim["expires_at"])
    except Exception:
        _finish(store, record_key, claim, owned_version, "failed")
        raise
    _finish(store, record_key, claim, owned_version, "completed")
    return response


def _finish(store, key, claim, version, status):
    try:
        store.write_idempotency(key, {**claim, "status": status}, version=version)
    except Exception as exc:
        # A failed status update never invalidates an already committed snapshot
        # or overwrites the original query error. Pending claims remain fenced.
        logger.warning("snapshot claim update failed status=%s failure_class=%s", status, type(exc).__name__)
