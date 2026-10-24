"""S3-compatible object store for answer-snapshot exports.

Thin boto3 wrapper targeting on-prem S3-compatible storage (Dell ECS / NetApp
StorageGRID) — path-style addressing is mandatory on those appliances, and the
endpoint comes from config (``EXPORT_S3_ENDPOINT``).  The surface is kept
deliberately small and synchronous (the FastAPI routes calling it run in the
threadpool) and is designed to be swapped for an in-memory fake in unit tests —
app/export_service.py only ever calls these six methods; tests never touch the
network.

boto3/botocore are imported LAZILY inside the methods that need them: the
snapshot store is pluggable (EXPORT_STORE=couchbase|s3), and a Couchbase-mode
deployment must not require the S3 SDK at import time (the same rule applies
the other way around — see app/export_store_couchbase.py).

Object layout produced/consumed via this store (see app/export_service.py)::

    {EXPORT_S3_PREFIX}/{tenant_hash}/{snapshot_id}/rows.jsonl.gz
    {EXPORT_S3_PREFIX}/{tenant_hash}/{snapshot_id}/data.part-NNNN.csv
    {EXPORT_S3_PREFIX}/{tenant_hash}/{snapshot_id}/meta.json     (written LAST)

Never log keys' presigned URLs or object contents; snapshot_id is safe to log.
"""

from __future__ import annotations

import logging
from typing import BinaryIO

from app.config import Settings

logger = logging.getLogger(__name__)


class SnapshotObjectMissingError(Exception):
    """The requested object (or prefix) does not exist in the bucket.

    Raised by read methods on a genuine miss.  The service layer maps this to
    SNAPSHOT_NOT_FOUND — it never distinguishes a foreign-tenant id from an
    unknown id, and this single exception type keeps that boundary clean.
    """


class S3SnapshotStore:
    """boto3-backed store for snapshot objects under a configured bucket/prefix.

    All methods that talk to S3 may raise ``botocore.exceptions.ClientError``
    (translated to :class:`SnapshotObjectMissingError` on a missed read) or a
    network-level ``BotoCoreError``; the service layer treats any such failure
    as an upload/read failure and cleans up/raises accordingly.
    """

    def __init__(self, settings: Settings) -> None:
        # Lazy SDK imports (see module docstring): an EXPORT_STORE=couchbase
        # deployment imports this module only for SnapshotObjectMissingError.
        import boto3
        from botocore.client import Config

        self._bucket = settings.export_s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.export_s3_endpoint or None,
            region_name=settings.export_s3_region,
            aws_access_key_id=settings.export_s3_access_key or None,
            aws_secret_access_key=settings.export_s3_secret_key or None,
            # Path-style addressing is REQUIRED by the on-prem targets (Dell ECS
            # / NetApp StorageGRID); virtual-hosted style would build hosts like
            # <bucket>.<endpoint> that their TLS/DNS does not serve.  SigV4 is
            # pinned explicitly: botocore presigns S3 URLs with legacy SigV2
            # unless told otherwise, and the modern on-prem appliances expect V4.
            config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    def put_file(self, local_path: str, key: str) -> None:
        """Upload the staged file at *local_path* to ``s3://bucket/key``."""
        from boto3.s3.transfer import TransferConfig
        # The HTTP worker is already dedicated to this export; avoid spawning
        # another transfer thread pool for every file under concurrent load.
        self._client.upload_file(local_path, self._bucket, key,
                                 Config=TransferConfig(use_threads=False))

    def read_bytes(self, key: str) -> bytes:
        """Return the full object body (small objects only — meta.json)."""
        from botocore.exceptions import ClientError

        try:
            with self._client.get_object(Bucket=self._bucket, Key=key)["Body"] as body:
                return body.read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "NoSuchBucket", "404"):
                raise SnapshotObjectMissingError(key) from exc
            raise

    def open_reader(self, key: str) -> BinaryIO:
        """Return a streaming reader for *key* (rows.jsonl.gz — never buffered whole).

        The returned StreamingBody is file-like and must be closed by the caller.
        """
        from botocore.exceptions import ClientError

        try:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "NoSuchBucket", "404"):
                raise SnapshotObjectMissingError(key) from exc
            raise

    def delete_prefix(self, prefix: str) -> None:
        """Delete every object under *prefix* (best-effort partial-snapshot cleanup).

        Deletes are exact-key ops in S3, so this lists the prefix first and then
        deletes in 1000-key batches; called on abort/expiry-cleanup paths where
        the snapshot may be only partially uploaded.
        """
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:
                self._client.delete_objects(Bucket=self._bucket, Delete={"Objects": keys})

    def presign_get(self, key: str, *, filename: str, expires_in: int) -> str:
        """Return a presigned GET URL for *key* that downloads as *filename*.

        ``ResponseContentDisposition`` is signed into the URL so the on-prem
        store answers the GET with ``attachment; filename="…"`` — the browser
        saves the CSV under the contract filename rather than the object key.
        """
        return self._client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self._bucket,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=expires_in,
        )

    def read_idempotency(self, key: str):
        """Read the claim and its ETag for conditional replacement."""
        import json
        from botocore.exceptions import ClientError
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            with response["Body"] as body:
                return json.loads(body.read()), response["ETag"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise SnapshotObjectMissingError(key) from exc
            raise

    def write_idempotency(self, key: str, value: dict, *, version=None):
        """Atomic claim/CAS update. Never fall back to an unconditional write."""
        import json
        from botocore.exceptions import ClientError
        condition = {"IfNoneMatch": "*"} if version is None else {"IfMatch": version}
        try:
            response = self._client.put_object(
                Bucket=self._bucket, Key=key, ContentType="application/json",
                Body=json.dumps(value, sort_keys=True, separators=(",", ":")).encode(), **condition,
            )
            return response["ETag"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in (
                "PreconditionFailed", "ConditionalRequestConflict", "412", "409",
            ) or (version is not None and exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404")):
                return None
            raise
