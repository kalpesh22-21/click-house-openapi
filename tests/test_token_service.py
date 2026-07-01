"""Tests for app/token_service.py — the token-issuing service.

Covers JWKS/discovery/health, the issuer-API-key guard on POST /token, config
fail-closed behaviour, and an end-to-end mint -> validate round trip where the
ClickHouse API's real app.auth_jwt.validate_token accepts a token this service
minted (verifying against the service's published public key).
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.token_service import TokenSettings, create_app

_ISSUER = "https://token.test/"
_AUDIENCE = "clickhouse-api"
_ISSUER_KEY = "issuer-secret-key"


def _signing_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


@pytest.fixture()
def signing_pem() -> bytes:
    return _signing_pem()


@pytest.fixture()
def settings(signing_pem) -> TokenSettings:
    return TokenSettings(
        token_issuer=_ISSUER,
        token_audience=_AUDIENCE,
        token_signing_key=signing_pem.decode("utf-8"),
        token_issuer_api_key=_ISSUER_KEY,
        token_dev_mode=False,
    )


@pytest.fixture()
def client(settings) -> TestClient:
    return TestClient(create_app(settings), raise_server_exceptions=False)


_ISSUER_AUTH = {"Authorization": f"Bearer {_ISSUER_KEY}"}


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------

class TestPublicEndpoints:

    def test_health(self, client):
        assert client.get("/health").json() == {"status": "ok"}

    def test_jwks_has_one_signing_key(self, client):
        jwks = client.get("/.well-known/jwks.json").json()
        assert len(jwks["keys"]) == 1
        jwk = jwks["keys"][0]
        assert jwk["kty"] == "RSA"
        assert jwk["use"] == "sig"
        assert jwk["kid"]

    def test_discovery_points_at_jwks(self, client):
        doc = client.get("/.well-known/openid-configuration").json()
        assert doc["issuer"] == _ISSUER
        assert doc["jwks_uri"].endswith("/.well-known/jwks.json")


# ---------------------------------------------------------------------------
# POST /token — issuance + the issuer-API-key guard
# ---------------------------------------------------------------------------

class TestTokenIssuance:

    def test_mint_requires_issuer_key(self, client):
        resp = client.post("/token", json={"user_name": "alice"})
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "MISSING_AUTH"

    def test_mint_rejects_wrong_issuer_key(self, client):
        resp = client.post(
            "/token", json={"user_name": "alice"}, headers={"Authorization": "Bearer nope"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "INVALID_AUTH"

    def test_mint_returns_token_with_claims(self, client):
        resp = client.post("/token", json={"user_name": "alice"}, headers=_ISSUER_AUTH)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["user_name"] == "alice"
        assert body["token_type"] == "Bearer"
        assert body["access_token"].count(".") == 2  # header.payload.signature

    def test_mint_requires_user_name(self, client):
        resp = client.post("/token", json={}, headers=_ISSUER_AUTH)
        assert resp.status_code == 422  # pydantic validation


# ---------------------------------------------------------------------------
# Config fail-closed
# ---------------------------------------------------------------------------

class TestConfigFailClosed:

    def test_missing_issuer_or_audience_raises(self, signing_pem):
        with pytest.raises(RuntimeError):
            create_app(
                TokenSettings(
                    token_issuer="",
                    token_audience=_AUDIENCE,
                    token_signing_key=signing_pem.decode(),
                    token_issuer_api_key=_ISSUER_KEY,
                )
            )

    def test_missing_issuer_api_key_without_dev_mode_raises(self, signing_pem):
        with pytest.raises(RuntimeError):
            create_app(
                TokenSettings(
                    token_issuer=_ISSUER,
                    token_audience=_AUDIENCE,
                    token_signing_key=signing_pem.decode(),
                    token_issuer_api_key="",
                    token_dev_mode=False,
                )
            )

    def test_no_signing_key_without_dev_mode_raises(self):
        with pytest.raises(RuntimeError):
            create_app(
                TokenSettings(
                    token_issuer=_ISSUER,
                    token_audience=_AUDIENCE,
                    token_signing_key="",
                    token_signing_key_file="",
                    token_issuer_api_key=_ISSUER_KEY,
                    token_dev_mode=False,
                )
            )

    def test_dev_mode_generates_key_and_opens_token(self):
        # No signing key, no issuer key — dev mode must still build and mint.
        app = create_app(
            TokenSettings(
                token_issuer=_ISSUER, token_audience=_AUDIENCE, token_dev_mode=True
            )
        )
        c = TestClient(app, raise_server_exceptions=False)
        assert len(c.get("/.well-known/jwks.json").json()["keys"]) == 1
        resp = c.post("/token", json={"user_name": "bob"})  # no auth header in dev
        assert resp.status_code == 200, resp.text
        assert resp.json()["user_name"] == "bob"


# ---------------------------------------------------------------------------
# End-to-end: a token minted here validates in the ClickHouse API
# ---------------------------------------------------------------------------

class TestEndToEndWithClickHouseApi:

    def test_minted_token_validates_in_api(self, client, signing_pem, monkeypatch):
        # Mint a token from the running token service.
        minted = client.post(
            "/token", json={"user_name": "carol"}, headers=_ISSUER_AUTH
        ).json()["access_token"]

        # The API validates against the service's PUBLIC key. Resolve the public
        # key from the same signing PEM and feed it to the validator (standing in
        # for the JWKS fetch, which tests/test_jwt_roundtrip.py exercises for real).
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        public_key = load_pem_private_key(signing_pem, password=None).public_key()
        monkeypatch.setattr(
            "app.auth_jwt._resolve_signing_key", lambda token, settings: public_key
        )

        from app.auth_jwt import validate_token
        from app.config import Settings

        api_settings = Settings(
            oidc_jwks_url="https://unused.test/jwks.json",
            oidc_issuer=_ISSUER,
            oidc_audience=_AUDIENCE,
        )
        principal = validate_token(minted, api_settings)
        assert principal.claims["user_name"] == "carol"


# ---------------------------------------------------------------------------
# column_scope field — issuance, validation, and round-trip
# ---------------------------------------------------------------------------

class TestColumnScopeIssuance:

    def test_column_scope_list_embedded_in_token(self, client):
        """POST /token with column_scope list → JWT carries that exact scope."""
        import json as _json
        import base64

        scope = ["analytics.orders.order_id", "analytics.orders.status"]
        resp = client.post(
            "/token",
            json={"user_name": "dave", "column_scope": scope},
            headers=_ISSUER_AUTH,
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]

        # Decode payload (no signature check needed — we just want the claim value).
        payload_b64 = token.split(".")[1]
        # Re-pad for standard base64 decoding.
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))

        assert payload["column_scope"] == _json.dumps(scope)

    def test_omitted_column_scope_gives_allow_all(self, client):
        """POST /token without column_scope → token carries column_scope: '[]' (allow-all)."""
        import json as _json
        import base64

        resp = client.post("/token", json={"user_name": "eve"}, headers=_ISSUER_AUTH)
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]

        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))

        assert payload["column_scope"] == _json.dumps([])

    def test_empty_column_scope_list_gives_allow_all(self, client):
        """POST /token with column_scope=[] → token carries column_scope: '[]' (allow-all)."""
        import json as _json
        import base64

        resp = client.post(
            "/token", json={"user_name": "frank", "column_scope": []}, headers=_ISSUER_AUTH
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]

        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))

        assert payload["column_scope"] == _json.dumps([])

    def test_column_scope_string_instead_of_list_rejected(self, client):
        """column_scope as a plain string (not a list) → 422 validation error."""
        resp = client.post(
            "/token",
            json={"user_name": "grace", "column_scope": "analytics.orders.order_id"},
            headers=_ISSUER_AUTH,
        )
        assert resp.status_code == 422

    def test_column_scope_list_with_empty_string_rejected(self, client):
        """A list containing an empty string → 422 validation error."""
        resp = client.post(
            "/token",
            json={"user_name": "heidi", "column_scope": ["analytics.orders.order_id", ""]},
            headers=_ISSUER_AUTH,
        )
        assert resp.status_code == 422

    def test_column_scope_list_with_whitespace_string_rejected(self, client):
        """A list containing a whitespace-only string → 422 validation error."""
        resp = client.post(
            "/token",
            json={"user_name": "ivan", "column_scope": ["   "]},
            headers=_ISSUER_AUTH,
        )
        assert resp.status_code == 422


class TestColumnScopeMintValidateRoundTrip:
    """Mint a scoped token via token_service, then run it through validate_token.

    This proves that a token_service-minted restricted token is accepted by the
    MCP's auth layer and that Principal.column_scope carries the intended triples.
    The token_service's own RSA keypair signs the token; we resolve the matching
    public key to stand in for the JWKS fetch (same pattern as
    test_minted_token_validates_in_api).
    """

    def test_scoped_token_round_trip_via_validate_token(self, client, signing_pem, monkeypatch):
        """Mint a restricted token → validate_token → Principal.column_scope matches."""
        scope = ["db.t.col_a", "db.t.col_b"]
        minted = client.post(
            "/token",
            json={"user_name": "judy", "column_scope": scope},
            headers=_ISSUER_AUTH,
        ).json()["access_token"]

        # Resolve the token_service's public key to stand in for the JWKS fetch.
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        public_key = load_pem_private_key(signing_pem, password=None).public_key()
        monkeypatch.setattr(
            "app.auth_jwt._resolve_signing_key", lambda token, settings: public_key
        )

        from app.auth_jwt import validate_token
        from app.config import Settings

        api_settings = Settings(
            oidc_jwks_url="https://unused.test/jwks.json",
            oidc_issuer=_ISSUER,
            oidc_audience=_AUDIENCE,
        )
        principal = validate_token(minted, api_settings)

        # The column_scope on the Principal must be exactly the frozenset of the two triples.
        assert principal.column_scope == frozenset(scope)
        # user_name is also carried through correctly.
        assert principal.claims["user_name"] == "judy"

    def test_allow_all_token_round_trip_principal_has_empty_frozenset(
        self, client, signing_pem, monkeypatch
    ):
        """Mint an allow-all token → Principal.column_scope is an empty frozenset."""
        minted = client.post(
            "/token", json={"user_name": "karl"}, headers=_ISSUER_AUTH
        ).json()["access_token"]

        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        public_key = load_pem_private_key(signing_pem, password=None).public_key()
        monkeypatch.setattr(
            "app.auth_jwt._resolve_signing_key", lambda token, settings: public_key
        )

        from app.auth_jwt import validate_token
        from app.config import Settings

        api_settings = Settings(
            oidc_jwks_url="https://unused.test/jwks.json",
            oidc_issuer=_ISSUER,
            oidc_audience=_AUDIENCE,
        )
        principal = validate_token(minted, api_settings)
        assert principal.column_scope == frozenset()
        assert principal.claims["user_name"] == "karl"
