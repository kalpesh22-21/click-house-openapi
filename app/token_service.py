"""Token-issuing service (a minimal OIDC-style identity provider).

A standalone FastAPI app — deployed separately from the ClickHouse API — that:
  * holds an RSA signing key (loaded from a mounted secret, NOT generated per
    pod, so every replica signs with the same key and the JWKS is consistent),
  * publishes the public half as JWKS at /.well-known/jwks.json,
  * mints per-user JWTs (carrying the ``user_name`` tenant claim) at POST /token,
    guarded by a static issuer API key,
  * exposes /.well-known/openid-configuration and /health.

The ClickHouse API points OIDC_JWKS_URL / OIDC_ISSUER / OIDC_AUDIENCE at this
service and validates the tokens it mints (app/auth_jwt.py).  This service is the
only component that ever holds the private key; the ClickHouse API only ever
sees public keys.

Run (factory):
    uvicorn app.token_service:create_app --factory --host 0.0.0.0 --port 8000
or via the shared image with MODE=token (see entrypoint.sh).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.algorithms import RSAAlgorithm
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration (independent of app.config — the issuer needs no ClickHouse)
# ---------------------------------------------------------------------------

class TokenSettings(BaseSettings):
    """Token-service configuration, read from TOKEN_* environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # Claims stamped on every issued token. These MUST match the ClickHouse API's
    # OIDC_ISSUER / OIDC_AUDIENCE for validation to succeed.
    token_issuer: str = Field("", description="Value stamped as the JWT 'iss' claim.")
    token_audience: str = Field("", description="Value stamped as the JWT 'aud' claim.")
    token_ttl_seconds: int = Field(3600, ge=1, description="Default token lifetime in seconds.")
    token_algorithm: str = Field("RS256", description="Signing algorithm (RS256/ES256/PS256).")

    # Signing key — supply ONE of these (file preferred for multi-line PEM via a
    # mounted Secret). The private key must be the same across all replicas.
    token_signing_key: str = Field("", description="PEM private key (inline).")
    token_signing_key_file: str = Field("", description="Path to a PEM private key file.")
    token_kid: str = Field("", description="Explicit JWKS key id; derived from the key if empty.")

    # Static API key that authorizes POST /token (minting is privileged).
    token_issuer_api_key: str = Field("", description="Bearer key required to call POST /token.")

    # Local-dev escape hatch: generate an ephemeral key and open /token. NEVER in
    # a multi-replica or production deployment (tokens won't survive a restart and
    # replicas would each publish a different key).
    token_dev_mode: bool = Field(False, description="Dev only: ephemeral key + unauthenticated /token.")


# ---------------------------------------------------------------------------
# Key loading / JWKS / minting
# ---------------------------------------------------------------------------

def _load_private_key(settings: TokenSettings) -> rsa.RSAPrivateKey:
    """Load the signing key from config, or generate one in dev mode.

    Resolution order: inline PEM → PEM file → (dev mode) generate → fail closed.
    """
    pem: Optional[bytes] = None
    if settings.token_signing_key.strip():
        pem = settings.token_signing_key.encode("utf-8")
    elif settings.token_signing_key_file.strip():
        path = Path(settings.token_signing_key_file)
        if path.is_file() and path.stat().st_size > 0:
            pem = path.read_bytes()

    if pem is not None:
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise RuntimeError("TOKEN signing key must be an RSA private key.")
        return key

    if settings.token_dev_mode:
        logger.critical(
            "TOKEN_DEV_MODE: generating an EPHEMERAL signing key. Tokens will not "
            "survive a restart and multi-replica deployments are unsupported. "
            "Never use this in production."
        )
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    raise RuntimeError(
        "No signing key configured. Set TOKEN_SIGNING_KEY or TOKEN_SIGNING_KEY_FILE "
        "(or TOKEN_DEV_MODE=true for local development)."
    )


def _compute_kid(public_key: rsa.RSAPublicKey) -> str:
    """Derive a stable key id from the public key (RFC 7638 JWK thumbprint).

    Deterministic for a given key, so every replica computes the same kid and the
    JWKS `kid` matches the `kid` header on minted tokens.
    """
    jwk = json.loads(RSAAlgorithm.to_jwk(public_key))
    canonical = json.dumps(
        {"e": jwk["e"], "kty": "RSA", "n": jwk["n"]}, separators=(",", ":"), sort_keys=True
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _build_jwks(public_key: rsa.RSAPublicKey, kid: str, algorithm: str) -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(public_key))
    jwk.update({"kid": kid, "use": "sig", "alg": algorithm})
    return {"keys": [jwk]}


def _mint(
    private_key: rsa.RSAPrivateKey,
    settings: TokenSettings,
    kid: str,
    *,
    user_name: str,
    sub: Optional[str] = None,
    ttl: Optional[int] = None,
    extra_claims: Optional[dict[str, Any]] = None,
) -> tuple[str, int]:
    """Mint a signed JWT for *user_name*. Returns (token, ttl_seconds)."""
    now = int(time.time())
    ttl_seconds = ttl or settings.token_ttl_seconds
    claims: dict[str, Any] = {
        "sub": sub or f"user:{user_name}",
        "iss": settings.token_issuer,
        "aud": settings.token_audience,
        "iat": now,
        "nbf": now,
        "exp": now + ttl_seconds,
        "user_name": user_name,
        # column_scope: empty list means no column restrictions (all columns permitted).
        # Enforcement only fires when scope is non-empty and query uses disallowed columns.
        # session_id is NOT a JWT claim — it is sent by the agent backend as X-Session-Id.
        "column_scope": json.dumps([]),
    }
    if extra_claims:
        claims.update(extra_claims)
    token = jwt.encode(
        claims, private_key, algorithm=settings.token_algorithm, headers={"kid": kid}
    )
    return token, ttl_seconds


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class TokenRequest(BaseModel):
    user_name: str = Field(..., min_length=1, description="Tenant identity to embed as 'user_name'.")
    sub: Optional[str] = Field(None, description="Override the 'sub' claim (default: user:<user_name>).")
    ttl_seconds: Optional[int] = Field(
        None, ge=1, le=86_400_000, description="Token lifetime override (1s–24h)."
    )
    claims: Optional[dict[str, Any]] = Field(None, description="Extra non-reserved claims to include.")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    user_name: str
    kid: str


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(settings: Optional[TokenSettings] = None) -> FastAPI:
    """Build the token-service FastAPI app (fails closed on bad config at startup)."""
    settings = settings or TokenSettings()

    if not settings.token_issuer.strip() or not settings.token_audience.strip():
        raise RuntimeError(
            "TOKEN_ISSUER and TOKEN_AUDIENCE are required (they become the JWT "
            "iss/aud and must match the ClickHouse API's OIDC_ISSUER/OIDC_AUDIENCE)."
        )
    if not settings.token_dev_mode and not settings.token_issuer_api_key.strip():
        raise RuntimeError(
            "TOKEN_ISSUER_API_KEY is required to protect POST /token "
            "(or set TOKEN_DEV_MODE=true for local development)."
        )

    private_key = _load_private_key(settings)
    public_key = private_key.public_key()
    kid = settings.token_kid.strip() or _compute_kid(public_key)
    jwks = _build_jwks(public_key, kid, settings.token_algorithm)
    issuer = settings.token_issuer.rstrip("/")

    bearer_scheme = HTTPBearer(auto_error=False)

    def require_issuer_key(
        credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    ) -> None:
        """Authorize POST /token with the static issuer API key (constant-time)."""
        # Dev mode with no key configured leaves /token open (local trust only).
        if settings.token_dev_mode and not settings.token_issuer_api_key.strip():
            return
        if not settings.token_issuer_api_key.strip():
            raise HTTPException(
                status_code=503,
                detail={"error": "Token issuance is not configured.", "code": "ISSUER_NOT_CONFIGURED"},
            )
        if credentials is None or not credentials.credentials:
            raise HTTPException(
                status_code=401,
                detail={"error": "Missing Authorization header.", "code": "MISSING_AUTH"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not hmac.compare_digest(
            credentials.credentials.encode("utf-8"),
            settings.token_issuer_api_key.encode("utf-8"),
        ):
            raise HTTPException(
                status_code=401,
                detail={"error": "Invalid issuer API key.", "code": "INVALID_AUTH"},
                headers={"WWW-Authenticate": "Bearer"},
            )

    app = FastAPI(
        title="Token Service",
        version="1.0.0",
        description=(
            "Issues per-user JWTs (with the user_name tenant claim) for the "
            "ClickHouse API and publishes JWKS for token validation."
        ),
    )

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/.well-known/jwks.json", tags=["oidc"])
    def jwks_endpoint() -> dict[str, Any]:
        """Public signing keys — the ClickHouse API fetches this to verify tokens."""
        return jwks

    @app.get("/.well-known/openid-configuration", tags=["oidc"])
    def discovery() -> dict[str, Any]:
        return {
            "issuer": settings.token_issuer,
            "jwks_uri": f"{issuer}/.well-known/jwks.json",
            "token_endpoint": f"{issuer}/token",
            "id_token_signing_alg_values_supported": [settings.token_algorithm],
            "response_types_supported": ["token"],
        }

    @app.post("/token", response_model=TokenResponse, tags=["token"])
    def issue_token(body: TokenRequest, _: None = Depends(require_issuer_key)) -> TokenResponse:
        """Mint a JWT for body.user_name (requires the issuer API key)."""
        token, ttl = _mint(
            private_key,
            settings,
            kid,
            user_name=body.user_name,
            sub=body.sub,
            ttl=body.ttl_seconds,
            extra_claims=body.claims,
        )
        return TokenResponse(access_token=token, expires_in=ttl, user_name=body.user_name, kid=kid)

    logger.info("Token service ready: issuer=%s audience=%s kid=%s", issuer, settings.token_audience, kid)
    return app
