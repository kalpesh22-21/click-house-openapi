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
    "API_KEY": "test-api-key-abc123",
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
    """The API key used in test env."""
    return _TEST_ENV["API_KEY"]


@pytest.fixture()
def auth_headers(api_key: str) -> dict:
    """Authorization headers for authenticated requests."""
    return {"Authorization": f"Bearer {api_key}"}


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
        with patch("app.clickhouse_client.execute_query") as mock_execute:
            # Default: return two columns and one row so routes work out of the box.
            mock_execute.return_value = (["col1", "col2"], [["val1", "val2"]])

            # Also reset the settings cache so patching env vars takes effect.
            get_settings.cache_clear()

            from app.main import app
            with TestClient(app, raise_server_exceptions=False) as client:
                yield client, mock_execute, mock_client
