"""Tests for app/export_service.py — answer-snapshot mint/page/download.

S3 is an in-memory FakeSnapshotStore (never boto3/network); the ClickHouse
stream is stubbed at the app.export_service.execute_query_rows_stream seam.
Covers: mint (wire-value fidelity, NUL cleaning, object layout, meta-LAST
commit), part-split math (rows and bytes), local total-budget aborts
(EXPORT_TOO_LARGE with cleanup), mid-stream ClickHouse failure cleanup,
guardrail propagation, tenant-hash indistinguishability (404), expiry (410),
exact paging math, and presign plumbing.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import re
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.errors import (
    ClickHouseQueryError,
    ExportTooLargeError,
    QueryValidationError,
    SnapshotExpiredError,
    SnapshotNotFoundError,
)
from app.export_client import SnapshotObjectMissingError
from app.export_service import (
    _tenant_hash,
    _create_snapshot as create_snapshot,
    download_urls,
    page_snapshot,
)
from app.principal import Principal, current_principal

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSnapshotStore:
    """In-memory stand-in for S3SnapshotStore — records every interaction."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: list[str] = []          # upload order (commit-point check)
        self.deleted_prefixes: list[str] = []
        self.presign_calls: list[dict] = []

    def put_file(self, local_path: str, key: str) -> None:
        with open(local_path, "rb") as fh:
            self.objects[key] = fh.read()
        self.uploads.append(key)

    def read_bytes(self, key: str) -> bytes:
        if key not in self.objects:
            raise SnapshotObjectMissingError(key)
        return self.objects[key]

    def open_reader(self, key: str):
        if key not in self.objects:
            raise SnapshotObjectMissingError(key)
        return io.BytesIO(self.objects[key])

    def delete_prefix(self, prefix: str) -> None:
        self.deleted_prefixes.append(prefix)
        for key in [k for k in self.objects if k.startswith(prefix)]:
            del self.objects[key]

    def presign_get(self, key: str, *, filename: str, expires_in: int) -> str:
        self.presign_calls.append({"key": key, "filename": filename, "expires_in": expires_in})
        return f"https://s3.test/b/{key}?sig=fake&expires={expires_in}"


class _FakeChStream:
    """Stand-in for QueryRowsStream at the execute_query_rows_stream seam."""

    def __init__(self, columns, type_names, rows, on_iter: Any = None):
        self.column_names = columns
        self.column_type_names = type_names
        self._rows = rows
        self._on_iter = on_iter
        self.sql_seen: str | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def __iter__(self) -> Iterator[list[Any]]:
        if self._on_iter is not None:
            return self._on_iter()
        return iter(self._rows)


def _exploding_iter(rows, exc):
    yield from rows
    raise exc


@contextlib.contextmanager
def _stub_stream(columns, type_names, rows, capture: dict | None = None, on_iter=None):
    """Patch app.export_service.execute_query_rows_stream to a canned stream."""

    def _fake(sql, settings, parameters=None, *, caps):
        if capture is not None:
            capture["sql"] = sql
            capture["caps"] = caps
        return _FakeChStream(columns, type_names, rows, on_iter=on_iter)

    with patch("app.export_service.execute_query_rows_stream", _fake):
        yield


# ---------------------------------------------------------------------------
# Settings / principal helpers
# ---------------------------------------------------------------------------


def _settings(**overrides) -> Settings:
    base: dict[str, Any] = {
        "clickhouse_host": "localhost",
        "clickhouse_user": "default",
        "employee_access_enabled": False,
        "restrict_to_catalogued_tables": False,
        "export_enabled": True,
        "export_store": "s3",  # this suite exercises the S3 adapter/layout
        "export_s3_endpoint": "https://s3.example.com",
        "export_s3_bucket": "snapshot-bucket",
        "export_s3_access_key": "ak",
        "export_s3_secret_key": "sk",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _principal(client_code="CLIENT_A", proc_center="PC01", jti="TESTJTI001") -> Principal:
    return Principal(
        subject="sub-1",
        claims={"clientcode": client_code, "proc_center": proc_center, "jti": jti},
    )


@contextlib.contextmanager
def _bound_principal(**claims):
    token = current_principal.set(_principal(**claims))
    try:
        yield
    finally:
        current_principal.reset(token)


def _sorted_thash(settings: Settings) -> str:
    """Tenant hash for the test default principal (binds one when none is bound).

    Object-layout helpers run OUTSIDE _bound_principal blocks; under production
    REST a principal is always bound by require_principal, so binding the
    default test principal here mirrors the production mint identity.
    """
    from app.export_service import _snapshot_owner_identity

    if current_principal.get() is None:
        with _bound_principal():
            return _tenant_hash(_snapshot_owner_identity(settings))
    return _tenant_hash(_snapshot_owner_identity(settings))


def _objects_for(store: FakeSnapshotStore, settings: Settings, snapshot_id: str) -> dict[str, bytes]:
    prefix = f"{settings.export_s3_prefix}/{_sorted_thash(settings)}/{snapshot_id}"
    return {k[len(prefix) + 1:]: v for k, v in store.objects.items() if k.startswith(prefix + "/")}


def _read_jsonl(store: FakeSnapshotStore, settings: Settings, snapshot_id: str) -> list[list[Any]]:
    objs = _objects_for(store, settings, snapshot_id)
    with gzip.GzipFile(fileobj=io.BytesIO(objs["rows.jsonl.gz"]), mode="rb") as gz:
        return [json.loads(line) for line in gz.read().decode("utf-8").splitlines() if line]


def _read_meta(store: FakeSnapshotStore, settings: Settings, snapshot_id: str) -> dict:
    return json.loads(_objects_for(store, settings, snapshot_id)["meta.json"].decode("utf-8"))


# ---------------------------------------------------------------------------
# create_snapshot — mint
# ---------------------------------------------------------------------------

class TestCreateSnapshot:

    def test_happy_path_wire_fidelity_and_layout(self):
        settings = _settings()
        store = FakeSnapshotStore()
        rows = [
            [1, "Alice\x00\x00", None],
            [2, "café", {"nested": ["pad\x00\x00", 2]}],
        ]
        with _bound_principal():
            with _stub_stream(
                ["id", "name", "extra"], ["UInt8", "String", "Nullable(String)"], rows
            ) as _:
                result = create_snapshot("SELECT id, name, extra FROM t", settings, store=store)

            sid = result["snapshot_id"]
            assert result["columns"] == ["id", "name", "extra"]  # names only on the wire
            assert result["row_count"] == 2
            uuid.UUID(sid)  # server-minted UUID
            created = datetime.fromisoformat(result["created_at"].replace("Z", "+00:00"))
            expires = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
            assert (expires - created) == timedelta(seconds=settings.export_ttl_seconds)
            assert result["created_at"].endswith("Z")

            objs = _objects_for(store, settings, sid)
            assert set(objs) == {"rows.jsonl.gz", "data.part-0001.csv", "meta.json"}

            # meta.json is uploaded LAST (commit point).
            assert store.uploads[-1].endswith("/meta.json")

            # Wire values: strings NUL-cleaned (padding only), None → null.
            assert _read_jsonl(store, settings, sid) == [
                [1, "Alice", None],
                [2, "café", {"nested": ["pad", 2]}],
            ]

            # CSV part: header + both rows, canonical values.
            csv_text = objs["data.part-0001.csv"].decode("utf-8")
            assert csv_text.splitlines() == [
                "id,name,extra",
                "1,Alice,",
                '2,café,"{""nested"":[""pad"",2]}"',
            ]

            meta = _read_meta(store, settings, sid)
            assert meta["snapshot_id"] == sid
            assert meta["tenant_hash"] == _sorted_thash(settings)
            assert meta["columns"] == [
                {"name": "id", "type": "UInt8"},
                {"name": "name", "type": "String"},
                {"name": "extra", "type": "Nullable(String)"},
            ]
            assert meta["row_count"] == 2
            assert [p["rows"] for p in meta["parts"]] == [2]
            assert meta["parts"][0]["key"].endswith("/data.part-0001.csv")
            # SQL is private metadata for reauthorization/rebuild, never a response field.
            expected_clean = "SELECT id, name, extra FROM t"  # (no LIMIT injected)
            assert meta["sql_sha256"] == hashlib.sha256(expected_clean.encode()).hexdigest()
            assert meta["source_sql"] == expected_clean
            assert "source_sql" not in result
            for key, blob in objs.items():
                if key != "meta.json":
                    assert b"FROM t" not in blob

    def test_datetime_and_decimal_encode_like_rest_query(self):
        settings = _settings()
        store = FakeSnapshotStore()
        from datetime import date
        from datetime import datetime as dt
        from decimal import Decimal

        rows = [[dt(2026, 10, 22, 13, 0, 5), date(2026, 10, 22), Decimal("12.50")]]
        with _bound_principal():
            with _stub_stream(["ts", "d", "amt"], ["DateTime", "Date", "Decimal(10,2)"], rows):
                result = create_snapshot("SELECT ts, d, amt FROM t", settings, store=store)

        # Match /query's Pydantic Decimal wire representation without precision loss.
        assert _read_jsonl(store, settings, result["snapshot_id"]) == [
            ["2026-10-22T13:00:05", "2026-10-22", "12.50"]
        ]
        csv_lines = _objects_for(store, settings, result["snapshot_id"])[
            "data.part-0001.csv"
        ].decode("utf-8").splitlines()
        assert csv_lines[1] == "2026-10-22T13:00:05,2026-10-22,12.50"

    def test_no_default_limit_injected_on_export_sql(self):
        settings = _settings()
        store = FakeSnapshotStore()
        capture: dict = {}
        with _bound_principal():
            with _stub_stream(["x"], ["UInt8"], [[1]], capture=capture):
                create_snapshot("SELECT x FROM t", settings, store=store)
        assert capture["sql"] == "SELECT x FROM t"  # no "LIMIT 1000" appended

    def test_export_caps_flow_to_stream(self):
        settings = _settings(export_max_execution_time=90, export_total_max_rows=500)
        store = FakeSnapshotStore()
        capture: dict = {}
        with _bound_principal():
            with _stub_stream(["x"], ["UInt8"], [[1]], capture=capture):
                create_snapshot("SELECT x FROM t", settings, store=store)
        assert capture["caps"]["max_execution_time"] == 90
        assert capture["caps"]["max_result_rows"] == 501
        assert capture["caps"]["result_overflow_mode"] == "throw"

    def test_guardrail_rejection_propagates_and_streams_nothing(self):
        settings = _settings()
        store = FakeSnapshotStore()

        def _boom(*args, **kwargs):  # pragma: no cover - must never be called
            raise AssertionError("stream must not run for invalid SQL")

        with _bound_principal():
            with patch("app.export_service.execute_query_rows_stream", _boom):
                with pytest.raises(QueryValidationError) as exc_info:
                    create_snapshot("DELETE FROM t", settings, store=store)
        assert exc_info.value.code == "DISALLOWED_STATEMENT_TYPE"
        assert store.objects == {}

    def test_part_split_by_row_count(self):
        settings = _settings(export_max_rows=3, export_part_max_bytes=10**6)
        store = FakeSnapshotStore()
        rows = [[i, f"name-{i}"] for i in range(7)]
        with _bound_principal():
            with _stub_stream(["id", "name"], ["UInt8", "String"], rows):
                result = create_snapshot("SELECT id, name FROM t", settings, store=store)

        objs = _objects_for(store, settings, result["snapshot_id"])
        part_names = sorted(k for k in objs if k.startswith("data.part-"))
        assert part_names == ["data.part-0001.csv", "data.part-0002.csv", "data.part-0003.csv"]
        meta = _read_meta(store, settings, result["snapshot_id"])
        assert [p["rows"] for p in meta["parts"]] == [3, 3, 1]
        # Every part is a standalone CSV: header row first.
        for name in part_names:
            lines = objs[name].decode("utf-8").splitlines()
            assert lines[0] == "id,name"
        # All data rows present, in order, across parts.
        data_lines = [
            line
            for name in part_names
            for line in objs[name].decode("utf-8").splitlines()[1:]
        ]
        assert data_lines == [f"{i},name-{i}" for i in range(7)]

    def test_part_split_by_part_bytes(self):
        # Header "id,name\r\n" is 9 bytes; each row "N,name-N\r\n" is 11 bytes
        # (1-digit ids) → cap 31 bytes => header + 2 rows per part.
        settings = _settings(export_max_rows=10**6, export_part_max_bytes=31)
        store = FakeSnapshotStore()
        rows = [[i, f"name-{i}"] for i in range(5)]
        with _bound_principal():
            with _stub_stream(["id", "name"], ["UInt8", "String"], rows):
                result = create_snapshot("SELECT id, name FROM t", settings, store=store)

        meta = _read_meta(store, settings, result["snapshot_id"])
        assert [p["rows"] for p in meta["parts"]] == [2, 2, 1]
        for p in meta["parts"]:
            assert p["bytes"] <= 31 + 11  # cap + at most one overshooting row

    def test_total_row_budget_aborts_cleanly(self):
        settings = _settings(export_total_max_rows=2)
        store = FakeSnapshotStore()
        rows = [[i] for i in range(5)]
        with _bound_principal(), _stub_stream(["id"], ["UInt8"], rows):
            with pytest.raises(ExportTooLargeError) as exc_info:
                create_snapshot("SELECT id FROM t", settings, store=store)
        assert exc_info.value.code == "EXPORT_TOO_LARGE"
        assert "WHERE" in exc_info.value.message  # advises narrowing the query
        # Clean abort: no snapshot objects remain; best-effort prefix delete ran.
        assert store.objects == {}
        assert store.deleted_prefixes != []

    def test_total_byte_budget_aborts_cleanly(self):
        settings = _settings(export_total_max_bytes=64)
        store = FakeSnapshotStore()
        rows = [[i, "x" * 20] for i in range(10)]
        with _bound_principal():
            with _stub_stream(["id", "pad"], ["UInt8", "String"], rows):
                with pytest.raises(ExportTooLargeError):
                    create_snapshot("SELECT id, pad FROM t", settings, store=store)
        assert store.objects == {}

    def test_mid_stream_clickhouse_failure_cleans_up(self):
        settings = _settings()
        store = FakeSnapshotStore()
        ch_fail = HTTPException(
            status_code=400, detail={"error": "ClickHouse query error: boom", "code": "CLICKHOUSE_QUERY_ERROR"}
        )
        with _bound_principal():
            with _stub_stream(["id"], ["UInt8"], None, on_iter=lambda: _exploding_iter([[1]], ch_fail)):
                with pytest.raises(ClickHouseQueryError) as exc_info:
                    create_snapshot("SELECT id FROM t", settings, store=store)
        assert exc_info.value.code == "CLICKHOUSE_QUERY_ERROR"
        assert store.objects == {}
        assert store.deleted_prefixes != []

    def test_zero_row_snapshot_mints_with_empty_part_list(self):
        settings = _settings()
        store = FakeSnapshotStore()
        with _bound_principal(), _stub_stream(["id"], ["UInt8"], []):
            result = create_snapshot("SELECT id FROM t", settings, store=store)

        assert result["row_count"] == 0
        meta = _read_meta(store, settings, result["snapshot_id"])
        assert meta["row_count"] == 0
        assert meta["parts"] == []
        assert _read_jsonl(store, settings, result["snapshot_id"]) == []


# ---------------------------------------------------------------------------
# Fixtures producing a minted snapshot for page/download tests
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _minted(settings: Settings, store: FakeSnapshotStore, rows, columns=("id",), type_names=("UInt8",)):
    with _bound_principal(), _stub_stream(list(columns), list(type_names), rows):
        yield create_snapshot("SELECT id FROM t", settings, store=store)


class TestPageSnapshot:

    def test_exact_paging_boundaries(self):
        settings = _settings()
        store = FakeSnapshotStore()
        rows = [[i] for i in range(5)]
        with _minted(settings, store, rows) as created:
            sid = created["snapshot_id"]
            with _bound_principal():
                page0 = page_snapshot(sid, 0, 2, settings, store=store)
                page1 = page_snapshot(sid, 2, 2, settings, store=store)
                page2 = page_snapshot(sid, 4, 2, settings, store=store)

        assert page0["rows"] == [[0], [1]]
        assert page0["has_more"] is True   # 0 + 2 < 5
        assert page1["rows"] == [[2], [3]]
        assert page1["has_more"] is True   # 2 + 2 < 5
        assert page2["rows"] == [[4]]
        assert page2["has_more"] is False  # 4 + 1 == 5
        for page in (page0, page1, page2):
            assert page["columns"] == ["id"]
            assert page["row_count"] == 5
            assert page["snapshot_id"] == sid
            assert page["expires_at"].endswith("Z")
            assert page["offset"] in (0, 2, 4)
            assert page["limit"] == 2

    def test_offset_past_end_returns_empty_not_error(self):
        settings = _settings()
        store = FakeSnapshotStore()
        with _minted(settings, store, [[1], [2]]) as created:
            sid = created["snapshot_id"]
            with _bound_principal():
                page = page_snapshot(sid, 5, 100, settings, store=store)
        assert page["rows"] == []
        assert page["has_more"] is False
        assert page["row_count"] == 2

    def test_unknown_id_is_404_not_found(self):
        settings = _settings()
        store = FakeSnapshotStore()
        with _bound_principal():
            with pytest.raises(SnapshotNotFoundError) as exc_info:
                page_snapshot(str(uuid.uuid4()), 0, 100, settings, store=store)
        assert exc_info.value.code == "SNAPSHOT_NOT_FOUND"

    def test_malformed_id_is_404_not_found(self):
        settings = _settings()
        store = FakeSnapshotStore()
        with _bound_principal():
            for bad in ("../..", "not-a-uuid", "abc; DROP TABLE t", "../x" * 4):
                with pytest.raises(SnapshotNotFoundError):
                    page_snapshot(bad, 0, 100, settings, store=store)

    def test_foreign_tenant_is_indistinguishable_404(self):
        settings = _settings()
        store = FakeSnapshotStore()
        with _minted(settings, store, [[1]]) as created:
            sid = created["snapshot_id"]
            # Same service, same store, DIFFERENT tenant identity → 404, and the
            # message/code must be IDENTICAL to the unknown-id case.
            with _bound_principal(client_code="CLIENT_B"):
                with pytest.raises(SnapshotNotFoundError) as foreign:
                    page_snapshot(sid, 0, 100, settings, store=store)
                with pytest.raises(SnapshotNotFoundError) as unknown:
                    page_snapshot(str(uuid.uuid4()), 0, 100, settings, store=store)
        assert foreign.value.code == unknown.value.code == "SNAPSHOT_NOT_FOUND"
        assert foreign.value.message == unknown.value.message

    def test_expired_snapshot_is_410(self):
        settings = _settings()
        store = FakeSnapshotStore()
        with _minted(settings, store, [[1]]) as created:
            sid = created["snapshot_id"]
        # Tamper the stored expiry into the past (the app-side clock is the
        # TTL's enforcement point; the bucket lifecycle is only a backstop).
        thash = _sorted_thash(settings)
        meta_key = f"{settings.export_s3_prefix}/{thash}/{sid}/meta.json"
        meta = json.loads(store.objects[meta_key].decode("utf-8"))
        meta["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        store.objects[meta_key] = json.dumps(meta).encode("utf-8")

        with _bound_principal():
            with pytest.raises(SnapshotExpiredError) as exc_info:
                page_snapshot(sid, 0, 100, settings, store=store)
        assert exc_info.value.code == "SNAPSHOT_EXPIRED"

    def test_missing_payload_object_is_410_expired(self):
        # meta.json present but rows.jsonl.gz reaped ahead of the clock.
        settings = _settings()
        store = FakeSnapshotStore()
        with _minted(settings, store, [[1]]) as created:
            sid = created["snapshot_id"]
        thash = _sorted_thash(settings)
        del store.objects[f"{settings.export_s3_prefix}/{thash}/{sid}/rows.jsonl.gz"]
        with _bound_principal(), pytest.raises(SnapshotExpiredError):
            page_snapshot(sid, 0, 100, settings, store=store)


class TestDownloadUrls:

    def test_ordered_parts_with_presign_params(self):
        settings = _settings(export_max_rows=2, export_part_max_bytes=10**6)
        store = FakeSnapshotStore()
        rows = [[i] for i in range(5)]
        with _minted(settings, store, rows) as created:
            sid = created["snapshot_id"]
            with _bound_principal():
                result = download_urls(sid, settings, store=store)

        assert result["expires_in"] == settings.export_presign_ttl_seconds
        files = result["files"]
        assert [f["part"] for f in files] == [1, 2, 3]
        assert [f["rows"] for f in files] == [2, 2, 1]
        assert [f["filename"] for f in files] == [
            "query-export-part-0001.csv",
            "query-export-part-0002.csv",
            "query-export-part-0003.csv",
        ]
        for f in files:
            assert f["url"].startswith("https://s3.test/")
        # The presign plumbing received (key, filename, expires_in) exactly.
        assert [c["filename"] for c in store.presign_calls] == [f["filename"] for f in files]
        assert all(c["expires_in"] == 300 for c in store.presign_calls)
        assert all(re.search(r"/data\.part-\d{4}\.csv$", c["key"]) for c in store.presign_calls)

    def test_zero_row_snapshot_has_no_files(self):
        settings = _settings()
        store = FakeSnapshotStore()
        with _minted(settings, store, []) as created:
            sid = created["snapshot_id"]
            with _bound_principal():
                result = download_urls(sid, settings, store=store)
        assert result == {"files": [], "expires_in": settings.export_presign_ttl_seconds}

    def test_foreign_tenant_404_and_expired_410_on_download(self):
        settings = _settings()
        store = FakeSnapshotStore()
        with _minted(settings, store, [[1]]) as created:
            sid = created["snapshot_id"]
            with _bound_principal(client_code="CLIENT_B"):
                with pytest.raises(SnapshotNotFoundError):
                    download_urls(sid, settings, store=store)
        thash = _sorted_thash(settings)
        meta_key = f"{settings.export_s3_prefix}/{thash}/{sid}/meta.json"
        meta = json.loads(store.objects[meta_key].decode("utf-8"))
        meta["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        store.objects[meta_key] = json.dumps(meta).encode("utf-8")
        with _bound_principal(), pytest.raises(SnapshotExpiredError):
            download_urls(sid, settings, store=store)

    def test_custom_prefix_layout_respected(self):
        settings = _settings(export_s3_prefix="snap-root/")
        store = FakeSnapshotStore()
        with _minted(settings, store, [[1]]) as created:
            sid = created["snapshot_id"]
        assert all(k.startswith("snap-root/") for k in store.objects)
        with _bound_principal():
            result = download_urls(sid, settings, store=store)
        assert result["files"][0]["url"].startswith("https://s3.test/b/snap-root/")


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_snapshot_rejects_nonfinite_values(value):
    from app.export_service import _wire_row
    with pytest.raises(QueryValidationError, match="NaN or Infinity"):
        _wire_row([value])


def test_concurrent_first_use_constructs_one_store():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from time import sleep
    from app import export_service

    settings = _settings(export_store='couchbase')
    gate = Barrier(16)
    singleton = object()
    def build(*args):
        sleep(.02)
        return singleton
    def load():
        gate.wait()
        return export_service._resolve_store(settings, None)
    export_service._cached_store.cache_clear()
    try:
        with patch.object(export_service, 'get_settings', return_value=settings):
            with patch.object(export_service, 'CouchbaseSnapshotStore', side_effect=build) as constructor:
                with ThreadPoolExecutor(16) as workers:
                    results = list(workers.map(lambda _: load(), range(16)))
                assert all(result is singleton for result in results)
                constructor.assert_called_once()
    finally:
        export_service._cached_store.cache_clear()
