"""JWT authentication dependency for FastAPI routes.

Replaces the former single shared-API-key check with per-request JWT validation
(see app/auth_jwt.py).  On success the authenticated Principal is bound to a
context variable for the duration of the request so the query path can inject
per-tenant ClickHouse settings (see app/clickhouse_client.py).

Why an async-generator dependency (not a plain function):
  The sync route handlers run on anyio threadpool workers.  A ContextVar set
  inside an *async* dependency is set in the request task's context, which anyio
  copies into the worker thread — so the principal is visible in execute_query.
  A *sync* dependency would set it in a throwaway worker context that is
  discarded on return.  The ``finally`` after ``yield`` resets it (via the token)
  after the response so it can never leak onto a pooled thread.
"""

from __future__ import annotations

from typing import AsyncGenerator

import anyio
from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth_jwt import JWTAuthError, validate_token
from app.catalog_scope import resolve_principal_permissions
from app.config import Settings, get_settings
from app.errors import ClickHouseUnavailableError
from app.principal import Principal, current_principal

_bearer_scheme = HTTPBearer(auto_error=False)


async def require_principal(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> AsyncGenerator[Principal, None]:
    """FastAPI dependency that authenticates the request via JWT.

    Yields the validated :class:`Principal` (also bound to ``current_principal``
    for the request) and resets the context variable afterwards.

    Raises HTTP 401/403/503 (mapped from :class:`JWTAuthError`) on any failure.
    """
    # Fail closed: a network-exposed REST API must not start serving without a
    # way to verify tokens.  The lifespan check in main.py is the primary guard;
    # this is a per-request second line of defence.
    if not settings.auth_configured():
        raise HTTPException(
            status_code=503,
            detail={"error": "Authentication is not configured.", "code": "AUTH_NOT_CONFIGURED"},
        )

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=401,
            detail={"error": "Missing Authorization header.", "code": "MISSING_AUTH"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        principal = validate_token(credentials.credentials, settings)
    except JWTAuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": exc.message, "code": exc.code},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        principal = await anyio.to_thread.run_sync(resolve_principal_permissions, principal)
    except ClickHouseUnavailableError as exc:
        raise HTTPException(status_code=503, detail={"error": exc.message, "code": exc.code}) from exc

    token = current_principal.set(principal)
    try:
        yield principal
    finally:
        current_principal.reset(token)
