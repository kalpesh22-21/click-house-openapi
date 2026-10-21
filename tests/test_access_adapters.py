"""JWT and SQL adapters run per request, intersect, and fail closed."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from app import catalog_scope as access
from app.catalog_scope import CatalogScope
from app.errors import ClickHouseUnavailableError
from app.principal import Principal, current_principal


@pytest.mark.parametrize("left,right,expected", [
    (CatalogScope(), CatalogScope(), CatalogScope()),
    (CatalogScope(columns=frozenset()), CatalogScope(), CatalogScope(columns=frozenset())),
    (CatalogScope(), CatalogScope(schemas=frozenset()), CatalogScope(schemas=frozenset())),
    (CatalogScope(schemas=frozenset({"dbpcm_datawarehouse"})),
     CatalogScope(schemas=frozenset({"dbpcm_warehouse"})),
     CatalogScope(schemas=frozenset({"dbpcm_warehouse"}))),
    (CatalogScope(columns=frozenset({"dbpcm_datawarehouse.t.a", "dbpcm_warehouse.t.b"})),
     CatalogScope(columns=frozenset({"dbpcm_warehouse.t.a"})),
     CatalogScope(columns=frozenset({"dbpcm_warehouse.t.a"}))),
])
def test_intersection(left, right, expected):
    assert access.intersect_scopes(left, right) == expected
    assert access.intersect_scopes(right, left) == expected


def test_both_stubs_run_once_for_each_request(monkeypatch):
    jwt = MagicMock(return_value=CatalogScope())
    sql = MagicMock(return_value=CatalogScope())
    monkeypatch.setattr(access, "jwt_permission_scope", jwt)
    monkeypatch.setattr(access, "sql_permission_scope", sql)
    user = Principal("alice")
    for _ in range(2):
        assert access.resolve_principal_permissions(user).catalog_scope == CatalogScope()
    assert jwt.call_count == sql.call_count == 2
    assert user.catalog_scope is None


@pytest.mark.parametrize("adapter", ["jwt_permission_scope", "sql_permission_scope"])
@pytest.mark.parametrize("value", [None, CatalogScope(columns="bad"), CatalogScope(columns=frozenset({"bad"}))])
def test_bad_adapter_fails_closed_and_restores_identity(monkeypatch, adapter, value):
    original = Principal("outer")
    token = current_principal.set(original)
    monkeypatch.setattr(access, adapter, lambda _: value)
    try:
        with pytest.raises(ClickHouseUnavailableError) as exc:
            access.resolve_principal_permissions(Principal("alice"))
        assert exc.value.code == "SCOPE_RESOLUTION_UNAVAILABLE"
        assert current_principal.get() is original
    finally:
        current_principal.reset(token)


def test_sql_adapter_receives_isolated_request_identity(monkeypatch):
    def sql(user):
        assert current_principal.get() is user
        return CatalogScope(columns=frozenset({f"db.table.{user.subject}"}))
    monkeypatch.setattr(access, "sql_permission_scope", sql)
    users = [Principal("alice"), Principal("bob")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(access.resolve_principal_permissions, users))
    assert results[0].catalog_scope.columns == frozenset({"db.table.alice"})
    assert results[1].catalog_scope.columns == frozenset({"db.table.bob"})
    assert current_principal.get() is None


def test_token_is_not_in_principal_repr():
    assert "private-bearer" not in repr(Principal("alice", token="private-bearer"))
