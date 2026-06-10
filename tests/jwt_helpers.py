"""Offline JWT minting helpers shared across the test suite.

A process-wide RSA keypair signs test tokens.  Tests pair this with the autouse
``_patch_jwks`` fixture in conftest, which points
``app.auth_jwt._resolve_signing_key`` at :data:`PUBLIC_KEY` so the full
validate_token() path runs without contacting a real JWKS endpoint.  Tokens
minted with :data:`WRONG_PRIVATE_PEM` therefore fail signature verification —
exactly what the negative tests need.

These constants MUST match the OIDC_* values configured in conftest._TEST_ENV.
"""

from __future__ import annotations

import time

import jwt as _jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

TEST_ISSUER = "https://idp.test/"
TEST_AUDIENCE = "clickhouse-api"
TEST_KID = "test-key-1"


def _pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _pem(_PRIVATE_KEY)
PUBLIC_KEY = _PRIVATE_KEY.public_key()

# A second, unrelated keypair used to forge tokens with a bad signature.
_WRONG_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
WRONG_PRIVATE_PEM = _pem(_WRONG_PRIVATE_KEY)


def make_jwt(
    *,
    user_name: str = "alice",
    sub: str = "user-1",
    issuer: str | None = None,
    audience: str | None = None,
    exp_delta: int = 3600,
    algorithm: str = "RS256",
    signing_key: bytes | None = None,
    include_user_name: bool = True,
    extra_claims: dict | None = None,
) -> str:
    """Mint a signed JWT for tests.

    Defaults produce a fully valid token carrying the ``user_name`` tenant claim.
    Override the keyword args to exercise negative cases (expired, wrong
    audience/issuer, missing tenant claim, bad signature, alg=none, ...).
    """
    now = int(time.time())
    claims: dict = {
        "sub": sub,
        "iss": issuer if issuer is not None else TEST_ISSUER,
        "aud": audience if audience is not None else TEST_AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + exp_delta,
    }
    if include_user_name:
        claims["user_name"] = user_name
    if extra_claims:
        claims.update(extra_claims)

    headers = {"kid": TEST_KID}
    if algorithm == "none":
        return _jwt.encode(claims, key="", algorithm="none", headers=headers)
    key = signing_key if signing_key is not None else PRIVATE_PEM
    return _jwt.encode(claims, key, algorithm=algorithm, headers=headers)
