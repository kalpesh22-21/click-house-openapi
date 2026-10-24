"""Tests for the Couchbase mode (EXPORT_STORE=couchbase) of app/export_service.py.

The store is a FakeCouchbaseStore mirroring CouchbaseSnapshotStore's service-facing
ops behind an in-memory dict — never any SDK or network.  Covers: doc layout/keys
and meta-LAST commit, per-row chunk math, paging across chunk boundaries, the
CB-mode download_urls shape (relative urls, expires_in null), download_part
streaming and 404/410 semantics, abort cleanup (chunk/docs deleted), and the
tenant/expiry invariants shared with S3 mode.  The stream stub and principal
helpers are reused from tests/test_export_service.py.
"""

from __future__ import annotations

import contextlib
import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.config import Settings
from app.errors import (
    ExportTooLargeError,
    SnapshotExpiredError,
    SnapshotNotFoundError,
)
from app.export_client import SnapshotObjectMissingError
from app.export_service import (
    _create_snapshot as create_snapshot,
    download_part,
    download_urls,
    page_snapshot,
)
from app.export_store_couchbase import CouchbaseSnapshotStore
from tests.test_export_service import _bound_principal, _sorted_thash, _stub_stream

# ---------------------------------------------------------------------------
# Fake store
# ---------------------------------------------------------------------------


class FakeCouchbaseStore:
    """Dict-backed stand-in for CouchbaseSnapshotStore (records write order)."""

    def __init__(self) -> None:
        self.docs: dict[str, bytes] = {}
        self.writes: list[str] = []  # write order (commit-point check)
        self.deleted_prefixes: list[str] = []

    def put_json(self, key: str, value: Any) -> None:
        self.docs[key] = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.writes.append(key)

    def put_file(self, local_path: str, key: str) -> None:
        with open(local_path, "rb") as fh:
            self.docs[key] = fh.read()
        self.writes.append(key)

    def read_bytes(self, key: str) -> bytes:
        if key not in self.docs:
            raise SnapshotObjectMissingError(key)
        return self.docs[key]

    def open_reader(self, key: str):
        if key not in self.docs:
            raise SnapshotObjectMissingError(key)
        return io.BytesIO(self.docs[key])

    def delete_prefix(self, prefix: str) -> None:
        self.deleted_prefixes.append(prefix)
        for key in [k for k in self.docs if k.startswith(prefix)]:
            del self.docs[key]


# ---------------------------------------------------------------------------
# Settings / fixture helpers
# ---------------------------------------------------------------------------


def _settings(**overrides) -> Settings:
    base: dict[str, Any] = {
        "clickhouse_host": "localhost",
        "clickhouse_user": "default",
        "employee_access_enabled": False,
        "restrict_to_catalogued_tables": False,
        "export_enabled": True,
        "export_store": "couchbase",
        "export_cb_conn_str": "couchbase://cb.example.com",
        "export_cb_bucket": "agent_snapshots",
        "export_cb_username": "snapshot-writer",
        "export_cb_password": "secret",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _prefix(settings: Settings, snapshot_id: str) -> str:
    return CouchbaseSnapshotStore.prefix(_sorted_thash(settings), snapshot_id)


def _docs_for(store: FakeCouchbaseStore, settings: Settings, snapshot_id: str) -> dict[str, bytes]:
    prefix = _prefix(settings, snapshot_id)
    return {k: v for k, v in store.docs.items() if k.startswith(prefix)}


def _read_meta(store: FakeCouchbaseStore, settings: Settings, snapshot_id: str) -> dict:
    return json.loads(_docs_for(store, settings, snapshot_id)[_prefix(settings, snapshot_id) + "meta"])


@contextlib.contextmanager
def _minted(settings: Settings, store: FakeCouchbaseStore, rows, columns=("id",), type_names=("UInt8",)):
    with _bound_principal(), _stub_stream(list(columns), list(type_names), rows):
        yield create_snapshot("SELECT id FROM t", settings, store=store)


# ---------------------------------------------------------------------------
# create_snapshot — CB mint layout
# ---------------------------------------------------------------------------


class TestCreateSnapshotCouchbase:

    def test_doc_layout_and_meta_last_commit(self):
        settings = _settings()
        store = FakeCouchbaseStore()
        rows = [[1, "a"], [2, "b"], [3, "c"]]
        with _bound_principal(), _stub_stream(["id", "name"], ["UInt8", "String"], rows):
            result = create_snapshot("SELECT id, name FROM t", settings, store=store)

        sid = result["snapshot_id"]
        prefix = _prefix(settings, sid)
        assert prefix.startswith("snap::") and prefix.endswith("::")
        assert set(_docs_for(store, settings, sid)) == {
            prefix + "rows.0000",
            prefix + "csv.0001",
            prefix + "meta",
        }
        # meta written LAST (commit point).
        assert store.writes[-1] == prefix + "meta"
        # Row chunks were written DURING streaming (before any csv part).
        assert store.writes.index(prefix + "rows.0000") < store.writes.index(prefix + "csv.0001")

        # Chunk doc is a JSON array of wire rows.
        chunk = json.loads(store.docs[prefix + "rows.0000"])
        assert chunk == [[1, "a"], [2, "b"], [3, "c"]]

        # CSV part doc bytes are identical to the S3-mode part staging.
        csv_lines = store.docs[prefix + "csv.0001"].decode("utf-8").splitlines()
        assert csv_lines == ["id,name", "1,a", "2,b", "3,c"]

        meta = _read_meta(store, settings, sid)
        assert meta["snapshot_id"] == sid
        assert meta["tenant_hash"] == _sorted_thash(settings)
        assert meta["row_count"] == 3
        assert meta["chunk_rows"] == settings.export_cb_chunk_rows
        assert [c["name"] for c in meta["columns"]] == ["id", "name"]
        assert meta["parts"] == [
            {"key": prefix + "csv.0001", "rows": 3, "bytes": len(store.docs[prefix + "csv.0001"])}
        ]
        assert meta["sql_sha256"]
        assert meta["source_sql"] == "SELECT id, name FROM t"
        assert "source_sql" not in result

    def test_chunk_math_splits_rows_into_docs(self):
        settings = _settings(export_cb_chunk_rows=3)
        store = FakeCouchbaseStore()
        rows = [[i] for i in range(7)]
        with _bound_principal(), _stub_stream(["id"], ["UInt8"], rows):
            result = create_snapshot("SELECT id FROM t", settings, store=store)

        prefix = _prefix(settings, result["snapshot_id"])
        chunk_keys = sorted(k for k in store.docs if "::rows." in k)
        assert chunk_keys == [
            prefix + "rows.0000",
            prefix + "rows.0001",
            prefix + "rows.0002",
        ]
        counts = [len(json.loads(store.docs[k])) for k in chunk_keys]
        assert counts == [3, 3, 1]
        assert _read_meta(store, settings, result["snapshot_id"])["chunk_rows"] == 3

    def test_zero_row_snapshot_mints_meta_only(self):
        settings = _settings()
        store = FakeCouchbaseStore()
        with _minted(settings, store, []) as created:
            sid = created["snapshot_id"]
        assert created["row_count"] == 0
        prefix = _prefix(settings, sid)
        assert set(store.docs) == {prefix + "meta"}
        meta = _read_meta(store, settings, sid)
        assert meta["row_count"] == 0
        assert meta["parts"] == []

    def test_part_split_math_identical_to_s3_mode(self):
        # Same per-part row cap semantics as S3 mode (parts 1-indexed).
        settings = _settings(export_max_rows=2, export_part_max_bytes=10**6)
        store = FakeCouchbaseStore()
        rows = [[i] for i in range(5)]
        with _minted(settings, store, rows) as created:
            sid = created["snapshot_id"]
        prefix = _prefix(settings, sid)
        csv_keys = sorted(k for k in store.docs if "::csv." in k)
        assert csv_keys == [prefix + "csv.0001", prefix + "csv.0002", prefix + "csv.0003"]
        for key in csv_keys:
            assert store.docs[key].decode("utf-8").splitlines()[0] == "id"
        meta = _read_meta(store, settings, sid)
        assert [p["rows"] for p in meta["parts"]] == [2, 2, 1]

    def test_budget_abort_cleans_streamed_chunk_docs(self):
        # Chunk docs ARE already persisted when the budget trips — the abort
        # path must delete_prefix them (no orphan readable snapshot).
        settings = _settings(export_total_max_rows=2, export_cb_chunk_rows=1)
        store = FakeCouchbaseStore()
        rows = [[i] for i in range(5)]
        with (
            _bound_principal(),
            _stub_stream(["id"], ["UInt8"], rows),
            pytest.raises(ExportTooLargeError) as exc_info,
        ):
            create_snapshot("SELECT id FROM t", settings, store=store)
        assert exc_info.value.code == "EXPORT_TOO_LARGE"
        assert store.docs == {}
        assert store.deleted_prefixes, "abort path must delete_prefix the snapshot docs"


# ---------------------------------------------------------------------------
# page_snapshot — CB chunk-window reads
# ---------------------------------------------------------------------------


class TestPageSnapshotCouchbase:

    def test_page_across_chunk_boundaries(self):
        settings = _settings(export_cb_chunk_rows=3)
        store = FakeCouchbaseStore()
        rows = [[i] for i in range(8)]
        with _minted(settings, store, rows) as created:
            sid = created["snapshot_id"]
            with _bound_principal():
                page = page_snapshot(sid, 2, 4, settings, store=store)  # spans chunks 0+1
        assert page["rows"] == [[2], [3], [4], [5]]
        assert page["has_more"] is True  # 2 + 4 < 8
        assert page["row_count"] == 8
        assert page["snapshot_id"] == sid
        assert page["expires_at"].endswith("Z")

    def test_page_single_doc_read_when_within_one_chunk(self):
        settings = _settings(export_cb_chunk_rows=10)
        store = FakeCouchbaseStore()
        with _minted(settings, store, [[i] for i in range(8)]) as created:
            sid = created["snapshot_id"]
            with _bound_principal():
                page = page_snapshot(sid, 0, 3, settings, store=store)
        assert page["rows"] == [[0], [1], [2]]
        assert page["has_more"] is True

    def test_offset_past_end_returns_empty_not_error(self):
        settings = _settings()
        store = FakeCouchbaseStore()
        with _minted(settings, store, [[1], [2]]) as created:
            sid = created["snapshot_id"]
            with _bound_principal():
                page = page_snapshot(sid, 5, 100, settings, store=store)
        assert page["rows"] == []
        assert page["has_more"] is False

    def test_exact_last_page_ends_stream(self):
        settings = _settings(export_cb_chunk_rows=2)
        store = FakeCouchbaseStore()
        with _minted(settings, store, [[1], [2], [3], [4]]) as created:
            sid = created["snapshot_id"]
            with _bound_principal():
                page = page_snapshot(sid, 3, 100, settings, store=store)
        assert page["rows"] == [[4]]  # short read at the tail, not an empty non-stop loop
        assert page["has_more"] is False

    def test_missing_chunk_doc_is_410_expired(self):
        settings = _settings()
        store = FakeCouchbaseStore()
        with _minted(settings, store, [[1]]) as created:
            sid = created["snapshot_id"]
        prefix = _prefix(settings, sid)
        del store.docs[prefix + "rows.0000"]  # payload reaped ahead of the clock
        with _bound_principal(), pytest.raises(SnapshotExpiredError) as exc_info:
            page_snapshot(sid, 0, 100, settings, store=store)
        assert exc_info.value.code == "SNAPSHOT_EXPIRED"

    def test_foreign_tenant_is_indistinguishable_404(self):
        settings = _settings()
        store = FakeCouchbaseStore()
        with _minted(settings, store, [[1]]) as created:
            sid = created["snapshot_id"]
            with _bound_principal(client_code="CLIENT_B"):
                with pytest.raises(SnapshotNotFoundError) as foreign:
                    page_snapshot(sid, 0, 100, settings, store=store)
                with pytest.raises(SnapshotNotFoundError) as unknown:
                    page_snapshot(str(uuid.uuid4()), 0, 100, settings, store=store)
        assert foreign.value.code == unknown.value.code == "SNAPSHOT_NOT_FOUND"
        assert foreign.value.message == unknown.value.message

    def test_expired_snapshot_is_410(self):
        settings = _settings()
        store = FakeCouchbaseStore()
        with _minted(settings, store, [[1]]) as created:
            sid = created["snapshot_id"]
        meta_key = _prefix(settings, sid) + "meta"
        meta = json.loads(store.docs[meta_key])
        meta["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        store.docs[meta_key] = json.dumps(meta).encode("utf-8")
        with _bound_principal(), pytest.raises(SnapshotExpiredError):
            page_snapshot(sid, 0, 100, settings, store=store)


# ---------------------------------------------------------------------------
# download_urls — CB-mode wire shape
# ---------------------------------------------------------------------------


class TestDownloadUrlsCouchbase:

    def test_relative_urls_and_null_expiry(self):
        settings = _settings(export_max_rows=2, export_part_max_bytes=10**6)
        store = FakeCouchbaseStore()
        with _minted(settings, store, [[i] for i in range(5)]) as created:
            sid = created["snapshot_id"]
            with _bound_principal():
                result = download_urls(sid, settings, store=store)

        assert result["expires_in"] is None
        files = result["files"]
        assert [f["part"] for f in files] == [1, 2, 3]
        assert [f["rows"] for f in files] == [2, 2, 1]
        assert [f["url"] for f in files] == [
            f"query/snapshots/{sid}/download?part={i}" for i in (1, 2, 3)
        ]
        assert [f["filename"] for f in files] == [
            "query-export-part-0001.csv",
            "query-export-part-0002.csv",
            "query-export-part-0003.csv",
        ]

    def test_zero_row_snapshot_has_no_files(self):
        settings = _settings()
        store = FakeCouchbaseStore()
        with _minted(settings, store, []) as created:
            sid = created["snapshot_id"]
            with _bound_principal():
                result = download_urls(sid, settings, store=store)
        assert result == {"files": [], "expires_in": None}

    def test_foreign_tenant_404_and_expired_410(self):
        settings = _settings()
        store = FakeCouchbaseStore()
        with _minted(settings, store, [[1]]) as created:
            sid = created["snapshot_id"]
        with _bound_principal(client_code="CLIENT_B"), pytest.raises(SnapshotNotFoundError):
            download_urls(sid, settings, store=store)
        meta_key = _prefix(settings, sid) + "meta"
        meta = json.loads(store.docs[meta_key])
        meta["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        store.docs[meta_key] = json.dumps(meta).encode("utf-8")
        with _bound_principal(), pytest.raises(SnapshotExpiredError):
            download_urls(sid, settings, store=store)


# ---------------------------------------------------------------------------
# download_part — the CB-mode download transport
# ---------------------------------------------------------------------------


class TestDownloadPart:

    def _read_all(self, byte_iter) -> bytes:
        return b"".join(byte_iter)

    def test_streams_stored_csv_part_bytes(self):
        settings = _settings()
        store = FakeCouchbaseStore()
        rows = [[1, "a"], [2, "caf\x00\x00"]]
        with _bound_principal(), _stub_stream(["id", "name"], ["UInt8", "String"], rows):
            created = create_snapshot("SELECT id, name FROM t", settings, store=store)
            sid = created["snapshot_id"]
            byte_iter, filename = download_part(sid, 1, settings, store=store)

        assert filename == "query-export-part-0001.csv"
        body = self._read_all(byte_iter).decode("utf-8")
        # NUL-padding cleaned exactly like S3-mode staging.
        assert body.splitlines() == ["id,name", "1,a", "2,caf"]
        assert body.startswith("id,name\r\n")  # CSV writer terminators preserved

    def test_part_out_of_range_is_404(self):
        settings = _settings()
        store = FakeCouchbaseStore()
        with _minted(settings, store, [[1]]) as created:
            sid = created["snapshot_id"]
            with _bound_principal():
                for bad in (0, 2, 99, -1):
                    with pytest.raises(SnapshotNotFoundError) as exc_info:
                        download_part(sid, bad, settings, store=store)
                    assert exc_info.value.code == "SNAPSHOT_NOT_FOUND"

    def test_foreign_tenant_404_and_expired_410(self):
        settings = _settings()
        store = FakeCouchbaseStore()
        with _minted(settings, store, [[1]]) as created:
            sid = created["snapshot_id"]
        with _bound_principal(client_code="CLIENT_B"), pytest.raises(SnapshotNotFoundError):
            download_part(sid, 1, settings, store=store)
        meta_key = _prefix(settings, sid) + "meta"
        meta = json.loads(store.docs[meta_key])
        meta["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        store.docs[meta_key] = json.dumps(meta).encode("utf-8")
        with _bound_principal(), pytest.raises(SnapshotExpiredError):
            download_part(sid, 1, settings, store=store)

    def test_reaped_part_doc_is_410_expired(self):
        settings = _settings()
        store = FakeCouchbaseStore()
        with _minted(settings, store, [[1]]) as created:
            sid = created["snapshot_id"]
        del store.docs[_prefix(settings, sid) + "csv.0001"]
        with _bound_principal(), pytest.raises(SnapshotExpiredError):
            download_part(sid, 1, settings, store=store)

    def test_no_parts_means_any_part_is_404(self):
        settings = _settings()
        store = FakeCouchbaseStore()
        with _minted(settings, store, []) as created:
            sid = created["snapshot_id"]
            with _bound_principal(), pytest.raises(SnapshotNotFoundError):
                download_part(sid, 1, settings, store=store)
