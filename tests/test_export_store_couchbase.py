"""Tests for app/export_store_couchbase.py — the Couchbase snapshot store.

Never any network: the store's KV handle is an in-memory FakeCollection
injected through the ``_connect`` seam (which is also lazy-SDK construction),
so these tests exercise the REAL store code — doc layout/keys, per-doc expiry
on every write, JSON-doc byte normalization, binary passthrough, and
prefix-scan cleanup — without an SDK or server.
"""

from __future__ import annotations

import io
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from couchbase.exceptions import DocumentNotFoundException

from app.config import Settings
from app.export_client import SnapshotObjectMissingError
from app.export_store_couchbase import CouchbaseSnapshotStore

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------



class _FakeGetResult:
    def __init__(self, value):
        self.value = value


class FakeCouchbaseCollection:
    """In-memory default-collection stand-in (the store's only KV dependency)."""

    def __init__(self) -> None:
        self.docs: dict[str, object] = {}
        self.expiries: dict[str, timedelta] = {}
        self.write_order: list[str] = []
        self.removed: list[str] = []

    def upsert(self, key, value, expiry=None, transcoder=None):
        self.docs[key] = value
        self.expiries[key] = expiry
        self.write_order.append(key)

    def get(self, key, **options):
        if key not in self.docs:
            raise DocumentNotFoundException(key)
        return _FakeGetResult(self.docs[key])

    def scan(self, scan_type):
        prefix = scan_type.prefix  # PrefixScan built by delete_prefix
        return _FakeScanIterable(
            [k for k in self.docs if isinstance(k, str) and k.startswith(prefix)]
        )

    def remove(self, key):
        self.removed.append(key)
        self.docs.pop(key, None)
        self.expiries.pop(key, None)


class _FakeScanIterable:
    def __init__(self, ids):
        self._ids = ids

    def __iter__(self):
        class _R:
            def __init__(self, id_):
                self.id = id_

        return (_R(i) for i in self._ids)


# ---------------------------------------------------------------------------
# Settings / store helpers
# ---------------------------------------------------------------------------


def _settings(**overrides) -> Settings:
    base = {
        "clickhouse_host": "localhost",
        "clickhouse_user": "default",
        "employee_access_enabled": False,
        "export_enabled": True,
        "export_store": "couchbase",
        "export_cb_conn_str": "couchbase://cb.example.com",
        "export_cb_bucket": "agent_snapshots",
        "export_cb_username": "snapshot-writer",
        "export_cb_password": "secret",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _store(settings: Settings) -> tuple[CouchbaseSnapshotStore, FakeCouchbaseCollection]:
    collection = FakeCouchbaseCollection()
    with patch("app.export_store_couchbase._connect", return_value=collection):
        store = CouchbaseSnapshotStore(settings)
    return store, collection


# ---------------------------------------------------------------------------
# Doc key layout
# ---------------------------------------------------------------------------


class TestDocKeyLayout:

    def test_layout_helpers_match_contract(self):
        prefix = CouchbaseSnapshotStore.prefix("t" * 64, "sid-1")
        assert prefix == f"snap::{'t' * 64}::sid-1::"
        assert CouchbaseSnapshotStore.meta_key(prefix) == prefix + "meta"
        assert CouchbaseSnapshotStore.rows_chunk_key(prefix, 0) == prefix + "rows.0000"
        assert CouchbaseSnapshotStore.rows_chunk_key(prefix, 12) == prefix + "rows.0012"
        assert CouchbaseSnapshotStore.csv_part_key(prefix, 1) == prefix + "csv.0001"

    def test_constructor_resolves_collection_via_connect_seam(self):
        settings = _settings()
        with patch("app.export_store_couchbase._connect") as connect:
            store = CouchbaseSnapshotStore(settings)
        connect.assert_called_once_with(settings)
        assert store._collection is connect.return_value


# ---------------------------------------------------------------------------
# Writes — per-doc expiry on EVERY write
# ---------------------------------------------------------------------------


class TestWrites:

    def test_put_json_stores_json_doc_with_ttl_expiry(self):
        settings = _settings()
        store, col = _store(settings)
        store.put_json("snap::t::s::meta", {"row_count": 2})
        assert col.docs["snap::t::s::meta"] == {"row_count": 2}
        assert col.expiries["snap::t::s::meta"] == timedelta(
            seconds=settings.export_ttl_seconds
        )

    def test_put_json_rows_chunk_doc(self):
        store, col = _store(_settings())
        store.put_json("snap::t::s::rows.0000", [[1, "a"], [2, "b"]])
        assert col.docs["snap::t::s::rows.0000"] == [[1, "a"], [2, "b"]]

    def test_put_file_stages_binary_doc_with_expiry(self, tmp_path):
        settings = _settings(export_ttl_seconds=60)
        store, col = _store(settings)
        staged = tmp_path / "data.part-0001.csv"
        staged.write_bytes(b"a,b\r\n1,2\r\n")
        store.put_file(str(staged), "snap::t::s::csv.0001")
        assert col.docs["snap::t::s::csv.0001"] == b"a,b\r\n1,2\r\n"
        assert col.expiries["snap::t::s::csv.0001"] == timedelta(seconds=60)

    def test_every_write_carries_expiry(self, tmp_path):
        settings = _settings()
        store, col = _store(settings)
        staged = tmp_path / "part.csv"
        staged.write_bytes(b"a\r\n")
        store.put_json("snap::t::s::meta", {})
        store.put_json("snap::t::s::rows.0000", [])
        store.put_file(str(staged), "snap::t::s::csv.0001")
        assert len(col.expiries) == 3
        assert all(
            e == timedelta(seconds=settings.export_ttl_seconds)
            for e in col.expiries.values()
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class TestReads:

    def test_read_bytes_returns_binary_csv_verbatim(self):
        store, col = _store(_settings())
        col.docs["snap::t::s::csv.0001"] = b"a,b\r\n1,caf\xc3\xa9\r\n"
        assert store.read_bytes("snap::t::s::csv.0001") == b"a,b\r\n1,caf\xc3\xa9\r\n"

    def test_read_bytes_normalizes_json_doc_to_compact_bytes(self):
        store, col = _store(_settings())
        col.docs["snap::t::s::meta"] = {"row_count": 2, "parts": [{"key": "k", "rows": 2}]}
        col.docs["snap::t::s::rows.0000"] = [[1, "café"], [2, None]]
        assert json.loads(store.read_bytes("snap::t::s::meta")) == col.docs["snap::t::s::meta"]
        # Compact separators (matches the S3-mode meta.json staging shape).
        assert store.read_bytes("snap::t::s::meta").startswith(b'{"row_count":2,')
        assert store.read_bytes("snap::t::s::rows.0000") == '[[1,"café"],[2,null]]'.encode()

    def test_read_bytes_missing_doc_raises_missing(self):
        store, _ = _store(_settings())
        with pytest.raises(SnapshotObjectMissingError):
            store.read_bytes("snap::t::nope::meta")

    def test_open_reader_streams_doc_bytes(self):
        store, col = _store(_settings())
        col.docs["snap::t::s::csv.0002"] = b"a\r\nb\r\n"
        reader = store.open_reader("snap::t::s::csv.0002")
        with reader:
            assert reader.read() == b"a\r\nb\r\n"
        assert isinstance(reader, io.BytesIO)

    def test_open_reader_missing_doc_raises_missing(self):
        store, _ = _store(_settings())
        with pytest.raises(SnapshotObjectMissingError):
            store.open_reader("snap::t::nope::csv.0001")


# ---------------------------------------------------------------------------
# delete_prefix cleanup
# ---------------------------------------------------------------------------


class TestDeletePrefix:

    def test_delete_prefix_removes_exactly_the_snapshot_docs(self):
        store, col = _store(_settings())
        sideways = "snap::OTHER::s::meta"
        for key in (
            "snap::t::s::meta",
            "snap::t::s::rows.0000",
            "snap::t::s::csv.0001",
            sideways,
        ):
            col.docs[key] = b"x"
        store.delete_prefix("snap::t::s::")
        assert set(col.docs) == {sideways}
        assert set(col.removed) == {
            "snap::t::s::meta",
            "snap::t::s::rows.0000",
            "snap::t::s::csv.0001",
        }

    def test_delete_prefix_empty_is_noop(self):
        store, col = _store(_settings())
        col.docs["snap::t::other::meta"] = b"x"
        store.delete_prefix("snap::t::s::")
        assert col.removed == []
        assert set(col.docs) == {"snap::t::other::meta"}
