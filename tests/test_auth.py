"""Tests for app/auth.py + app/auth_jwt.py — JWT authentication on REST routes.

Exercises require_principal through a real request/response cycle with the
TestClient.  ClickHouse is patched out, and the JWKS signing key is resolved to
a local public key by the autouse `_patch_jwks` fixture in conftest, so the full
validate_token() path runs offline.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.jwt_helpers import WRONG_PRIVATE_PEM, make_jwt


@pytest.fixture(autouse=True)
def patch_clickhouse():
    """Patch execute_query so routes resolve without a live ClickHouse."""
    with patch("app.service.execute_query", return_value=(["col"], [["v"]])):
        yield


@pytest.fixture()
def client():
    from app.main import app

    # raise_server_exceptions=False so error responses are returned, not raised.
    return TestClient(app, raise_server_exceptions=False)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Valid token
# ---------------------------------------------------------------------------

class TestValidToken:

    def test_valid_jwt_passes(self, client):
        resp = client.post("/query", headers=_bearer(make_jwt()), json={"sql": "SELECT 1"})
        assert resp.status_code != 401, resp.json()

    def test_valid_jwt_on_databases(self, client):
        resp = client.get("/databases", headers=_bearer(make_jwt()))
        assert resp.status_code != 401


# ---------------------------------------------------------------------------
# Missing / malformed credentials
# ---------------------------------------------------------------------------

class TestMissingToken:

    def test_no_auth_header_returns_401(self, client):
        resp = client.post("/query", json={"sql": "SELECT 1"})
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "MISSING_AUTH"

    def test_empty_bearer_returns_401(self, client):
        resp = client.post("/query", headers={"Authorization": "Bearer "}, json={"sql": "SELECT 1"})
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "MISSING_AUTH"

    def test_basic_scheme_returns_401(self, client):
        import base64

        creds = base64.b64encode(b"user:pass").decode()
        resp = client.post(
            "/query", headers={"Authorization": f"Basic {creds}"}, json={"sql": "SELECT 1"}
        )
        # HTTPBearer(auto_error=False) yields None for a non-Bearer scheme.
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "MISSING_AUTH"


# ---------------------------------------------------------------------------
# Invalid tokens — the isolation boundary
# ---------------------------------------------------------------------------

class TestInvalidToken:

    def test_bad_signature_returns_401(self, client):
        """A token signed by a key the JWKS does not vouch for must be rejected."""
        token = make_jwt(signing_key=WRONG_PRIVATE_PEM)
        resp = client.post("/query", headers=_bearer(token), json={"sql": "SELECT 1"})
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "INVALID_AUTH"

    def test_expired_token_returns_401(self, client):
        token = make_jwt(exp_delta=-3600)
        resp = client.post("/query", headers=_bearer(token), json={"sql": "SELECT 1"})
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "TOKEN_EXPIRED"

    def test_wrong_audience_returns_401(self, client):
        token = make_jwt(audience="some-other-api")
        resp = client.post("/query", headers=_bearer(token), json={"sql": "SELECT 1"})
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "INVALID_AUDIENCE"

    def test_wrong_issuer_returns_401(self, client):
        token = make_jwt(issuer="https://evil.example/")
        resp = client.post("/query", headers=_bearer(token), json={"sql": "SELECT 1"})
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "INVALID_ISSUER"

    def test_alg_none_returns_401(self, client):
        """An unsigned ('none') token must never be accepted."""
        token = make_jwt(algorithm="none")
        resp = client.post("/query", headers=_bearer(token), json={"sql": "SELECT 1"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Authenticated but missing tenant claim — fail closed with 403
# ---------------------------------------------------------------------------

class TestMissingTenantClaim:

    def test_missing_tenant_claim_returns_403(self, client):
        # No clientcode/proc_center/jti claims -> the first mapped tenant claim is
        # absent -> fail closed rather than run an untenanted query.
        token = make_jwt(include_tenant_claims=False)
        resp = client.post("/query", headers=_bearer(token), json={"sql": "SELECT 1"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "MISSING_TENANT_CLAIM"

    def test_blank_tenant_claim_returns_403(self, client):
        token = make_jwt(client_code="   ")
        resp = client.post("/query", headers=_bearer(token), json={"sql": "SELECT 1"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "MISSING_TENANT_CLAIM"


# ---------------------------------------------------------------------------
# Health endpoint — no auth required
# ---------------------------------------------------------------------------

class TestHealthNoAuth:

    def test_health_requires_no_auth(self, client):
        with patch("app.routers.health.ping", return_value=True):
            resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
