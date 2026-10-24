"""Tests for app/export_client.py — the boto3 S3 snapshot store.

Never any network: botocore's Stubber intercepts the HTTP layer for
upload/read/delete calls, and generate_presigned_url is computed locally by
botocore (SigV4 signing happens client-side), so its output URL can be parsed
and asserted on directly.
"""

from __future__ import annotations

import io
import urllib.parse
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from app.export_client import S3SnapshotStore, SnapshotObjectMissingError


def _settings(**overrides):
    """Build a Settings carrying a full export S3 identity, bypassing env/validators."""
    from app.config import Settings

    base = {
        "clickhouse_host": "localhost",
        "clickhouse_user": "default",
        "employee_access_enabled": False,
        "export_enabled": True,
        "export_s3_endpoint": "https://s3.example.com",
        "export_s3_bucket": "snapshot-bucket",
        "export_s3_region": "us-east-1",
        "export_s3_prefix": "exports",
        "export_s3_access_key": "AKIDEXAMPLE",
        "export_s3_secret_key": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture()
def store():
    return S3SnapshotStore(_settings())


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------

class TestClientConstruction:

    def test_path_style_addressing_and_endpoint(self, store):
        meta = store._client.meta
        assert meta.config.s3 == {"addressing_style": "path"}
        assert meta.config.signature_version == "s3v4"
        assert meta.endpoint_url == "https://s3.example.com"
        assert meta.region_name == "us-east-1"
        assert store.bucket == "snapshot-bucket"


# ---------------------------------------------------------------------------
# Upload / read / delete via Stubber
# ---------------------------------------------------------------------------

class TestObjectOperations:

    def test_put_file_uploads_to_bucket_and_key(self, store, tmp_path):
        staged = tmp_path / "data.part-0001.csv"
        staged.write_bytes(b"a,b\r\n1,2\r\n")
        with patch.object(store._client, "upload_file") as upload:
            store.put_file(str(staged), "exports/h/snap/data.part-0001.csv")
        assert upload.call_args.args == (str(staged), "snapshot-bucket", "exports/h/snap/data.part-0001.csv")
        assert upload.call_args.kwargs["Config"].use_threads is False

    def test_read_bytes_returns_body(self, store):
        with Stubber(store._client) as stub:
            stub.add_response(
                "get_object",
                {"Body": io.BytesIO(b'{"row_count": 3}')},
                expected_params={"Bucket": "snapshot-bucket", "Key": "exports/h/snap/meta.json"},
            )
            assert store.read_bytes("exports/h/snap/meta.json") == b'{"row_count": 3}'

    def test_read_bytes_missing_key_raises_missing(self, store):
        with Stubber(store._client) as stub:
            stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
            with pytest.raises(SnapshotObjectMissingError):
                store.read_bytes("exports/h/nope/meta.json")

    def test_open_reader_streams_body(self, store):
        with Stubber(store._client) as stub:
            stub.add_response(
                "get_object",
                {"Body": io.BytesIO(b"line1\nline2\n")},
                expected_params={"Bucket": "snapshot-bucket", "Key": "exports/h/snap/rows.jsonl.gz"},
            )
            reader = store.open_reader("exports/h/snap/rows.jsonl.gz")
            with reader:
                assert reader.read() == b"line1\nline2\n"

    def test_open_reader_missing_key_raises_missing(self, store):
        with Stubber(store._client) as stub:
            stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
            with pytest.raises(SnapshotObjectMissingError):
                store.open_reader("exports/h/nope/rows.jsonl.gz")

    def test_other_client_errors_propagate(self, store):
        with Stubber(store._client) as stub:
            stub.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
            with pytest.raises(ClientError):
                store.read_bytes("exports/h/snap/meta.json")

    def test_delete_prefix_lists_and_deletes_in_order(self, store):
        with Stubber(store._client) as stub:
            stub.add_response(
                "list_objects_v2",
                {"Contents": [{"Key": "exports/h/snap/meta.json"}, {"Key": "exports/h/snap/rows.jsonl.gz"}]},
                expected_params={"Bucket": "snapshot-bucket", "Prefix": "exports/h/snap"},
            )
            stub.add_response(
                "delete_objects",
                {},
                expected_params={
                    "Bucket": "snapshot-bucket",
                    "Delete": {
                        "Objects": [
                            {"Key": "exports/h/snap/meta.json"},
                            {"Key": "exports/h/snap/rows.jsonl.gz"},
                        ]
                    },
                },
            )
            store.delete_prefix("exports/h/snap")
            stub.assert_no_pending_responses()

    def test_delete_prefix_empty_page_is_noop(self, store):
        with Stubber(store._client) as stub:
            stub.add_response(
                "list_objects_v2",
                {"Contents": []},
                expected_params={"Bucket": "snapshot-bucket", "Prefix": "exports/h/empty"},
            )
            store.delete_prefix("exports/h/empty")
            stub.assert_no_pending_responses()


# ---------------------------------------------------------------------------
# Presigned URLs — computed locally by botocore, asserted by parsing
# ---------------------------------------------------------------------------

class TestPresign:

    def test_presign_params_path_style_and_content_disposition(self, store):
        url = store.presign_get(
            "exports/h/snap/data.part-0001.csv",
            filename="query-export-part-0001.csv",
            expires_in=300,
        )
        parsed = urllib.parse.urlparse(url)

        # Path-style addressing: /{bucket}/{key}, never virtual-hosted bucket DNS.
        assert parsed.netloc == "s3.example.com"
        assert parsed.path == "/snapshot-bucket/exports/h/snap/data.part-0001.csv"

        query = urllib.parse.parse_qs(parsed.query)
        # Lifetime honours the configured presign TTL.
        assert query["X-Amz-Expires"] == ["300"]
        # The download filename is signed into the response headers.
        assert query["response-content-disposition"] == [
            'attachment; filename="query-export-part-0001.csv"'
        ]
        # A real SigV4 signature is present (creds resolved from settings).
        assert query["X-Amz-Signature"]
        cred = query["X-Amz-Credential"][0]
        assert cred.startswith("AKIDEXAMPLE/")
        assert "/us-east-1/s3/aws4_request" in cred

    def test_presign_ttl_is_caller_controlled(self, store):
        url = store.presign_get("exports/h/snap/meta.json", filename="m.json", expires_in=60)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert query["X-Amz-Expires"] == ["60"]


def test_atomic_idempotency_insert_and_conflict(store):
    import json
    value={'status':'pending','expires_at':12345}
    params={'Bucket':store.bucket,'Key':'claims/k','ContentType':'application/json',
            'Body':json.dumps(value,sort_keys=True,separators=(',',':')).encode(),'IfNoneMatch':'*'}
    with Stubber(store._client) as stub:
        stub.add_response('put_object',{'ETag':'"v1"'},params)
        assert store.write_idempotency('claims/k',value)=='"v1"'
        stub.add_client_error('put_object',service_error_code='PreconditionFailed',http_status_code=412,expected_params=params)
        assert store.write_idempotency('claims/k',value) is None
        params={**params,'IfMatch':'"v1"'};params.pop('IfNoneMatch')
        stub.add_response('put_object',{'ETag':'"v2"'},params)
        assert store.write_idempotency('claims/k',value,version='"v1"')=='"v2"'


def test_s3_conditional_write_unsupported_never_falls_back(store):
    with Stubber(store._client) as stub:
        stub.add_client_error('put_object',service_error_code='NotImplemented',http_status_code=501)
        with pytest.raises(ClientError):
            store.write_idempotency('claims/k',{'status':'pending'})
