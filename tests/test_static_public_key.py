"""Static public-key verification — RS256 from an external minter with no JWKS.

When OIDC_PUBLIC_KEY is set, the asymmetric path verifies against that PEM
directly instead of fetching a JWKS document.  These drive validate_token() with
an explicit Settings carrying the PEM; because the static branch never calls
_resolve_signing_key, the autouse `_patch_jwks` fixture does not interfere.

The signing keypair comes from tests.jwt_helpers, so a token signed with its
PRIVATE key verifies against the corresponding PUBLIC PEM, and a token signed
with WRONG_PRIVATE_PEM does not.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization

from app.auth_jwt import JWTAuthError, validate_token
from app.config import Settings
from tests.jwt_helpers import PUBLIC_KEY, WRONG_PRIVATE_PEM, make_jwt


def _public_pem() -> str:
    return PUBLIC_KEY.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def _settings(**overrides) -> Settings:
    # OIDC_ISSUER/OIDC_AUDIENCE come from the conftest test env (match jwt_helpers).
    return Settings(**overrides)


class TestStaticKeyVerification:

    def test_valid_rs256_token_verified_against_static_pem(self):
        settings = _settings(oidc_public_key=_public_pem(), oidc_jwks_url="")
        principal = validate_token(make_jwt(), settings)
        assert principal.claims["user_name"] == "alice"

    def test_token_signed_by_wrong_key_is_rejected(self):
        settings = _settings(oidc_public_key=_public_pem(), oidc_jwks_url="")
        token = make_jwt(signing_key=WRONG_PRIVATE_PEM)
        with pytest.raises(JWTAuthError) as exc:
            validate_token(token, settings)
        assert exc.value.code == "INVALID_AUTH"

    def test_static_key_takes_precedence_over_jwks_url(self):
        # A bogus JWKS URL that would fail if fetched — but the static key path
        # short-circuits before any network call, so a valid token still passes.
        settings = _settings(
            oidc_public_key=_public_pem(),
            oidc_jwks_url="https://unreachable.invalid/jwks.json",
        )
        principal = validate_token(make_jwt(), settings)
        assert principal.claims["user_name"] == "alice"

    def test_escaped_newline_pem_is_normalised_and_works(self):
        # env/secret stores often flatten a PEM to one line with literal \n.
        flattened = _public_pem().replace("\n", "\\n")
        settings = _settings(oidc_public_key=flattened, oidc_jwks_url="")
        principal = validate_token(make_jwt(), settings)
        assert principal.claims["user_name"] == "alice"

    def test_claims_still_enforced_on_static_path(self):
        settings = _settings(oidc_public_key=_public_pem(), oidc_jwks_url="")
        token = make_jwt(audience="some-other-api")
        with pytest.raises(JWTAuthError) as exc:
            validate_token(token, settings)
        assert exc.value.code == "INVALID_AUDIENCE"


class TestStaticKeyConfig:

    def test_invalid_pem_rejected_at_config_time(self):
        with pytest.raises(ValueError, match="valid PEM-encoded public key"):
            _settings(oidc_public_key="not-a-pem")

    def test_private_key_pem_rejected(self):
        # A pasted PRIVATE key must not be accepted as a verification key.
        with pytest.raises(ValueError, match="valid PEM-encoded public key"):
            _settings(oidc_public_key=WRONG_PRIVATE_PEM.decode("ascii"))

    def test_auth_configured_true_with_static_key_and_no_jwks(self):
        settings = _settings(oidc_public_key=_public_pem(), oidc_jwks_url="")
        assert settings.auth_configured() is True
