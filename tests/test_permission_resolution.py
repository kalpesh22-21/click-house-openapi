"""Server permissions replace JWT column grants on REST and MCP."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app import catalog_scope, service
from app.auth import require_principal
from app.auth_jwt import validate_token
from app.catalog_scope import (
    CatalogScope,
    resolve_catalog_scope,
    resolve_principal_permissions,
)
from app.config import get_settings
from app.errors import ClickHouseUnavailableError
from app.mcp_server import JWTAuthMiddleware
from app.principal import Principal, current_principal
from app.token_service import TokenRequest, TokenSettings, _mint
from tests.jwt_helpers import _PRIVATE_KEY, PUBLIC_KEY, make_jwt


@pytest.mark.parametrize("legacy", [None, "not-json", 123, [], ["db.table.private"]])
def test_jwt_permission_claim_is_ignored(legacy):
    principal = validate_token(
        make_jwt(include_column_scope=False, extra_claims={"column_scope": legacy})
    )
    assert "column_scope" not in principal.claims
    assert not hasattr(principal, "column_scope")
    assert principal.catalog_scope is None
    assert resolve_principal_permissions(principal).catalog_scope == CatalogScope()


def test_jwt_without_scope_is_valid():
    principal = validate_token(make_jwt(include_column_scope=False))
    assert principal.subject == "user-1"
    assert principal.catalog_scope is None


def test_dummy_grants_whole_catalog_without_permission_backend_calls(monkeypatch):
    execute = MagicMock(side_effect=AssertionError("metadata I/O"))
    monkeypatch.setattr(service, "_execute", execute)
    # Column enrichment is separate from permission resolution and may perform I/O.
    context_lookup = MagicMock(return_value=(["context_key", "earn_code"], []))
    monkeypatch.setattr("app.catalog_context.execute_context_sql", context_lookup)
    principal = resolve_principal_permissions(Principal("user"))
    token = current_principal.set(principal)
    try:
        catalog = service.get_semantic_catalog()
        scoped = catalog_scope.scoped_catalog(catalog)
        assert set(scoped) == set(catalog)
        for key, entry in catalog.items():
            assert set(scoped[key]["columns"]) == set(
                entry["columns"]
            ) - service.hidden_columns_for(catalog, key)
        assert service.list_databases()
        for entry in catalog.values():
            assert service.get_table_schema(entry["database"], entry["table"])[
                "columns"
            ]
        execute.assert_not_called()
    finally:
        current_principal.reset(token)


@pytest.mark.parametrize(
    "result",
    [None, {}, CatalogScope(columns="bad"), CatalogScope(schemas=frozenset({123}))],
)
def test_malformed_permission_results_fail_closed(monkeypatch, result):
    monkeypatch.setattr(catalog_scope, "query_permission_scope", lambda _: result)
    with pytest.raises(ClickHouseUnavailableError) as caught:
        resolve_principal_permissions(Principal("u"))
    assert caught.value.code == "SCOPE_RESOLUTION_UNAVAILABLE"


def _app(transport):
    def result():
        # Repeated use during one request must not repeat the permission query.
        first = resolve_catalog_scope()
        assert first == resolve_catalog_scope()
        return {"columns": sorted(first.columns) if first.columns is not None else None}

    if transport == "rest":
        app = FastAPI()
        app.get("/inspect", dependencies=[Depends(require_principal)])(result)
        return app

    async def endpoint(request):
        return JSONResponse(result())

    return JWTAuthMiddleware(
        Starlette(routes=[Route("/inspect", endpoint)]), settings=get_settings()
    )


def test_session_grants_reused_without_running_adapters(monkeypatch):
    from app.session_catalog import materialize, verify_scope
    from tests.catalog_helpers import authored_catalog
    catalog = authored_catalog({"db.t": {"alice": "String", "bob": "String"}})
    monkeypatch.setattr("app.session_catalog.get_semantic_catalog", lambda: catalog)
    resolver = MagicMock(side_effect=lambda p: CatalogScope(columns=frozenset({f"db.t.{p.subject}"})))
    monkeypatch.setattr(catalog_scope, "query_permission_scope", resolver)
    for user in ("alice", "bob"):
        principal = Principal(user, {"sub": user})
        snapshot = materialize(principal, user, get_settings())
        for _ in range(2):
            verified = verify_scope(principal, user, snapshot["scope_token"], get_settings())
            assert verified.catalog_scope.columns == frozenset({f"db.t.{user}"})
    assert resolver.call_count == 2
    assert current_principal.get() is None


def test_permission_failure_blocks_session_initialization(monkeypatch):
    from app.session_catalog import materialize
    monkeypatch.setattr(catalog_scope, "query_permission_scope", MagicMock(side_effect=RuntimeError("private authorization SQL")))
    with pytest.raises(ClickHouseUnavailableError) as caught:
        materialize(Principal("alice"), "session", get_settings())
    assert caught.value.code == "SCOPE_RESOLUTION_UNAVAILABLE"
    assert "private authorization SQL" not in str(caught.value)
    assert current_principal.get() is None


def test_token_issuer_never_mints_scope_and_preserves_binding():
    from app.session_binding import sid_hash

    settings = TokenSettings(_env_file=None)
    token, _ = _mint(
        _PRIVATE_KEY,
        settings,
        "test",
        user_name="alice",
        session_id="session",
        extra_claims={"column_scope": ["injected"]},
    )
    claims = jwt.decode(
        token, PUBLIC_KEY, algorithms=["RS256"], options={"verify_aud": False}
    )
    assert "column_scope" not in claims
    assert claims["sid_hash"] == sid_hash("session")
    assert "column_scope" not in TokenRequest.model_json_schema()["properties"]
    with pytest.raises(ValidationError):
        TokenRequest(user_name="alice", column_scope=[])
    with pytest.raises(ValidationError):
        TokenRequest(user_name="alice", claims={"column_scope": []})


def test_sample_projects_catalog_columns_without_introspection(monkeypatch):
    from tests.catalog_helpers import authored_catalog

    catalog = authored_catalog(
        {"db.t": {"id": "UInt64", "name": "String", "tenant": "String"}}
    )
    catalog["db.t"]["mcp_projection"] = {"hidden_columns": ["tenant"]}
    monkeypatch.setattr(service, "get_semantic_catalog", lambda: catalog)
    execute = MagicMock(return_value=(["id", "name"], [[1, "A"]]))
    monkeypatch.setattr(service, "_execute", execute)
    assert service.list_tables("db") == [
        {"database": "db", "name": "t", "engine": "ClickHouse"}
    ]
    assert service.get_table_schema("db", "t")["catalogued"]
    execute.assert_not_called()
    service.sample_rows("db", "t")
    execute.assert_called_once()
    assert execute.call_args.args[0] == "SELECT `id`, `name` FROM `db`.`t` LIMIT 5"
