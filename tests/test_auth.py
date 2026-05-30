"""Tests for app/auth.py — Bearer token authentication dependency.

Uses FastAPI TestClient to exercise require_api_key through a real
request/response cycle.  The ClickHouse client is patched so no live
ClickHouse server is needed.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(api_key: str = "test-api-key-abc123") -> TestClient:
    """Build a TestClient with the ClickHouse client fully patched out."""
    # Ensure the settings cache reflects our test API key.
    os.environ["API_KEY"] = api_key
    get_settings.cache_clear()

    mock_ch = MagicMock()
    mock_ch.ping.return_value = True

    with patch("app.clickhouse_client.get_client", return_value=mock_ch):
        with patch("app.service.execute_query", return_value=(["col"], [["v"]])):
            from app.main import app
            # raise_server_exceptions=False so 401 is returned as a response, not raised.
            return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_API_KEY = "test-api-key-abc123"


@pytest.fixture(autouse=True)
def patch_clickhouse():
    """Patch ClickHouse for every test in this module."""
    mock_ch = MagicMock()
    mock_ch.ping.return_value = True
    with patch("app.clickhouse_client.get_client", return_value=mock_ch):
        with patch("app.service.execute_query", return_value=(["col"], [["v"]])):
            os.environ["API_KEY"] = VALID_API_KEY
            get_settings.cache_clear()
            yield
    get_settings.cache_clear()


@pytest.fixture()
def client():
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Valid token
# ---------------------------------------------------------------------------

class TestValidToken:

    def test_valid_bearer_token_passes(self, client):
        """A request with the correct Bearer token must return 200 (not 401)."""
        resp = client.post(
            "/query",
            headers={"Authorization": f"Bearer {VALID_API_KEY}"},
            json={"sql": "SELECT 1"},
        )
        # 200 means auth passed; the actual response code may be 200 or a CH error
        # depending on mocks, but it must NOT be 401.
        assert resp.status_code != 401, f"Valid token rejected: {resp.json()}"

    def test_valid_token_on_get_databases(self, client):
        """GET /databases with valid token passes auth layer."""
        resp = client.get(
            "/databases",
            headers={"Authorization": f"Bearer {VALID_API_KEY}"},
        )
        assert resp.status_code != 401

    def test_valid_token_on_get_tables(self, client):
        """GET /tables with valid token passes auth layer."""
        mock_result = MagicMock()
        mock_result.result_rows = [("default", "my_table", "MergeTree")]
        with patch("app.clickhouse_client.get_client") as mock_get:
            mock_get.return_value.query.return_value = mock_result
            resp = client.get(
                "/tables",
                headers={"Authorization": f"Bearer {VALID_API_KEY}"},
                params={"database": "default"},
            )
        assert resp.status_code != 401


# ---------------------------------------------------------------------------
# Missing token
# ---------------------------------------------------------------------------

class TestMissingToken:

    def test_no_auth_header_returns_401(self, client):
        """Request without Authorization header must return 401."""
        resp = client.post("/query", json={"sql": "SELECT 1"})
        assert resp.status_code == 401

    def test_no_auth_header_returns_missing_auth_code(self, client):
        resp = client.post("/query", json={"sql": "SELECT 1"})
        body = resp.json()
        assert body.get("detail", {}).get("code") == "MISSING_AUTH"

    def test_no_auth_header_on_databases_returns_401(self, client):
        resp = client.get("/databases")
        assert resp.status_code == 401

    def test_no_auth_header_on_tables_returns_401(self, client):
        resp = client.get("/tables", params={"database": "default"})
        assert resp.status_code == 401

    def test_no_auth_header_on_schema_returns_401(self, client):
        resp = client.get("/tables/schema", params={"database": "default", "table": "t"})
        assert resp.status_code == 401

    def test_no_auth_header_on_query_explain_returns_401(self, client):
        resp = client.post("/query/explain", json={"sql": "SELECT 1"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Wrong / malformed token
# ---------------------------------------------------------------------------

class TestWrongToken:

    def test_wrong_token_returns_401(self, client):
        """An incorrect Bearer token must return 401."""
        resp = client.post(
            "/query",
            headers={"Authorization": "Bearer totally-wrong-token"},
            json={"sql": "SELECT 1"},
        )
        assert resp.status_code == 401

    def test_wrong_token_returns_invalid_auth_code(self, client):
        resp = client.post(
            "/query",
            headers={"Authorization": "Bearer totally-wrong-token"},
            json={"sql": "SELECT 1"},
        )
        body = resp.json()
        assert body.get("detail", {}).get("code") == "INVALID_AUTH"

    def test_empty_token_returns_401(self, client):
        """Bearer with empty string token must be rejected."""
        resp = client.post(
            "/query",
            headers={"Authorization": "Bearer "},
            json={"sql": "SELECT 1"},
        )
        assert resp.status_code == 401

    def test_basic_auth_scheme_returns_401(self, client):
        """Wrong auth scheme (Basic instead of Bearer) must be rejected."""
        import base64
        credentials = base64.b64encode(b"user:pass").decode()
        resp = client.post(
            "/query",
            headers={"Authorization": f"Basic {credentials}"},
            json={"sql": "SELECT 1"},
        )
        # FastAPI's HTTPBearer(auto_error=False) will return None for non-Bearer schemes,
        # causing require_api_key to see credentials=None and return 401 MISSING_AUTH.
        assert resp.status_code == 401

    def test_token_prefix_only_no_value_returns_401(self, client):
        """Authorization header with only 'Bearer' (no space + value) must be rejected."""
        resp = client.post(
            "/query",
            headers={"Authorization": "Bearer"},
            json={"sql": "SELECT 1"},
        )
        assert resp.status_code == 401

    def test_partial_token_returns_401(self, client):
        """A token that is a prefix of the correct token must be rejected."""
        partial = VALID_API_KEY[:5]
        resp = client.post(
            "/query",
            headers={"Authorization": f"Bearer {partial}"},
            json={"sql": "SELECT 1"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Health endpoint — no auth required
# ---------------------------------------------------------------------------

class TestHealthNoAuth:

    def test_health_requires_no_auth(self, client):
        """/health must return 200 without any Authorization header."""
        # health.py does `from app.clickhouse_client import ping`, so patch the
        # bound name in the health module, not the source module.
        with patch("app.routers.health.ping", return_value=True):
            resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_returns_ok_status(self, client):
        with patch("app.routers.health.ping", return_value=True):
            resp = client.get("/health")
        body = resp.json()
        assert body["status"] == "ok"
        assert body["clickhouse"] == "ok"

    def test_health_clickhouse_error_still_200(self, client):
        """/health returns 200 even when ClickHouse is down — just reports error state."""
        with patch("app.routers.health.ping", return_value=False):
            resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["clickhouse"] == "error"
