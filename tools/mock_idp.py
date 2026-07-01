"""A self-contained mock OIDC identity provider for local dev and tests.

It generates an RSA signing keypair, publishes the public half as a JWKS
document, and mints signed JWTs carrying the ``user_name`` tenant claim — so you
can exercise the *real* validation path in app/auth_jwt.py (JWKS fetch +
signature verify) without a real IdP.

Run it:
    python -m tools.mock_idp                  # serves on :9001 by default

Point the API at it:
    export OIDC_JWKS_URL=http://localhost:9001/.well-known/jwks.json
    export OIDC_ISSUER=http://localhost:9001/
    export OIDC_AUDIENCE=clickhouse-api

Mint a token and call the API:
    TOKEN=$(curl -s 'http://localhost:9001/token?user_name=alice' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
    curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/databases

The importable helpers (generate_keypair / build_jwks / mint_token) back both
this server and tests/test_jwt_roundtrip.py, so the key that signs a token is
always the key the JWKS publishes.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

DEFAULT_PORT = 9001
DEFAULT_ISSUER = f"http://localhost:{DEFAULT_PORT}/"
DEFAULT_AUDIENCE = "clickhouse-api"
DEFAULT_KID = "mock-idp-key-1"


# ---------------------------------------------------------------------------
# Crypto / token helpers (importable; reused by tests)
# ---------------------------------------------------------------------------

def generate_keypair() -> rsa.RSAPrivateKey:
    """Return a fresh RSA-2048 private key (its .public_key() backs the JWKS)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def build_jwks(public_key: rsa.RSAPublicKey, kid: str = DEFAULT_KID) -> dict[str, Any]:
    """Build a JWKS document (``{"keys": [...]}``) for *public_key*.

    Uses PyJWT's RSAAlgorithm.to_jwk to emit the RSA `n`/`e` members, then adds
    the `kid`/`use`/`alg` metadata clients match against.
    """
    jwk = json.loads(RSAAlgorithm.to_jwk(public_key))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


def mint_token(
    private_key: rsa.RSAPrivateKey,
    *,
    user_name: str = "alice",
    sub: Optional[str] = None,
    issuer: str = DEFAULT_ISSUER,
    audience: str = DEFAULT_AUDIENCE,
    kid: str = DEFAULT_KID,
    ttl: int = 3600,
    extra_claims: Optional[dict[str, Any]] = None,
) -> str:
    """Mint an RS256-signed JWT with the standard claims + the tenant claim.

    *ttl* is seconds until expiry; pass a negative value to mint an expired
    token for negative tests.  The `kid` header must match the JWKS `kid`.

    Includes ``column_scope`` (empty JSON list — no column restrictions) so tokens
    pass validate_token's required-claim checks out of the box.
    session_id is NOT a JWT claim — it flows via the X-Session-Id request header.
    """
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": sub or f"user-{user_name}",
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "user_name": user_name,
        # column_scope: empty list means no column restrictions (all columns permitted).
        # Enforcement only fires when scope is non-empty and query uses disallowed columns.
        # session_id is NOT a JWT claim — it is sent by the agent backend as X-Session-Id.
        "column_scope": json.dumps([]),
    }
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


# ---------------------------------------------------------------------------
# Runnable Starlette app
# ---------------------------------------------------------------------------

def create_app(
    *,
    issuer: Optional[str] = None,
    audience: Optional[str] = None,
    kid: str = DEFAULT_KID,
):
    """Build the mock-IdP ASGI app.

    Generates a keypair at construction time and exposes:
      GET  /.well-known/jwks.json  → the JWKS for the generated public key
      GET|POST /token              → mint a JWT (params: user_name, sub, ttl)
    """
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    issuer = issuer or os.getenv("MOCK_IDP_ISSUER", DEFAULT_ISSUER)
    audience = audience or os.getenv("MOCK_IDP_AUDIENCE", DEFAULT_AUDIENCE)

    private_key = generate_keypair()
    jwks = build_jwks(private_key.public_key(), kid=kid)

    async def jwks_endpoint(request):
        return JSONResponse(jwks)

    async def token_endpoint(request):
        params: dict[str, Any] = dict(request.query_params)
        if request.method == "POST":
            try:
                params.update(await request.json())
            except Exception:  # noqa: BLE001 — tolerate empty/non-JSON bodies
                pass
        user_name = params.get("user_name", "alice")
        sub = params.get("sub")
        ttl = int(params.get("ttl", 3600))
        token = mint_token(
            private_key,
            user_name=user_name,
            sub=sub,
            issuer=issuer,
            audience=audience,
            kid=kid,
            ttl=ttl,
        )
        return JSONResponse(
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": ttl,
                "user_name": user_name,
            }
        )

    return Starlette(
        routes=[
            Route("/.well-known/jwks.json", jwks_endpoint, methods=["GET"]),
            Route("/token", token_endpoint, methods=["GET", "POST"]),
        ]
    )


def main() -> None:
    import uvicorn

    port = int(os.getenv("MOCK_IDP_PORT", str(DEFAULT_PORT)))
    issuer = os.getenv("MOCK_IDP_ISSUER", f"http://localhost:{port}/")
    audience = os.getenv("MOCK_IDP_AUDIENCE", DEFAULT_AUDIENCE)

    print("Mock IdP starting. Configure the API with:")
    print(f"  export OIDC_JWKS_URL=http://localhost:{port}/.well-known/jwks.json")
    print(f"  export OIDC_ISSUER={issuer}")
    print(f"  export OIDC_AUDIENCE={audience}")
    print(f"Mint a token:  curl -s 'http://localhost:{port}/token?user_name=alice'")

    uvicorn.run(create_app(issuer=issuer, audience=audience), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
