from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app import data_freshness
from app.principal import Principal, current_principal


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([["employee", 1791871200000], ["payroll", 1791783000000]], "2026-10-12T05:30:00.000Z"),
        ([["employee", 1791871200000]], None),
        ([], None),
    ],
)
def test_scoped_complete_union(monkeypatch, rows, expected):
    execute = Mock(return_value=([], rows))
    monkeypatch.setattr(data_freshness, "execute_query", execute)
    settings = SimpleNamespace(
        trino_enabled=False,
        clickhouse_tenant_settings={},
        clickhouse_database="dbpcm_warehouse",
        scratch_database="scratch",
    )
    token = current_principal.set(
        Principal("test", {"proc_center": "PC01", "clientcode": "CLIENT_A"})
    )
    try:
        result = data_freshness.data_as_of(
            "SELECT * FROM employee e JOIN payroll p ON e.EmployeeCode=p.EmployeeCode", settings
        )
    finally:
        current_principal.reset(token)
    assert result == expected
    sql, _, parameters = execute.call_args.args
    assert " FINAL " in sql
    assert parameters == {
        "fresh_pc": "PC01",
        "fresh_client": "CLIENT_A",
        "fresh_tables": ["employee", "payroll"],
    }


def test_missing_principal_never_queries(monkeypatch):
    execute = Mock()
    monkeypatch.setattr(data_freshness, "execute_query", execute)
    token = current_principal.set(None)
    try:
        assert data_freshness.data_as_of("SELECT * FROM employee", SimpleNamespace()) is None
    finally:
        current_principal.reset(token)
    execute.assert_not_called()


@pytest.mark.parametrize("sql", ["SELECT 1", "SELECT * FROM `scratch`.`s_test`"])
def test_unknown_lineage_never_claims_timestamp(monkeypatch, sql):
    execute = Mock()
    monkeypatch.setattr(data_freshness, "execute_query", execute)
    settings = SimpleNamespace(
        trino_enabled=False,
        clickhouse_tenant_settings={},
        clickhouse_database="dbpcm_warehouse",
        scratch_database="scratch",
    )
    token = current_principal.set(
        Principal("test", {"proc_center": "PC01", "clientcode": "CLIENT_A"})
    )
    try:
        assert data_freshness.data_as_of(sql, settings) is None
    finally:
        current_principal.reset(token)
    execute.assert_not_called()


def test_internal_table_is_never_public_even_without_catalog_restriction():
    from app.errors import TableNotAllowedError
    from app.service import _check_table_allowed

    with pytest.raises(TableNotAllowedError):
        _check_table_allowed(
            "dbpcm_warehouse",
            "etl_product_refresh_status",
            SimpleNamespace(restrict_to_catalogued_tables=False),
        )


@pytest.mark.parametrize("error", ["UNKNOWN_TABLE", "UNKNOWN_DATABASE"])
def test_missing_status_table_preserves_result_and_omits_key(monkeypatch, error):
    from app import service
    from fastapi import HTTPException

    execute = Mock(
        side_effect=HTTPException(
            status_code=400,
            detail={"code": "CLICKHOUSE_QUERY_ERROR", "error": f"Metadata unavailable ({error})"},
        )
    )
    monkeypatch.setattr(data_freshness, "execute_query", execute)
    monkeypatch.setattr(service, "get_semantic_catalog", lambda: {})
    settings = SimpleNamespace(
        trino_enabled=False,
        clickhouse_tenant_settings={},
        clickhouse_database="dbpcm_warehouse",
        scratch_database="scratch",
    )
    raw = {"columns": ["Employee Count"], "rows": [[7]], "row_count": 1, "truncated": False}
    token = current_principal.set(
        Principal("test", {"proc_center": "PC01", "clientcode": "CLIENT_A"})
    )
    try:
        result = service._with_provenance(
            raw,
            frozenset(),
            sql="SELECT count() FROM employee",
            settings=settings,
        )
    finally:
        current_principal.reset(token)
    assert "data_as_of" not in result
    assert result == {**raw, "provenance": {"version": 1, "columns": []}}
    execute.assert_called_once()


def test_disabled_flag_skips_lookup_and_omits_key(monkeypatch):
    from app import service

    execute = Mock()
    monkeypatch.setattr(data_freshness, "execute_query", execute)
    monkeypatch.setattr(service, "get_semantic_catalog", lambda: {})
    settings = SimpleNamespace(data_as_of_enabled=False)
    token = current_principal.set(
        Principal("test", {"proc_center": "PC01", "clientcode": "CLIENT_A"})
    )
    try:
        result = service._with_provenance(
            {"columns": ["n"], "rows": [[1]]},
            frozenset(),
            sql="SELECT count() FROM employee",
            settings=settings,
        )
    finally:
        current_principal.reset(token)
    assert "data_as_of" not in result
    assert result["rows"] == [[1]]
    execute.assert_not_called()


def test_disabled_environment_flag(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("DATA_AS_OF_ENABLED", "false")
    assert (
        Settings(
            _env_file=None, clickhouse_host="localhost", clickhouse_user="test"
        ).data_as_of_enabled
        is False
    )
