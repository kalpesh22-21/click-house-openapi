"""External JWT / OIDC validation — the isolation boundary for this service.

Because every query runs as one privileged ClickHouse account and ClickHouse
row policies trust ``getSetting('SQL_tenant')`` blindly, a forged or
over-permissive token equals full cross-tenant read.  This module is therefore
the entire authorization boundary and is deliberately strict:

  * Signature verified against the IdP's JWKS (public keys only) — or, for an
    external minter that publishes no JWKS, against a configured static public key
    (OIDC_PUBLIC_KEY), which takes precedence over the JWKS URL when set.
  * Algorithm pinned to the configured asymmetric allowlist — PyJWT rejects any
    token whose header ``alg`` is outside it, defeating 'none' and RS/HS
    algorithm-confusion attacks (the config validator also refuses to *configure*
    HS*/none, so a public key can never be coerced into an HMAC secret).
  * expiry / not-before are always required and verified.  issuer / audience are
    verified only when configured (OIDC_ISSUER / OIDC_AUDIENCE); left empty, that
    claim check is skipped — for an external minter that stamps neither.  The
    signature and algorithm pinning above are unconditional and are the core.
  * Every JWT claim that a tenant setting maps to must be present and non-empty,
    or the request is rejected (fail closed) — never run a query with an absent
    tenant, which would make row policies match nothing or (mis-policied)
    everything.

Both transports call :func:`validate_token`; on success it returns a
:class:`app.principal.Principal` carrying the verified claims.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any, Optional

import jwt
from jwt import PyJWKClient

from app.config import Settings, get_settings
from app.principal import Principal

logger = logging.getLogger(__name__)


class JWTAuthError(Exception):
    """Raised when a token cannot be validated.

    Carries an HTTP *status_code* and a stable *code* so each transport can
    render a consistent error response.  Messages are intentionally generic —
    detail stays in server logs, not in the client response.
    """

    def __init__(self, message: str, code: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


# PyJWKClient instances cache fetched signing keys internally; we keep one per
# JWKS URL for the life of the process so we are not refetching keys per request.
_jwk_clients: dict[str, PyJWKClient] = {}


def _jwk_client(jwks_url: str) -> PyJWKClient:
    client = _jwk_clients.get(jwks_url)
    if client is None:
        # lifespan="optional" keeps PyJWKClient from holding a connection open;
        # cache_keys=True reuses fetched JWKs across requests.
        client = PyJWKClient(jwks_url, cache_keys=True)
        _jwk_clients[jwks_url] = client
    return client


@lru_cache(maxsize=8)
def _load_public_key(pem: str) -> Any:
    """Load a PEM public key, memoised per PEM string.

    The PEM is already validated/normalised by the config layer; loading is
    cached so we do not re-parse it on every request.  Keyed by the PEM string
    so a config change (new key) naturally produces a new cache entry.
    """
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    return load_pem_public_key(pem.encode())


def _resolve_signing_key(token: str, settings: Settings) -> Any:
    """Return the public signing key for *token* from the JWKS endpoint.

    Reads the (unverified) ``kid`` header to select the matching JWK.  This is
    the network seam — tests patch this function to return a known public key so
    the decode/claims path can be exercised without a live IdP.
    """
    client = _jwk_client(settings.oidc_jwks_url)
    return client.get_signing_key_from_jwt(token).key


def _decode_and_map(token: str, key: Any, algorithms: list[str], settings: Settings) -> dict:
    """Verify *token* against *key*, pinned to *algorithms*, and return its claims.

    Shared by the static-key and JWKS paths.  ``algorithms`` is always the
    asymmetric allowlist, so PyJWT rejects any token whose header ``alg`` is
    outside it.  All JWTAuthError mapping lives here so both callers behave
    identically.
    """
    # Issuer/audience are optional: verify each only when it is configured, so an
    # external minter that stamps neither still validates.  Expiry is always
    # required; the signature and algorithm pinning are unconditional.
    issuer = settings.oidc_issuer.strip()
    audience = settings.oidc_audience.strip()
    require = ["exp"]
    decode_kwargs: dict[str, Any] = {}
    if issuer:
        require.append("iss")
        decode_kwargs["issuer"] = settings.oidc_issuer
    if audience:
        require.append("aud")
        decode_kwargs["audience"] = settings.oidc_audience
    try:
        return jwt.decode(
            token,
            key,
            algorithms=algorithms,
            leeway=60,  # tolerate up to 60s of clock skew on exp/nbf/iat
            # verify_aud is False when no audience is configured, so a token that
            # DOES carry an aud claim is not rejected for being unverifiable.
            options={"require": require, "verify_aud": bool(audience)},
            **decode_kwargs,
        )
    except jwt.ExpiredSignatureError as exc:
        raise JWTAuthError("Token has expired.", code="TOKEN_EXPIRED") from exc
    except jwt.InvalidAudienceError as exc:
        raise JWTAuthError("Token audience is not accepted.", code="INVALID_AUDIENCE") from exc
    except jwt.InvalidIssuerError as exc:
        raise JWTAuthError("Token issuer is not accepted.", code="INVALID_ISSUER") from exc
    except jwt.InvalidAlgorithmError as exc:
        raise JWTAuthError("Token algorithm is not accepted.", code="INVALID_ALGORITHM") from exc
    except jwt.InvalidTokenError as exc:
        # Catch-all for signature failures, malformed tokens, missing required
        # claims, immature (nbf) tokens, etc.  Detail to logs only.
        logger.info("JWT validation failed: %s", exc)
        raise JWTAuthError("Invalid token.", code="INVALID_AUTH") from exc


def _verify_signature(token: str, settings: Settings) -> dict:
    """Verify *token*'s signature and return its claims.

    A configured static public key (``OIDC_PUBLIC_KEY``) wins over JWKS: verify
    against it directly (no network).  This serves an external minter that
    publishes no JWKS endpoint.  Either way, verification is pinned to the
    asymmetric allowlist (``JWT_ALGORITHMS``), which excludes ``none`` and HMAC —
    a public key is never offered to an HMAC verification.
    """
    static_pem = settings.oidc_public_key.strip()
    if static_pem:
        return _decode_and_map(
            token, _load_public_key(static_pem), settings.jwt_algorithms_list(), settings
        )

    # Otherwise resolve the public signing key from the IdP's JWKS.
    try:
        signing_key = _resolve_signing_key(token, settings)
    except jwt.PyJWKClientError as exc:
        # No matching kid, or the JWKS endpoint could not be reached/parsed.
        # The former is the client's fault; the latter is ours.  We cannot
        # cleanly distinguish here, so log detail and return a generic 503 —
        # failing closed (no token is trusted) rather than open.
        logger.warning("JWKS signing-key resolution failed: %s", exc)
        raise JWTAuthError(
            "Unable to verify token signing key.",
            code="JWKS_UNAVAILABLE",
            status_code=503,
        ) from exc
    except jwt.InvalidTokenError as exc:
        # A malformed token (not a JWT, bad/garbled header) fails inside
        # get_signing_key_from_jwt — which reads the unverified header to pick a
        # key — BEFORE we reach jwt.decode().  Treat it as an invalid token (401),
        # not a server error.
        logger.info("Malformed token rejected during key resolution: %s", exc)
        raise JWTAuthError("Invalid token.", code="INVALID_AUTH") from exc
    except Exception as exc:  # noqa: BLE001
        # Defensive: the auth boundary must NEVER surface a 500.  Any unexpected
        # failure resolving the signing key fails closed as an invalid token.
        logger.warning("Unexpected error resolving signing key: %s", exc)
        raise JWTAuthError("Invalid token.", code="INVALID_AUTH") from exc

    return _decode_and_map(token, signing_key, settings.jwt_algorithms_list(), settings)


def validate_token(token: str, settings: Optional[Settings] = None) -> Principal:
    """Validate *token* and return the authenticated :class:`Principal`.

    Raises:
        JWTAuthError: on any signature/claim failure (401), a token that is valid
            but missing a required tenant claim (403), or an unreachable JWKS
            endpoint (503).
    """
    if settings is None:
        settings = get_settings()

    if not token or not token.strip():
        raise JWTAuthError("Empty bearer token.", code="MISSING_AUTH")

    claims = _verify_signature(token, settings)

    # Fail closed: every claim a tenant setting maps to must be present and
    # non-empty.  This is enforced here (before any query) so a token that is
    # validly signed but missing the tenant claim is rejected outright.
    for ch_key, claim_name in settings.clickhouse_tenant_settings.items():
        value = claims.get(claim_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            logger.info(
                "Token missing required tenant claim '%s' (for setting '%s').",
                claim_name,
                ch_key,
            )
            raise JWTAuthError(
                f"Token is missing the required '{claim_name}' claim.",
                code="MISSING_TENANT_CLAIM",
                status_code=403,
            )

    # --- Extract column_scope claim ---
    # Expected: a JSON-encoded list of "database.table.column" strings.
    raw_scope = claims.get("column_scope")
    if raw_scope is None:
        logger.info("Token missing required 'column_scope' claim.")
        raise JWTAuthError(
            "Token is missing the required 'column_scope' claim.",
            code="MISSING_SCOPE_CLAIM",
            status_code=403,
        )
    if isinstance(raw_scope, str):
        try:
            scope_list = json.loads(raw_scope)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.info("Token 'column_scope' claim is not valid JSON: %s", exc)
            raise JWTAuthError(
                "Token 'column_scope' claim is not a valid JSON list.",
                code="MISSING_SCOPE_CLAIM",
                status_code=403,
            ) from exc
    elif isinstance(raw_scope, list):
        scope_list = raw_scope
    else:
        logger.info(
            "Token 'column_scope' claim has unexpected type: %s", type(raw_scope).__name__
        )
        raise JWTAuthError(
            "Token 'column_scope' claim must be a JSON list.",
            code="MISSING_SCOPE_CLAIM",
            status_code=403,
        )
    if not isinstance(scope_list, list) or not all(isinstance(s, str) for s in scope_list):
        logger.info("Token 'column_scope' is not a list of strings.")
        raise JWTAuthError(
            "Token 'column_scope' claim must be a list of strings.",
            code="MISSING_SCOPE_CLAIM",
            status_code=403,
        )
    column_scope: frozenset[str] = frozenset(scope_list)

    subject = str(claims.get("sub", "")) or "unknown"
    return Principal(
        subject=subject,
        claims=claims,
        column_scope=column_scope,
        # Retain the raw token so the employee-access fetch can forward the very
        # same JWT as a Bearer credential to /cl/eeaccess. Sensitive — never logged.
        token=token,
    )
