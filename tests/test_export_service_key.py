"""Tests for the static service-API-key auth on the MCP export side-channels.

The three scope-independent export routes — ``/catalog/export``,
``/blueprints/export``, ``/knowledge/export`` (all ``@mcp.custom_route`` handlers
on the MCP HTTP app) — must accept EITHER a valid user JWT (as today) OR a static
service API key presented via the ``X-Service-Key`` header. This lets a trusted
system hydrator fetch them without a user JWT.

Contract (see JWTAuthMiddleware in app/mcp_server.py):
  - The key is a static shared secret read from ``settings.mcp_service_key``.
  - A matching ``X-Service-Key`` (constant-time compared with
    ``hmac.compare_digest``) allows the request through — EXPORT PATHS ONLY, and
    without binding any principal/scope/session (the export handlers read none).
  - An absent/mismatched key does NOT reject: it falls through to the existing
    JWT path, so a valid user JWT still authenticates (EITHER-OR).
  - An EMPTY configured key (the default) is fail-closed: an ``X-Service-Key`` of
    any value can never match, so the route still requires a JWT.
  - The bypass is strictly export-paths-only — a non-export path is unaffected.

These drive the SAME ``JWTAuthMiddleware``-wrapped streamable-HTTP app the runtime
hits, mirroring tests/test_catalog_export.py.
"""

from __future__ import annotations

import inspect

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
from app.mcp_server import JWTAuthMiddleware, mcp
from tests.jwt_helpers import make_jwt

SERVICE_KEY = "svc-secret-key-abc123"
WRONG_SERVICE_KEY = "svc-secret-key-WRONG"

EXPORT_PATHS = ("/catalog/export", "/blueprints/export", "/knowledge/export")


def _settings(service_key: str):
    """Return a Settings copy with ``mcp_service_key`` overridden (global cache untouched)."""
    return get_settings().model_copy(update={"mcp_service_key": service_key})


def _client(service_key: str) -> TestClient:
    """MCP streamable-HTTP app behind JWTAuthMiddleware with the given configured key.

    Custom routes are served without entering the MCP lifespan (same as
    tests/test_catalog_export.py) — no ``with`` block needed.
    """
    app = JWTAuthMiddleware(mcp.streamable_http_app(), settings=_settings(service_key))
    return TestClient(app, raise_server_exceptions=False)


AUTH = {"Authorization": f"Bearer {make_jwt()}"}


# ===========================================================================
# 1. Configured key: X-Service-Key is an EITHER-OR alternative to a JWT
# ===========================================================================

class TestServiceKeyConfigured:

    @pytest.mark.parametrize("path", EXPORT_PATHS)
    def test_matching_service_key_no_jwt_returns_200(self, path):
        """A matching X-Service-Key with NO JWT authenticates the export route."""
        resp = _client(SERVICE_KEY).get(path, headers={"X-Service-Key": SERVICE_KEY})
        assert resp.status_code == 200, resp.text
        assert isinstance(resp.json(), dict) and resp.json()

    @pytest.mark.parametrize("path", EXPORT_PATHS)
    def test_matching_service_key_body_matches_builder(self, path):
        """The service-key path returns the same body the JWT path does."""
        via_key = _client(SERVICE_KEY).get(path, headers={"X-Service-Key": SERVICE_KEY})
        via_jwt = _client(SERVICE_KEY).get(path, headers=AUTH)
        assert via_key.status_code == 200
        assert via_jwt.status_code == 200
        assert via_key.content == via_jwt.content

    @pytest.mark.parametrize("path", EXPORT_PATHS)
    def test_wrong_service_key_no_jwt_returns_401(self, path):
        """A WRONG X-Service-Key with no JWT falls through to JWT auth → 401."""
        resp = _client(SERVICE_KEY).get(path, headers={"X-Service-Key": WRONG_SERVICE_KEY})
        assert resp.status_code == 401
        assert resp.json()["code"] == "MISSING_AUTH"

    @pytest.mark.parametrize("path", EXPORT_PATHS)
    def test_valid_jwt_no_service_key_returns_200(self, path):
        """A valid user JWT with no service key still authenticates (unchanged)."""
        resp = _client(SERVICE_KEY).get(path, headers=AUTH)
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("path", EXPORT_PATHS)
    def test_both_service_key_and_jwt_returns_200(self, path):
        """Presenting BOTH a matching service key and a valid JWT → 200."""
        headers = {"X-Service-Key": SERVICE_KEY, **AUTH}
        resp = _client(SERVICE_KEY).get(path, headers=headers)
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("path", EXPORT_PATHS)
    def test_wrong_service_key_but_valid_jwt_returns_200(self, path):
        """A WRONG service key does not reject when a valid JWT is also present."""
        headers = {"X-Service-Key": WRONG_SERVICE_KEY, **AUTH}
        resp = _client(SERVICE_KEY).get(path, headers=headers)
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("path", EXPORT_PATHS)
    def test_no_credentials_returns_401(self, path):
        """No JWT and no service key → default-deny 401 (unchanged)."""
        resp = _client(SERVICE_KEY).get(path)
        assert resp.status_code == 401
        assert resp.json()["code"] == "MISSING_AUTH"


# ===========================================================================
# 2. Empty configured key: fail-closed — X-Service-Key can NEVER authenticate
# ===========================================================================

class TestServiceKeyEmptyFailClosed:

    @pytest.mark.parametrize("path", EXPORT_PATHS)
    def test_service_key_header_ignored_when_config_empty(self, path):
        """With an EMPTY configured key, any X-Service-Key still requires a JWT → 401."""
        resp = _client("").get(path, headers={"X-Service-Key": SERVICE_KEY})
        assert resp.status_code == 401
        assert resp.json()["code"] == "MISSING_AUTH"

    @pytest.mark.parametrize("path", EXPORT_PATHS)
    def test_empty_service_key_header_when_config_empty(self, path):
        """An empty X-Service-Key against an empty config never matches → 401."""
        resp = _client("").get(path, headers={"X-Service-Key": ""})
        assert resp.status_code == 401

    @pytest.mark.parametrize("path", EXPORT_PATHS)
    def test_valid_jwt_still_works_when_config_empty(self, path):
        """The default (empty key) leaves the existing JWT path fully intact."""
        resp = _client("").get(path, headers=AUTH)
        assert resp.status_code == 200, resp.text


# ===========================================================================
# 3. The bypass is strictly EXPORT-PATHS-ONLY
# ===========================================================================

class TestBypassScopedToExportPaths:

    def test_health_is_public_regardless(self):
        """/health is public — unaffected by the service-key branch (sanity)."""
        resp = _client(SERVICE_KEY).get("/health", headers={"X-Service-Key": SERVICE_KEY})
        # /health is in the public_paths allowlist, so it is served without auth.
        assert resp.status_code != 401

    def test_non_export_path_service_key_does_not_bypass(self):
        """A non-export, non-public path with a matching X-Service-Key is NOT bypassed.

        The MCP tool endpoint (settings.mcp_path, default /mcp) is default-deny; a
        service key must not authenticate it — only a JWT can. Without a JWT the
        request is rejected (401) even though the service key matches.
        """
        mcp_path = get_settings().mcp_path
        resp = _client(SERVICE_KEY).get(mcp_path, headers={"X-Service-Key": SERVICE_KEY})
        assert resp.status_code == 401


# ===========================================================================
# 4. Constant-time compare (code inspection — timing is not directly testable)
# ===========================================================================

class TestConstantTimeCompare:

    def test_middleware_uses_hmac_compare_digest(self):
        """The service-key compare must go through hmac.compare_digest."""
        src = inspect.getsource(JWTAuthMiddleware.__call__)
        assert "hmac.compare_digest" in src

    def test_export_paths_frozenset_built_once(self):
        """The export-paths allowlist is a frozenset built in __init__ (not per request)."""
        mw = JWTAuthMiddleware(mcp.streamable_http_app(), settings=_settings(SERVICE_KEY))
        assert isinstance(mw._export_paths, frozenset)
        assert mw._export_paths == frozenset(EXPORT_PATHS)


# ===========================================================================
# 5. Non-ASCII X-Service-Key must NOT crash the middleware (locks the fix)
# ===========================================================================

async def _drive_asgi(app, path: str, service_key_bytes: bytes):
    """Drive a pure-ASGI app with a raw scope, returning (status, body_bytes).

    The httpx TestClient refuses non-ASCII header VALUES, so to deliver a byte in
    the 0x80–0xFF range (legal after the middleware's latin-1 decode, and attacker-
    deliverable) we hand-build the ASGI scope and pump receive/send ourselves.
    """
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "headers": [(b"x-service-key", service_key_bytes)],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    captured: dict = {"status": None, "body": b""}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
        elif message["type"] == "http.response.body":
            captured["body"] += message.get("body", b"")

    await app(scope, receive, send)
    return captured["status"], captured["body"]


class TestNonAsciiServiceKey:

    @pytest.mark.parametrize("path", EXPORT_PATHS)
    def test_non_ascii_header_does_not_500(self, path):
        """A non-ASCII X-Service-Key must fall through to JWT auth, not raise/500.

        0xE9 ('é' in latin-1) is a legal, attacker-deliverable header byte. The
        compare is on utf-8 bytes so it cannot raise TypeError; with no JWT the
        request falls through to the default-deny JWT path → 401 (never 500).
        """
        import anyio

        app = JWTAuthMiddleware(mcp.streamable_http_app(), settings=_settings(SERVICE_KEY))
        status, body = anyio.run(_drive_asgi, app, path, b"\xe9")
        assert status == 401
        import json as _json

        assert _json.loads(body)["code"] == "MISSING_AUTH"


# ===========================================================================
# 6. Path-normalization variants — the auth-check and router-dispatch paths
#    must not diverge (feature ON, no JWT: never a 200 export body)
# ===========================================================================

class TestPathNormalizationVariants:

    @pytest.mark.parametrize(
        "path",
        [
            "/catalog/export/",  # trailing slash
            "//catalog/export",  # doubled leading slash
            "/CATALOG/EXPORT",  # case variant
            "/catalog/export/../scratch/v1/materialize",  # dot-segment traversal
            "/catalog/export/%2e%2e/scratch/v1/materialize",  # %2e-encoded dot-segment
        ],
    )
    def test_variant_never_diverges_onto_a_non_export_route(self, path):
        """The auth-check and router-dispatch paths must not diverge for any variant.

        Feature is ON and a MATCHING service key is presented. The safety invariant
        the service-key bypass depends on is that a path the middleware treats as an
        export (and bypasses) resolves in the ROUTER to that SAME export handler —
        never to some OTHER route. So each variant must be EITHER:
          - denied / redirected (401 / 404 / 405 / 307) — the middleware did not
            bypass and the router required a JWT it did not get, or the router had
            no matching route; OR
          - a 200 whose body is byte-identical to the canonical export — i.e. the
            variant is merely a normalized alias of the intended export route (the
            trailing-slash case), which is the safe, consistent outcome.
        What must NEVER happen: a 200 that is a DIFFERENT (non-export) route's body,
        which would mean the bypass leaked onto another endpoint.
        """
        # Canonical catalog-export body served via the same service key (all variants
        # here are derived from /catalog/export, so this is the only legitimate 200).
        canonical = _client(SERVICE_KEY).get(
            "/catalog/export", headers={"X-Service-Key": SERVICE_KEY}
        )
        assert canonical.status_code == 200

        resp = _client(SERVICE_KEY).get(path, headers={"X-Service-Key": SERVICE_KEY})
        if resp.status_code == 200:
            # A 200 is only acceptable if it is the SAME export route (a normalized
            # alias), never a divergent non-export route.
            assert resp.content == canonical.content, (
                f"{path} served a 200 that is NOT the canonical export body — "
                f"the bypass may have leaked onto a different route"
            )
        else:
            assert resp.status_code in (401, 404, 405, 307), (
                f"{path} → {resp.status_code}: {resp.text[:200]}"
            )
