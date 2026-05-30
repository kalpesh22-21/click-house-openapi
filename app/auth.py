"""Bearer token authentication dependency for FastAPI routes."""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

_bearer_scheme = HTTPBearer(auto_error=False)


def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> None:
    """FastAPI dependency that enforces Bearer token authentication.

    Uses hmac.compare_digest to prevent timing-based side-channel attacks.
    Raises HTTP 401 on missing or invalid credentials.

    Usage::

        @router.get("/some-route")
        def handler(_: None = Depends(require_api_key)):
            ...
    """
    # Fail closed: if the server has no API key configured, reject every request
    # unconditionally.  This preserves the security invariant (no empty-key
    # fail-open) without requiring the key globally at Settings construction
    # time (which would break MCP stdio mode that needs no auth).
    if not settings.api_key or not settings.api_key.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Missing Authorization header.", "code": "MISSING_AUTH"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Missing Authorization header.", "code": "MISSING_AUTH"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    # hmac.compare_digest requires both operands to be the same type.
    provided = credentials.credentials
    expected = settings.api_key

    # Explicit empty-provided-token guard: hmac.compare_digest(b"", b"") == True,
    # so reject a blank credential before the constant-time comparison.
    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Invalid API key.", "code": "INVALID_AUTH"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not hmac.compare_digest(
        provided.encode("utf-8"),
        expected.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Invalid API key.", "code": "INVALID_AUTH"},
            headers={"WWW-Authenticate": "Bearer"},
        )
