"""Couchbase-backed snapshot store for answer-snapshot exports (EXPORT_STORE=couchbase).

Doc layout in EXPORT_CB_BUCKET (CONTRACTS §6.1)::

    snap::{tenant_hash}::{snapshot_id}::meta                 JSON meta object
    snap::{tenant_hash}::{snapshot_id}::rows.{chunk:04d}     JSON array of wire rows
    snap::{tenant_hash}::{snapshot_id}::csv.{part:04d}       binary CSV part bytes

Row chunks are 0-indexed and hold EXPORT_CB_CHUNK_ROWS rows each (paging fetches
them by ceil-math — O(1) doc reads, no stream-scan); CSV parts are 1-indexed
with the exact same split math as S3 mode.  The meta doc is written LAST (the
commit point) and carries the same fields as S3-mode meta.json plus
``chunk_rows``.

Every write (upsert) carries a per-doc ``expiry`` of EXPORT_TTL_SECONDS; the
bucket's ops-provisioned maxTTL (14400s, matching the TTL) is the datastore-level
backstop.  App-side ``_check_expiry`` stays the authoritative TTL enforcement —
belt and suspenders.

The same small surface as the S3 store (put_file / read_bytes / open_reader /
delete_prefix) is provided, plus ``put_json`` for the JSON-typed docs (meta,
row chunks) — the CSV part docs arrive through put_file as staged file bytes,
exactly like the S3 adapter.

The couchbase SDK is imported LAZILY — only inside this module's functions —
so an EXPORT_STORE=s3 deployment needs no couchbase SDK at import time (the
mirror rule of the lazy boto3 imports in app/export_client.py).  Tests inject
an in-memory fake collection via the ``_connect`` seam and never touch a
network.
"""

from __future__ import annotations

import io
import json
import logging
from datetime import timedelta
from typing import Any, BinaryIO

from app.config import Settings
from app.export_client import SnapshotObjectMissingError

logger = logging.getLogger(__name__)


def _connect(settings: Settings) -> Any:
    """Build the default-collection KV handle for the EXPORT_CB_* identity.

    Isolated so tests can patch it without ever importing the real SDK.  The
    SDK is imported here, on first CB-mode construction, never at module import.
    """
    from couchbase.auth import PasswordAuthenticator
    from couchbase.cluster import Cluster
    from couchbase.options import ClusterOptions

    authenticator = PasswordAuthenticator(
        settings.export_cb_username, settings.export_cb_password
    )
    cluster = Cluster(
        settings.export_cb_conn_str, ClusterOptions(authenticator)
    )
    bucket = cluster.bucket(settings.export_cb_bucket)
    return bucket.default_collection()


def _document_not_found_exception() -> type[Exception]:
    """The SDK's not-found exception type, imported lazily for the except clause."""
    from couchbase.exceptions import DocumentNotFoundException

    return DocumentNotFoundException


class CouchbaseSnapshotStore:
    """KV-backed store for snapshot docs under a dedicated Couchbase bucket.

    All KV errors from the underlying SDK may propagate to the caller EXCEPT a
    missed read, which is translated to
    :class:`app.export_client.SnapshotObjectMissingError` — the service layer
    maps that single exception to SNAPSHOT_NOT_FOUND and never distinguishes a
    foreign-tenant id from an unknown id.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._collection = _connect(settings)

    # -- doc-key layout -----------------------------------------------------

    @staticmethod
    def prefix(tenant_hash: str, snapshot_id: str) -> str:
        """Doc-id prefix owning every doc of one snapshot."""
        return f"snap::{tenant_hash}::{snapshot_id}::"

    @staticmethod
    def meta_key(prefix: str) -> str:
        return f"{prefix}meta"

    @staticmethod
    def rows_chunk_key(prefix: str, chunk_no: int) -> str:
        return f"{prefix}rows.{chunk_no:04d}"

    @staticmethod
    def csv_part_key(prefix: str, part_no: int) -> str:
        return f"{prefix}csv.{part_no:04d}"

    # -- writes (per-doc expiry on EVERY write) ------------------------------

    def _expiry(self) -> timedelta:
        return timedelta(seconds=self._settings.export_ttl_seconds)

    def put_json(self, key: str, value: Any) -> None:
        """Upsert a JSON-typed doc (meta object / rows-chunk array) with expiry."""
        self._collection.upsert(key, value, expiry=self._expiry())

    def put_file(self, local_path: str, key: str) -> None:
        """Upsert the staged file's bytes as a binary doc (CSV parts) with expiry."""
        from couchbase.transcoder import RawBinaryTranscoder

        with open(local_path, "rb") as fh:
            self._collection.upsert(
                key, fh.read(), expiry=self._expiry(), transcoder=RawBinaryTranscoder()
            )

    # -- reads ---------------------------------------------------------------

    def read_bytes(self, key: str) -> bytes:
        """Return the doc content as bytes.

        Binary docs (CSV parts) come back verbatim; JSON docs (meta, row
        chunks) are re-encoded to canonical compact JSON bytes so the service
        can ``json.loads`` them exactly like the S3-mode meta.json object.
        """
        from couchbase.transcoder import RawBinaryTranscoder

        options = {"transcoder": RawBinaryTranscoder()} if "::csv." in key else {}
        try:
            result = self._collection.get(key, **options)
        except _document_not_found_exception() as exc:
            raise SnapshotObjectMissingError(key) from exc
        value = result.value  # SDK-decoded: bytes for binary docs, dict/list/str for JSON
        if isinstance(value, str):
            return value.encode("utf-8")
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def open_reader(self, key: str) -> BinaryIO:
        """Return a streaming reader over the doc bytes (CSV download path).

        Docs are bounds-checked (CSV parts are capped by EXPORT_PART_MAX_BYTES),
        so a buffered BytesIO is the simple, correct reader for a KV payload.
        """
        return io.BytesIO(self.read_bytes(key))

    # -- cleanup -------------------------------------------------------------

    def delete_prefix(self, prefix: str) -> None:
        """Delete every doc whose id starts with *prefix* (partial-snapshot cleanup).

        KV prefix-scan enumerates the ids (no SQL++ / index needed — the bucket
        purposefully has no query index), then each id is removed.  Called on
        abort/foreign-cleanup paths; per-doc expiry is the backstop for any doc
        the scan fails to remove.
        """
        from couchbase.kv_range_scan import PrefixScan

        ids = [item.id for item in self._collection.scan(PrefixScan(prefix))]
        for doc_id in ids:
            self._collection.remove(doc_id)

    def read_idempotency(self, key: str):
        """Read a JSON claim with its CAS token."""
        try:
            result = self._collection.get(key)
        except _document_not_found_exception() as exc:
            raise SnapshotObjectMissingError(key) from exc
        return result.value, result.cas

    def write_idempotency(self, key: str, value: dict, *, version=None):
        """Insert once or replace the exact claim version, preserving its deadline."""
        import math
        import time
        from couchbase.exceptions import CASMismatchException, DocumentExistsException, DocumentNotFoundException

        expiry = timedelta(seconds=max(1, math.ceil(value["expires_at"] - time.time())))
        try:
            if version is None:
                result = self._collection.insert(key, value, expiry=expiry)
            else:
                result = self._collection.replace(key, value, cas=version, expiry=expiry)
            return result.cas
        except (CASMismatchException, DocumentExistsException, DocumentNotFoundException):
            return None
