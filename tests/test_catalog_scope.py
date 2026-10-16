from unittest.mock import MagicMock

import pytest

from app import service, trino_metadata
from app.catalog_scope import CatalogScope, resolve_catalog_scope, scoped_catalog
from app.config import Settings
from app.errors import ColumnScopeError
from app.principal import Principal, current_principal


@pytest.fixture
def scoped_user():
    tokens = []

    def bind(scope):
        tokens.append(
            current_principal.set(
                Principal("user", catalog_scope=scope)
            )
        )

    yield bind
    for token in reversed(tokens):
        current_principal.reset(token)


def test_scope_adapter_distinguishes_empty_grants(scoped_user):
    scoped_user(None)
    assert resolve_catalog_scope().columns is None
    scoped_user(CatalogScope(columns=frozenset()))
    assert resolve_catalog_scope().columns == frozenset()


def test_schema_denial_overrides_column_grants(scoped_user):
    scoped_user(CatalogScope(schemas=frozenset(), columns=frozenset({"db.t.id"})))
    assert not resolve_catalog_scope().allows_column("db.t", "id")
    assert scoped_catalog({"db.t": {"columns": {"id": {"type": "String"}}}}) == {}


@pytest.mark.parametrize(
    "sql,scope",
    [
        ("SELECT employee_code FROM employee", CatalogScope(schemas=frozenset())),
        ("SELECT count() FROM employee", CatalogScope(columns=frozenset())),
        (
            "SELECT employee_code FROM employee WHERE first_name = 'A'",
            CatalogScope(columns=frozenset({"dbpcm_warehouse.employee.employee_code"})),
        ),
        (
            "SELECT * FROM employee",
            CatalogScope(columns=frozenset({"dbpcm_warehouse.employee.employee_code"})),
        ),
    ],
)
def test_rejected_scope_never_executes_or_discovers(
    monkeypatch, scoped_user, sql, scope
):
    scoped_user(scope)
    settings = Settings(trino_enabled=True, trino_host="test", _env_file=None)
    monkeypatch.setattr("app.catalog.get_settings", lambda: settings)
    execute = MagicMock(side_effect=AssertionError("business SQL executed"))
    discover = MagicMock(side_effect=AssertionError("discovery called"))
    monkeypatch.setattr(service, "_execute", execute)
    monkeypatch.setattr(trino_metadata, "discover", discover)
    with pytest.raises(ColumnScopeError):
        service.run_query(sql, settings=settings, include_provenance=True)
    execute.assert_not_called()
    discover.assert_not_called()


def test_allowed_query_uses_catalog_on_every_request(monkeypatch, scoped_user):
    scoped_user(
        CatalogScope(
            schemas=frozenset({"dbpcm_datawarehouse"}),
            columns=frozenset({"dbpcm_warehouse.employee.employee_code"}),
        )
    )
    settings = Settings(trino_enabled=True, trino_host="test", _env_file=None)
    monkeypatch.setattr("app.catalog.get_settings", lambda: settings)
    execute = MagicMock(return_value=(["employee_code"], [["E1"]]))
    monkeypatch.setattr(service, "_execute", execute)
    discover = MagicMock(side_effect=AssertionError("discovery called"))
    monkeypatch.setattr(trino_metadata, "discover", discover)
    for _ in range(2):
        result = service.run_query(
            "SELECT employee_code FROM employee",
            settings=settings,
            include_provenance=True,
        )
        assert result["row_count"] == 1
    assert execute.call_count == 2
    discover.assert_not_called()
    assert [
        t["name"] for t in service.list_tables("dbpcm_datawarehouse", settings)
    ] == ["employee"]
    schema = service.get_table_schema("dbpcm_datawarehouse", "employee", settings)
    assert [c["name"] for c in schema["columns"]] == ["employee_code"]


def test_direct_metadata_hides_schemas_without_column_grants(monkeypatch, scoped_user):
    scoped_user(CatalogScope(columns=frozenset()))
    settings = Settings(
        clickhouse_host="test",
        clickhouse_user="test",
        allowed_databases="*",
        _env_file=None,
    )
    monkeypatch.setattr(
        service, "_execute", MagicMock(return_value=(["name"], [["dbpcm_warehouse"]]))
    )
    assert service.list_databases(settings) == []


def test_unknown_grant_cannot_authorize_table_count(monkeypatch, scoped_user):
    scoped_user(
        CatalogScope(columns=frozenset({"dbpcm_warehouse.employee.nonexistent"}))
    )
    settings = Settings(trino_enabled=True, trino_host="test", _env_file=None)
    monkeypatch.setattr("app.catalog.get_settings", lambda: settings)
    execute = MagicMock(side_effect=AssertionError("business SQL executed"))
    monkeypatch.setattr(service, "_execute", execute)
    with pytest.raises(ColumnScopeError):
        service.run_query(
            "SELECT count() FROM employee", settings=settings, include_provenance=True
        )
    execute.assert_not_called()
