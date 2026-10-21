"""Materialize permissions once; authenticate the resulting scope on every call."""
from __future__ import annotations

import base64
import hashlib
import json
import time
import zlib
import uuid
from dataclasses import replace

import jwt

from app.catalog_scope import CatalogScope, resolve_principal_permissions, scoped_catalog
from app.config import Settings
from app.principal import Principal, current_principal
from app.semantic_catalog import get_catalog_sha, get_semantic_catalog
from app.semantic_catalog.export import build_catalog_export_from

SCOPE_HEADER = "X-Catalog-Scope"
SESSION_PATH = "/session/catalog"


class SessionScopeError(Exception):
    def __init__(self, code: str, status: int = 403):
        self.code, self.status = code, status
        self.message = "Session catalog is unavailable; initialize or refresh the session catalog."
        super().__init__(self.message)


def owner_hash(claims: dict) -> str:
    # Access-token renewal must not invalidate a session. All other verified
    # identity/authorization claims are bound, including tenant and stable JTI.
    stable = {k: v for k, v in claims.items() if k not in {"exp", "iat", "nbf", "sid_hash", "session_id", "column_scope"}}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _key(settings: Settings) -> str:
    if not settings.mcp_service_key:
        raise SessionScopeError("SESSION_SCOPE_NOT_CONFIGURED", 503)
    return settings.mcp_service_key


def materialize(principal: Principal, session_id: str | None, settings: Settings) -> dict:
    key = _key(settings)
    if not session_id or len(session_id) > 256:
        raise SessionScopeError("SESSION_ID_REQUIRED", 400)
    scoped = resolve_principal_permissions(principal)
    token = current_principal.set(scoped)
    try:
        catalog = scoped_catalog(get_semantic_catalog())
    finally:
        current_principal.reset(token)
    # Freeze explicit grants: an allow-all adapter never grants columns added
    # after initialization. SQL definitions remain server-only in the export.
    grants = sorted(f"{table}.{column}" for table, entry in catalog.items() for column in entry["columns"])
    encoded_grants = json.dumps(grants).encode()
    if len(encoded_grants) > 524288:
        raise SessionScopeError("SESSION_SCOPE_TOO_LARGE", 503)
    source_sha = get_catalog_sha()
    export = build_catalog_export_from(catalog, source_sha, trino_enabled=settings.trino_enabled)
    snapshot_sha = hashlib.sha256(json.dumps(export["catalog"], sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    export["catalog_sha"] = snapshot_sha
    now = int(time.time())
    expires = now + settings.session_scope_ttl_seconds
    owner = owner_hash(principal.claims)
    payload = dict(iss="mcp-session-scope", aud="mcp-session-scope", sub=principal.subject,
                   sid=session_id, owner=owner, iat=now, exp=expires, source_sha=source_sha,
                   catalog_sha=snapshot_sha, version=1, jti=uuid.uuid4().hex,
                   grants=base64.urlsafe_b64encode(zlib.compress(encoded_grants)).decode())
    signed = jwt.encode(payload, key, algorithm="HS256", headers={"typ": "mcp-session-scope+jwt"})
    return {**export, "source_sha": source_sha, "scope_token": signed, "expires_at": expires,
            "owner": owner, "session_id": session_id, "columns": grants}


def verify_scope(principal: Principal, session_id: str | None, signed: str | None, settings: Settings) -> Principal:
    key = _key(settings)
    if not signed or not session_id:
        raise SessionScopeError("SESSION_SCOPE_REQUIRED")
    try:
        payload = jwt.decode(signed, key, algorithms=["HS256"], audience="mcp-session-scope",
                             issuer="mcp-session-scope", options={"require": ["exp", "iat", "sub", "sid", "owner", "source_sha", "grants", "catalog_sha"]})
        if (jwt.get_unverified_header(signed).get("typ") != "mcp-session-scope+jwt"
                or payload.get("version") != 1 or payload["sid"] != session_id
                or payload["sub"] != principal.subject or payload["owner"] != owner_hash(principal.claims)):
            raise ValueError("Scope binding mismatch")
        if payload["source_sha"] != get_catalog_sha():
            raise SessionScopeError("SESSION_SCOPE_STALE")
        decoder = zlib.decompressobj()
        raw = decoder.decompress(base64.urlsafe_b64decode(payload["grants"]), 524289)
        if len(raw) > 524288 or not decoder.eof or decoder.unused_data:
            raise ValueError("Invalid grants")
        grants = json.loads(raw)
        if not isinstance(grants, list) or not all(isinstance(g, str) and len(g.split('.')) == 3 for g in grants):
            raise ValueError("Invalid grants")
    except SessionScopeError:
        raise
    except jwt.ExpiredSignatureError:
        raise SessionScopeError("SESSION_SCOPE_EXPIRED") from None
    except Exception:
        raise SessionScopeError("SESSION_SCOPE_INVALID") from None
    return replace(principal, catalog_scope=CatalogScope(columns=frozenset(grants)))
