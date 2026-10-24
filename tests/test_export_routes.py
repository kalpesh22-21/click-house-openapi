"""Route-level tests for app/routers/export.py.

A minimal FastAPI app carries ONLY the export router (the real /query stack is
unrelated to these assertions); the service layer is patched at the router's
svc_* names.  Asserts the wire contract: 201 create, 200 page/download, the
{"detail": {"error", "code"}} error envelope, 422 query-param validation, 401
with no bearer token — plus app.main conditional registration (only when
EXPORT_ENABLED) and the lifespan's fail-loud incomplete-S3 gate.
"""

from __future__ import annotations

import contextlib
import importlib
import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.errors import (
    ClickHouseQueryError,
    ExportTooLargeError,
    QueryValidationError,
    SnapshotExpiredError,
    SnapshotNotFoundError,
)
from app.routers import export as export_router
from tests.jwt_helpers import make_jwt

AUTH = {"Authorization": f"Bearer {make_jwt()}"}


@pytest.fixture(autouse=True)
def _export_test_env():
    """Hermetic env: no repo-root .env, and the employee-access default (enabled
    without EEACCESS_BASE_URL) must not break Settings construction here."""
    with patch.dict(
        os.environ,
        {"EMPLOYEE_ACCESS_ENABLED": "false", "RESTRICT_TO_CATALOGUED_TABLES": "false"},
    ):
        get_settings.cache_clear()
        # These are router-envelope tests; session-token auth is covered by
        # test_session_catalog and the live snapshot integration test.
        from dataclasses import replace
        from app.catalog_scope import CatalogScope
        with patch("app.session_catalog.verify_scope", side_effect=lambda p, *args: replace(p, catalog_scope=CatalogScope(columns=frozenset()))):
            yield
        get_settings.cache_clear()

_CREATE_BODY = {
    "snapshot_id": "11111111-2222-3333-4444-555555555555",
    "columns": ["id", "name"],
    "row_count": 3,
    "created_at": "2026-10-22T13:00:00Z",
    "expires_at": "2026-10-22T17:00:00Z",
}
_PAGE_BODY = {
    "columns": ["id", "name"],
    "rows": [[1, "a"], [2, "b"]],
    "limit": 100,
    "offset": 0,
    "has_more": True,
    "row_count": 3,
    "snapshot_id": "11111111-2222-3333-4444-555555555555",
    "expires_at": "2026-10-22T17:00:00Z",
}
_DOWNLOAD_BODY = {
    "files": [
        {
            "part": 1,
            "url": "https://s3.test/b/k?sig=x",
            "rows": 3,
            "filename": "query-export-part-0001.csv",
        }
    ],
    "expires_in": 300,
}
_DOWNLOAD_BODY_CB = {
    "files": [
        {
            "part": 1,
            "url": "query/snapshots/11111111-2222-3333-4444-555555555555/download?part=1",
            "rows": 3,
            "filename": "query-export-part-0001.csv",
        }
    ],
    "expires_in": None,
}


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(export_router.router, prefix="/query")
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

class TestHappyPaths:

    def test_create_returns_201_with_shape(self, client):
        with patch.object(export_router, "svc_create_snapshot", return_value=dict(_CREATE_BODY)) as svc:
            resp = client.post("/query/snapshots", headers=AUTH, json={"sql": "SELECT 1"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body == _CREATE_BODY
        # Timestamps are ISO UTC 'Z' strings as serialized.
        assert body["created_at"].endswith("Z")
        assert svc.call_args.kwargs.get("settings") is not None or svc.call_args.args[1] is not None

    def test_page_returns_200_with_superset_shape(self, client):
        sid = _CREATE_BODY["snapshot_id"]
        with patch.object(export_router, "svc_page_snapshot", return_value=dict(_PAGE_BODY)) as svc:
            resp = client.get(f"/query/snapshots/{sid}/page?offset=100&limit=500", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json() == _PAGE_BODY
        # Query params flow to the service as (snapshot_id, offset, limit, settings).
        args = svc.call_args
        assert args.args[0] == sid
        assert args.args[1] == 100
        assert args.args[2] == 500

    def test_download_urls_returns_200(self, client):
        sid = _CREATE_BODY["snapshot_id"]
        with patch.object(export_router, "svc_download_urls", return_value=dict(_DOWNLOAD_BODY)):
            resp = client.get(f"/query/snapshots/{sid}/download-urls", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json() == _DOWNLOAD_BODY

    def test_download_urls_couchbase_mode_shape(self, client):
        """CB mode: relative app-path urls, expires_in serialized as null."""
        sid = _CREATE_BODY["snapshot_id"]
        with patch.object(export_router, "svc_download_urls", return_value=dict(_DOWNLOAD_BODY_CB)):
            resp = client.get(f"/query/snapshots/{sid}/download-urls", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body == _DOWNLOAD_BODY_CB
        assert "expires_in" in body and body["expires_in"] is None

    def test_download_part_streams_csv_with_attachment_headers(self, client):
        sid = _CREATE_BODY["snapshot_id"]
        body = b"id,name\r\n1,a\r\n2,b\r\n"
        with patch.object(
            export_router,
            "svc_download_part",
            return_value=(iter([body]), "query-export-part-0001.csv"),
        ) as svc:
            resp = client.get(f"/query/snapshots/{sid}/download?part=1", headers=AUTH)
        assert resp.status_code == 200
        # Starlette appends a charset to text/* media types.
        assert resp.headers["content-type"].startswith("text/csv")
        assert resp.headers["content-disposition"] == (
            'attachment; filename="query-export-part-0001.csv"'
        )
        assert resp.content == body
        args = svc.call_args.args
        assert args[0] == sid and args[1] == 1

    def test_download_part_maps_404_and_410(self, client):
        sid = _CREATE_BODY["snapshot_id"]
        with patch.object(
            export_router,
            "svc_download_part",
            side_effect=SnapshotNotFoundError(message="Snapshot not found.", code="SNAPSHOT_NOT_FOUND"),
        ):
            resp = client.get(f"/query/snapshots/{sid}/download?part=99", headers=AUTH)
        assert resp.status_code == 404
        assert resp.json() == {"detail": {"error": "Snapshot not found.", "code": "SNAPSHOT_NOT_FOUND"}}

        with patch.object(
            export_router,
            "svc_download_part",
            side_effect=SnapshotExpiredError(
                message="This snapshot has expired. Refresh the data to mint a new one.",
                code="SNAPSHOT_EXPIRED",
            ),
        ):
            resp = client.get(f"/query/snapshots/{sid}/download?part=1", headers=AUTH)
        assert resp.status_code == 410
        assert resp.json()["detail"]["code"] == "SNAPSHOT_EXPIRED"

    def test_download_part_requires_bearer_and_int_part(self, client):
        sid = _CREATE_BODY["snapshot_id"]
        assert client.get(f"/query/snapshots/{sid}/download?part=1").status_code == 401
        with patch.object(
            export_router, "svc_download_part", return_value=(iter([b"x"]), "p.csv")
        ):
            resp = client.get(f"/query/snapshots/{sid}/download?part=abc", headers=AUTH)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Error envelope + status mapping
# ---------------------------------------------------------------------------

class TestErrorEnvelope:

    def test_export_too_large_maps_400(self, client):
        with patch.object(
            export_router,
            "svc_create_snapshot",
            side_effect=ExportTooLargeError(
                message="This export exceeds the maximum snapshot size.", code="EXPORT_TOO_LARGE"
            ),
        ):
            resp = client.post("/query/snapshots", headers=AUTH, json={"sql": "SELECT 1"})
        assert resp.status_code == 400
        assert resp.json() == {
            "detail": {"error": "This export exceeds the maximum snapshot size.", "code": "EXPORT_TOO_LARGE"}
        }

    def test_query_validation_maps_400(self, client):
        with patch.object(
            export_router,
            "svc_create_snapshot",
            side_effect=QueryValidationError(message="Denied.", code="DISALLOWED_KEYWORD"),
        ):
            resp = client.post("/query/snapshots", headers=AUTH, json={"sql": "SELECT s3(1)"})
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "DISALLOWED_KEYWORD"

    def test_clickhouse_query_error_maps_400(self, client):
        with patch.object(
            export_router,
            "svc_create_snapshot",
            side_effect=ClickHouseQueryError(message="nope", code="CLICKHOUSE_QUERY_ERROR"),
        ):
            resp = client.post("/query/snapshots", headers=AUTH, json={"sql": "SELECT 1"})
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "CLICKHOUSE_QUERY_ERROR"

    def test_not_found_maps_404_identical_envelope(self, client):
        sid = _CREATE_BODY["snapshot_id"]
        with patch.object(
            export_router,
            "svc_page_snapshot",
            side_effect=SnapshotNotFoundError(message="Snapshot not found.", code="SNAPSHOT_NOT_FOUND"),
        ):
            page_resp = client.get(f"/query/snapshots/{sid}/page", headers=AUTH)
        with patch.object(
            export_router,
            "svc_download_urls",
            side_effect=SnapshotNotFoundError(message="Snapshot not found.", code="SNAPSHOT_NOT_FOUND"),
        ):
            dl_resp = client.get(f"/query/snapshots/{sid}/download-urls", headers=AUTH)
        for resp in (page_resp, dl_resp):
            assert resp.status_code == 404
            assert resp.json() == {
                "detail": {"error": "Snapshot not found.", "code": "SNAPSHOT_NOT_FOUND"}
            }

    def test_expired_maps_410(self, client):
        sid = _CREATE_BODY["snapshot_id"]
        with patch.object(
            export_router,
            "svc_page_snapshot",
            side_effect=SnapshotExpiredError(
                message="This snapshot has expired. Refresh the data to mint a new one.",
                code="SNAPSHOT_EXPIRED",
            ),
        ):
            resp = client.get(f"/query/snapshots/{sid}/page", headers=AUTH)
        assert resp.status_code == 410
        assert resp.json()["detail"]["code"] == "SNAPSHOT_EXPIRED"


# ---------------------------------------------------------------------------
# Validation / auth
# ---------------------------------------------------------------------------

class TestValidationAndAuth:

    def test_limit_bounds_enforced_by_fastapi_422(self, client):
        sid = _CREATE_BODY["snapshot_id"]
        for bad in ("limit=0", "limit=1001"):
            with patch.object(export_router, "svc_page_snapshot", return_value=dict(_PAGE_BODY)):
                resp = client.get(f"/query/snapshots/{sid}/page?{bad}", headers=AUTH)
            assert resp.status_code == 422, bad

    def test_negative_offset_rejected_422(self, client):
        sid = _CREATE_BODY["snapshot_id"]
        resp = client.get(f"/query/snapshots/{sid}/page?offset=-1", headers=AUTH)
        assert resp.status_code == 422

    def test_page_defaults_applied(self, client):
        sid = _CREATE_BODY["snapshot_id"]
        with patch.object(export_router, "svc_page_snapshot", return_value=dict(_PAGE_BODY)) as svc:
            resp = client.get(f"/query/snapshots/{sid}/page", headers=AUTH)
        assert resp.status_code == 200
        args = svc.call_args.args
        assert args[1] == 0    # default offset
        assert args[2] == 100  # default limit

    def test_requires_bearer_token(self, client):
        resp = client.post("/query/snapshots", json={"sql": "SELECT 1"})
        assert resp.status_code == 401
        resp = client.get(f"/query/snapshots/{_CREATE_BODY['snapshot_id']}/page")
        assert resp.status_code == 401

    def test_missing_sql_rejected_422(self, client):
        resp = client.post("/query/snapshots", headers=AUTH, json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# app.main conditional registration + lifespan fail-loud gate
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _reloaded_main(**env: str):
    """Hold *env* while reloading app.main AND for the whole with-block.

    Route registration is evaluated at app.main import time from get_settings(),
    and the lifespan re-reads settings when the TestClient enters — both must
    see the pinned env, so the patch spans the reload and the requests.
    """
    with patch.dict(os.environ, env):
        get_settings.cache_clear()
        import app.main as main_mod

        try:
            yield importlib.reload(main_mod)
        finally:
            get_settings.cache_clear()


@pytest.fixture()
def _restore_main():
    yield
    # Leave app.main in the default (export-disabled) state for other modules.
    get_settings.cache_clear()
    import app.main as main_mod

    importlib.reload(main_mod)
    get_settings.cache_clear()


class TestMainRegistration:

    def test_routes_registered_only_when_export_enabled(self, _restore_main):
        with _reloaded_main(EXPORT_ENABLED="false") as main_mod:
            paths = {getattr(r, "path", "") for r in main_mod.app.routes}
            assert "/query/snapshots" not in paths

        with _reloaded_main(
            EXPORT_ENABLED="true",
            EXPORT_STORE="s3",
            EXPORT_S3_ENDPOINT="https://s3.example.com",
            EXPORT_S3_BUCKET="b",
            EXPORT_S3_ACCESS_KEY="k",
            EXPORT_S3_SECRET_KEY="s",
        ) as main_mod:
            paths = {getattr(r, "path", "") for r in main_mod.app.routes}
        assert "/query/snapshots" in paths
        assert "/query/snapshots/{snapshot_id}/page" in paths
        assert "/query/snapshots/{snapshot_id}/download-urls" in paths
        assert "/query/snapshots/{snapshot_id}/download" in paths

    def test_lifespan_refuses_to_boot_with_incomplete_s3(self, _restore_main):
        with _reloaded_main(  # s3 store selected, S3 identity missing
            EXPORT_ENABLED="true", EXPORT_STORE="s3"
        ) as main_mod:
            with patch("app.clickhouse_client.get_client", return_value=object()):

                async def _enter_lifespan():
                    async with main_mod.lifespan(main_mod.app):
                        pass

                with pytest.raises(SystemExit):
                    import anyio

                    anyio.run(_enter_lifespan)

    def test_lifespan_refuses_to_boot_with_incomplete_couchbase(self, _restore_main):
        # Default store mode is couchbase: EXPORT_ENABLED=true with no CB
        # identity must refuse to boot — partial S3 fields are irrelevant.
        with _reloaded_main(
            EXPORT_ENABLED="true",
            EXPORT_CB_CONN_STR="couchbase://cb.example.com",  # partial CB only
        ) as main_mod:
            with patch("app.clickhouse_client.get_client", return_value=object()):

                async def _enter_lifespan():
                    async with main_mod.lifespan(main_mod.app):
                        pass

                with pytest.raises(SystemExit):
                    import anyio

                    anyio.run(_enter_lifespan)

    def test_lifespan_boots_with_complete_s3(self, _restore_main):
        with _reloaded_main(
            EXPORT_ENABLED="true",
            EXPORT_STORE="s3",
            EXPORT_S3_ENDPOINT="https://s3.example.com",
            EXPORT_S3_BUCKET="b",
            EXPORT_S3_ACCESS_KEY="k",
            EXPORT_S3_SECRET_KEY="s",
        ) as main_mod:
            with patch("app.clickhouse_client.get_client", return_value=object()):
                with TestClient(main_mod.app) as client:
                    # Auth works end-to-end on a registered export route (the service
                    # layer is patched; the store singleton is never touched).
                    with patch.object(
                        export_router, "svc_create_snapshot", return_value=dict(_CREATE_BODY)
                    ):
                        resp = client.post("/query/snapshots", headers=AUTH, json={"sql": "SELECT 1"})
        assert resp.status_code == 201, resp.text

    def test_lifespan_boots_with_complete_couchbase(self, _restore_main):
        with _reloaded_main(
            EXPORT_ENABLED="true",
            EXPORT_CB_CONN_STR="couchbase://cb.example.com",
            EXPORT_CB_BUCKET="agent_snapshots",
            EXPORT_CB_USERNAME="u",
            EXPORT_CB_PASSWORD="p",
        ) as main_mod:
            with patch("app.clickhouse_client.get_client", return_value=object()):
                with TestClient(main_mod.app) as client:
                    with patch.object(
                        export_router, "svc_create_snapshot", return_value=dict(_CREATE_BODY)
                    ):
                        resp = client.post("/query/snapshots", headers=AUTH, json={"sql": "SELECT 1"})
        assert resp.status_code == 201, resp.text


def test_mcp_snapshot_routes_enabled_and_auth_required(monkeypatch):
    import app.mcp_server as server
    settings = get_settings().model_copy(update={'export_enabled': True})
    monkeypatch.setattr(server, 'settings', settings)
    api = server.JWTAuthMiddleware(server.create_http_app(), settings=settings)
    with patch.object(export_router, 'svc_create_snapshot', return_value=dict(_CREATE_BODY)):
        # No lifespan: this isolates routing/auth from the single-use MCP manager.
        client = TestClient(api)
        assert client.post('/query/snapshots', json={'sql':'SELECT 1'}).status_code == 401
        response = client.post('/query/snapshots', headers=AUTH, json={'sql':'SELECT 1'})
        assert response.status_code == 201, response.text


def test_busy_creations_do_not_block_paging_and_release_slots(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock

    monkeypatch.setenv('EXPORT_MAX_CONCURRENT_CREATES', '2')
    get_settings.cache_clear()
    export_router._creation_slots.cache_clear()
    started = Event(); release = Event(); lock = Lock(); active = 0

    def slow_create(*args):
        nonlocal active
        with lock:
            active += 1
            if active == 2:
                started.set()
        assert release.wait(5)
        return dict(_CREATE_BODY)

    with patch.object(export_router, 'svc_create_snapshot', side_effect=slow_create) as create:
        with patch.object(export_router, 'svc_page_snapshot', return_value=dict(_PAGE_BODY)):
            with ThreadPoolExecutor(2) as workers:
                running = [workers.submit(client.post, '/query/snapshots', headers=AUTH,
                                          json={'sql':'SELECT 1'}) for _ in range(2)]
                try:
                    assert started.wait(5)
                    rejected = client.post('/query/snapshots', headers=AUTH, json={'sql':'SELECT 1'})
                    assert rejected.status_code == 429
                    assert rejected.json()['detail']['code'] == 'EXPORT_BUSY'
                    assert rejected.headers['retry-after'] == '1'
                    assert client.get('/query/snapshots/'+_CREATE_BODY['snapshot_id']+'/page', headers=AUTH).status_code == 200
                    assert create.call_count == 2
                finally:
                    release.set()
                assert all(f.result().status_code == 201 for f in running)
            assert client.post('/query/snapshots', headers=AUTH, json={'sql':'SELECT 1'}).status_code == 201
    export_router._creation_slots.cache_clear()


def test_automatic_idempotency_needs_no_header_and_pending_is_retryable(client):
    from app.snapshot_idempotency import SnapshotIdempotencyError
    with patch.object(export_router, 'svc_create_snapshot', return_value=dict(_CREATE_BODY)) as svc:
        response=client.post('/query/snapshots',headers=AUTH,json={'sql':'SELECT 1'})
        assert response.status_code==201
        assert svc.call_args.kwargs=={}
        assert 'Idempotency-Key' not in str(client.get('/openapi.json').json())
    for code in ['SNAPSHOT_IN_PROGRESS','SNAPSHOT_CREATION_FAILED']:
        with patch.object(export_router,'svc_create_snapshot',side_effect=SnapshotIdempotencyError('test',code)):
            response=client.post('/query/snapshots',headers=AUTH,json={'sql':'SELECT 1'})
            assert response.status_code==409
            assert response.json()['detail']['code']==code
            assert ('retry-after' in response.headers)==(code=='SNAPSHOT_IN_PROGRESS')


def test_refresh_reference_forwarded(client):
    with patch.object(export_router, 'svc_create_snapshot', return_value=dict(_CREATE_BODY)) as svc:
        response = client.post('/query/snapshots', headers=AUTH,
                               json={'sql': 'SELECT 1', 'refresh_of': _CREATE_BODY['snapshot_id']})
        assert response.status_code == 201
        assert svc.call_args.kwargs == {'refresh_of': _CREATE_BODY['snapshot_id']}
    assert client.post('/query/snapshots', headers=AUTH,
                       json={'sql': 'SELECT 1', 'refresh_of': '../foreign'}).status_code == 422


def test_scope_change_error_and_rebuild_route(client):
    from app.errors import SnapshotScopeChangedError
    sid = _CREATE_BODY['snapshot_id']
    with patch.object(export_router, 'svc_page_snapshot', side_effect=SnapshotScopeChangedError(
            'Access changed.', 'SNAPSHOT_SCOPE_CHANGED')):
        response = client.get(f'/query/snapshots/{sid}/page', headers=AUTH)
        assert response.status_code == 409
        assert response.json()['detail']['code'] == 'SNAPSHOT_SCOPE_CHANGED'
    with patch.object(export_router, 'svc_rebuild_snapshot', return_value=dict(_CREATE_BODY)) as rebuild:
        response = client.post(f'/query/snapshots/{sid}/rebuild', headers=AUTH)
        assert response.status_code == 201
        assert rebuild.call_args.args[0] == sid
