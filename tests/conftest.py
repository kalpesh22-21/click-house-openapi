"""Shared pytest fixtures for the ClickHouse API test suite.

Sets required environment variables before any app module is imported so that
pydantic-settings does not fail on missing required fields.  The values here
are fake / test-only — no real ClickHouse instance is needed.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Inject env vars early — before any app import loads pydantic-settings.
# pytest conftest.py is executed before test modules are collected, so putting
# this at module-level guarantees the env is ready when `from app.xxx import`
# statements run inside test files.
# ---------------------------------------------------------------------------

_TEST_ENV = {
    "CLICKHOUSE_HOST": "localhost",
    "CLICKHOUSE_PORT": "9000",
    "CLICKHOUSE_USER": "default",
    "CLICKHOUSE_PASSWORD": "test-password",
    "CLICKHOUSE_DATABASE": "default",
    "CLICKHOUSE_SECURE": "false",
    "API_KEY": "test-api-key-abc123",  # deprecated/unused; kept so old env stays valid
    # OIDC/JWT auth config — required for auth_configured() to be True so the app
    # starts.  The JWKS URL is never fetched in tests: the autouse _patch_jwks
    # fixture replaces the signing-key resolver with a local public key.
    "OIDC_JWKS_URL": "https://idp.test/.well-known/jwks.json",
    "OIDC_ISSUER": "https://idp.test/",
    "OIDC_AUDIENCE": "clickhouse-api",
    "JWT_ALGORITHMS": "RS256",
    "MAX_EXECUTION_TIME": "30",
    "MAX_RESULT_ROWS": "10000",
    "MAX_ROWS_TO_READ": "100000000",
    "DEFAULT_LIMIT": "1000",
    "MAX_RESPONSE_ROWS": "1000",
    "ALLOWED_DATABASES": "*",
    "APP_PORT": "8000",
    "LOG_LEVEL": "INFO",
    "PUBLIC_BASE_URL": "https://test.example.com",
}

for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)

# ---------------------------------------------------------------------------
# Now we can safely import app modules.
# ---------------------------------------------------------------------------

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from tests.jwt_helpers import PUBLIC_KEY, make_jwt  # noqa: E402


@pytest.fixture(autouse=True)
def _patch_jwks(request, monkeypatch):
    """Resolve every token's signing key to the local TEST public key (no network).

    Applied to every test so any request/middleware that validates a JWT runs
    offline.  Tokens signed with jwt_helpers.WRONG_PRIVATE_PEM still fail
    signature verification against this public key — that is intentional.

    Tests marked ``real_jwks`` opt out so they can exercise the genuine
    _resolve_signing_key -> PyJWKClient -> JWKS-fetch path (see
    tests/test_jwt_roundtrip.py).
    """
    if request.node.get_closest_marker("real_jwks"):
        yield
        return
    monkeypatch.setattr(
        "app.auth_jwt._resolve_signing_key",
        lambda token, settings: PUBLIC_KEY,
    )
    yield


@pytest.fixture(autouse=True)
def _reset_mcp_session_manager():
    """Give every test a FRESH MCP streamable-HTTP session manager.

    FastMCP lazily creates and caches ONE ``StreamableHTTPSessionManager`` on the
    module-global ``mcp`` object (``streamable_http_app()`` reuses it). That
    manager's ``.run()`` — entered by a ``TestClient`` lifespan — is
    once-per-instance, so a second test that drives ``mcp.streamable_http_app()``
    (e.g. the full-app auth/binding tests) would hit
    ``RuntimeError: .run() can only be called once per instance``. Nulling the
    cache before/after each test forces a fresh manager, restoring test
    isolation. Import is lazy + guarded so this stays inert for tests that never
    touch the MCP app.
    """
    try:
        from app.mcp_server import mcp
    except Exception:  # noqa: BLE001 — never let a fixture import break collection
        yield
        return
    mcp._session_manager = None
    yield
    mcp._session_manager = None


@pytest.fixture(autouse=False)
def reset_settings_cache():
    """Clear the lru_cache on get_settings between tests that mutate env vars."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def settings():
    """Return a Settings instance built from the test env."""
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture()
def api_key() -> str:
    """Deprecated — retained for backward-compat with older tests."""
    return _TEST_ENV["API_KEY"]


@pytest.fixture()
def auth_headers() -> dict:
    """Authorization headers carrying a valid tenant JWT (user_name='alice')."""
    return {"Authorization": f"Bearer {make_jwt()}"}


# ---------------------------------------------------------------------------
# TestClient fixture — patches clickhouse_client so no real CH is needed.
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_ch_client(mocker=None):
    """
    Return a factory that patches execute_query and ping so tests can control
    what ClickHouse 'returns' without a live connection.

    Usage in tests: just import and use `mock_execute_query` / `mock_ping`
    directly via unittest.mock.patch — this fixture provides a simpler
    pre-built alternative.
    """
    # We expose this as a plain object with replaceable callables so tests can
    # configure it per-case.  The actual patching is done in each test file
    # using unittest.mock.patch to keep dependencies explicit.
    pass


@pytest.fixture()
def test_client():
    """
    TestClient that does NOT call the real lifespan (which tries to connect to
    ClickHouse).  We patch get_client so the lifespan succeeds without a real
    server, then yield the client.
    """
    from unittest.mock import MagicMock, patch

    # Patch get_client at the module level used by main.py lifespan AND routers.
    mock_client = MagicMock()
    mock_client.ping.return_value = True

    with patch("app.clickhouse_client.get_client", return_value=mock_client):
        with patch("app.service.execute_query") as mock_execute:
            # Default: return two columns and one row so routes work out of the box.
            mock_execute.return_value = (["col1", "col2"], [["val1", "val2"]])

            # Also reset the settings cache so patching env vars takes effect.
            get_settings.cache_clear()

            from app.main import app
            with TestClient(app, raise_server_exceptions=False) as client:
                yield client, mock_execute, mock_client
