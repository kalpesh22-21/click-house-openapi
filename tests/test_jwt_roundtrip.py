"""End-to-end JWT round-trip against a real (in-process) JWKS endpoint.

Unlike the rest of the suite — which patches app.auth_jwt._resolve_signing_key
to skip the network — these tests stand up a tiny HTTP server publishing a real
JWKS document and let the genuine validate_token() path run:
PyJWKClient fetches the JWKS over the socket, matches the `kid`, and verifies the
RS256 signature.  The signing keypair and token minting come from
tools.mock_idp, so the key that signs is exactly the key the JWKS publishes.

Marked `real_jwks` so the autouse `_patch_jwks` fixture in conftest does NOT
short-circuit the signing-key lookup.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import app.auth_jwt as auth_jwt
from app.auth_jwt import JWTAuthError, validate_token
from app.config import Settings
from tools.mock_idp import build_jwks, generate_keypair, mint_token

pytestmark = pytest.mark.real_jwks

_ISSUER = "http://mock-idp.test/"
_AUDIENCE = "clickhouse-api"


@pytest.fixture()
def idp():
    """Serve a real JWKS for a freshly generated keypair on an ephemeral port."""
    key = generate_keypair()
    body = json.dumps(build_jwks(key.public_key())).encode("utf-8")

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server API
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence per-request logging
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # Clear any cached PyJWKClient so this URL is fetched fresh.
    auth_jwt._jwk_clients.clear()
    try:
        yield {"key": key, "url": f"http://127.0.0.1:{port}/.well-known/jwks.json"}
    finally:
        server.shutdown()
        auth_jwt._jwk_clients.clear()


def _settings(idp) -> Settings:
    # CLICKHOUSE_* required fields come from the conftest test env; we only
    # override the OIDC bits to point at our in-process IdP.
    return Settings(
        oidc_jwks_url=idp["url"],
        oidc_issuer=_ISSUER,
        oidc_audience=_AUDIENCE,
    )


class TestJwtRoundTrip:

    def test_valid_token_validates_via_real_jwks(self, idp):
        token = mint_token(idp["key"], user_name="alice", issuer=_ISSUER, audience=_AUDIENCE)
        principal = validate_token(token, _settings(idp))
        assert principal.claims["user_name"] == "alice"
        assert principal.subject == "user-alice"

    def test_token_signed_by_unpublished_key_is_rejected(self, idp):
        # Same kid as the JWKS, but signed by a different private key → the
        # published public key cannot verify the signature.
        impostor = generate_keypair()
        token = mint_token(impostor, user_name="alice", issuer=_ISSUER, audience=_AUDIENCE)
        with pytest.raises(JWTAuthError) as exc:
            validate_token(token, _settings(idp))
        assert exc.value.code == "INVALID_AUTH"

    def test_expired_token_is_rejected(self, idp):
        token = mint_token(idp["key"], ttl=-3600, issuer=_ISSUER, audience=_AUDIENCE)
        with pytest.raises(JWTAuthError) as exc:
            validate_token(token, _settings(idp))
        assert exc.value.code == "TOKEN_EXPIRED"

    def test_wrong_audience_is_rejected(self, idp):
        token = mint_token(idp["key"], issuer=_ISSUER, audience="some-other-api")
        with pytest.raises(JWTAuthError) as exc:
            validate_token(token, _settings(idp))
        assert exc.value.code == "INVALID_AUDIENCE"

    def test_malformed_token_is_rejected_not_500(self, idp):
        # Regression: a non-JWT string fails inside get_signing_key_from_jwt
        # (header parse) before jwt.decode. validate_token must surface a clean
        # JWTAuthError(401), never let the DecodeError escape as a 500.
        with pytest.raises(JWTAuthError) as exc:
            validate_token("not-a-real-token", _settings(idp))
        assert exc.value.status_code == 401
        assert exc.value.code == "INVALID_AUTH"

    def test_missing_tenant_claim_is_rejected(self, idp):
        # Mint a token, then strip user_name by re-minting with an override that
        # blanks it via extra_claims is not enough — instead audience/issuer are
        # valid but the tenant claim is absent: use ttl/user_name then drop it.
        import jwt as _jwt
        import time as _time

        now = int(_time.time())
        token = _jwt.encode(
            {
                "sub": "u1",
                "iss": _ISSUER,
                "aud": _AUDIENCE,
                "iat": now,
                "nbf": now,
                "exp": now + 3600,
            },
            idp["key"],
            algorithm="RS256",
            headers={"kid": "mock-idp-key-1"},
        )
        with pytest.raises(JWTAuthError) as exc:
            validate_token(token, _settings(idp))
        assert exc.value.code == "MISSING_TENANT_CLAIM"
