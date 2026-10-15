"""Public synthetic native source contracts; no proprietary SQL fixtures."""

# ruff: noqa: F811
import json
from unittest.mock import MagicMock

import pytest
import sqlglot
from pydantic import ValidationError
from sqlglot import exp

from app import trino_client, trino_metadata
from app.errors import QueryValidationError
from app.principal import Principal, current_principal
from tests.test_trino import (  # noqa: F401
    principal,
    shard_registry_path,
    trino_settings,
)


@pytest.fixture
def native_setup(tmp_path, monkeypatch, trino_settings):
    entries = {
        "dbpcm_warehouse.employee": {
            "database": "dbpcm_warehouse",
            "table": "employee",
            "source": "mysql",
            "columns": {
                "employee_code": {"type": "String"},
                "client_code": {"type": "String"},
            },
            "trino_join_keys": {
                "employee_code": {"column": "employee_code_num", "encoding": "test_v1"}
            },
        },
        "dbpcm_warehouse.payroll": {
            "database": "dbpcm_warehouse",
            "table": "payroll",
            "source": "clickhouse",
            "columns": {
                "employee_code": {"type": "String"},
                "amount": {"type": "Int64"},
            },
        },
    }
    monkeypatch.setattr(trino_client, "get_semantic_catalog", lambda: entries)
    monkeypatch.setattr(trino_metadata, "get_semantic_catalog", lambda: entries)
    contract = {
        "authorization": "employee",
        "native_sql": "SELECT e.* FROM {{MYSQL_SCHEMA}}.staff e WHERE e.client_code={{CLIENT_CODE}} AND EXISTS "
        "(SELECT 1 FROM {{MYSQL_SCHEMA}}.grants a WHERE a.tenant={{CLIENT_CODE}} AND a.principal={{USERNAME}} AND a.code=e.employee_code)",
        "projection_sql": "SELECT CAST(e.employee_code AS varchar) AS employee_code, e.employee_code_num, "
        "CAST(e.client_code AS varchar) AS client_code, {{PROC_CENTER}} AS proc_center FROM native_source e",
    }
    (tmp_path / "employee.json").write_text(json.dumps(contract))
    trino_settings.trino_mysql_native_sources_dir = str(tmp_path)
    return entries, contract, tmp_path


def native_strings(sql):
    return [
        n.this
        for n in sqlglot.parse_one(sql, read="trino").find_all(exp.Literal)
        if n.is_string and n.this.startswith("SELECT")
    ]


def test_clickhouse_settings_and_mysql_modes(native_setup, trino_settings, principal):
    query = "SELECT e.employee_code,p.amount FROM employee e JOIN payroll p ON e.employee_code=p.employee_code"
    old = trino_client.rewrite_query(query, trino_settings)
    assert len(native_strings(old)) == 1
    ch = native_strings(old)[0]
    assert "paycom_authenticated_user = 'alice-jti'" in ch
    assert "paycom_client_code = '10R37'" in ch
    assert "readonly = 2" in ch and "`final` = 1" in ch
    assert "result_overflow_mode = 'throw'" in ch
    assert '"mysql_shard_prod_01"."dbonline".employee' in old
    trino_settings.trino_mysql_source_mode = "system_query"
    new = trino_client.rewrite_query(query, trino_settings)
    assert len(native_strings(new)) == 2
    assert any(
        "X'616c696365'" in sql for sql in native_strings(new)
    )  # MySQL user_name, not ClickHouse JTI.
    assert "CAST(e.employee_code AS VARCHAR)" in new
    assert "AS e JOIN" in new and "AS p ON" in new


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM TABLE(clickhouse.system.query(query => 'SELECT 1'))",
        "SELECT * FROM clickhouse.dbpcm_warehouse.payroll",
        "WITH x AS (SELECT * FROM employee) SELECT * FROM x JOIN mysql.raw.secret s ON true",
    ],
)
def test_caller_native_sources_still_rejected(
    query, native_setup, trino_settings, principal
):
    trino_settings.trino_mysql_source_mode = "system_query"
    with pytest.raises(QueryValidationError):
        trino_client.rewrite_query(query, trino_settings)


def test_all_relations_validated_before_private_source_loading(
    native_setup, trino_settings, principal, monkeypatch
):
    trino_settings.trino_mysql_source_mode = "system_query"
    build = MagicMock()
    monkeypatch.setattr("app.trino_native_sources.source_relation", build)
    with pytest.raises(QueryValidationError):
        trino_client.rewrite_query(
            "SELECT * FROM employee JOIN unknown x ON true", trino_settings
        )
    build.assert_not_called()


def test_cte_shadowing_and_self_join(native_setup, trino_settings, principal):
    trino_settings.trino_mysql_source_mode = "system_query"
    sql = trino_client.rewrite_query(
        "WITH x AS (SELECT employee_code FROM employee) SELECT a.employee_code FROM x a JOIN x b ON a.employee_code=b.employee_code",
        trino_settings,
    )
    assert len(native_strings(sql)) == 1
    sql = trino_client.rewrite_query(
        "SELECT a.employee_code FROM employee a LEFT JOIN employee b ON a.employee_code=b.employee_code",
        trino_settings,
    )
    assert len(native_strings(sql)) == 2
    assert "a.employee_code_num = b.employee_code_num" in sql
    assert "a.employee_code = b.employee_code" in trino_client.rewrite_query(
        "SELECT a.employee_code FROM employee a LEFT JOIN employee b ON a.employee_code=b.employee_code",
        trino_settings,
        optimize_employee_joins=False,
    )


def test_literals_are_encoded_for_both_layers(native_setup, trino_settings):
    trino_settings.trino_mysql_source_mode = "system_query"
    name = "x'\\ OR 1=1 --"
    token = current_principal.set(
        Principal(
            "s",
            {
                "proc_center": "pc1",
                "clientcode": "10R37",
                "user_name": name,
                "jti": name,
            },
        )
    )
    try:
        sql = trino_client.rewrite_query(
            "SELECT * FROM employee JOIN payroll ON true", trino_settings
        )
        statements = native_strings(sql)
        assert any(f"X'{name.encode().hex()}'" in statement for statement in statements)
        ch = sqlglot.parse_one(
            next(s for s in statements if "SETTINGS" in s), read="clickhouse"
        )
        assert ch.args["settings"]
        assert any(n.is_string and n.this == name for n in ch.find_all(exp.Literal))
    finally:
        current_principal.reset(token)


@pytest.mark.parametrize(
    "change",
    [
        {"authorization": "unknown"},
        {"projection_sql": None},
        {"native_sql": []},
        {"native_sql": "DELETE FROM staff"},
        {"projection_sql": "SELECT * FROM secret"},
        {"native_sql": "SELECT {{CLIENT_CODE}},{{USERNAME}}; SELECT 2"},
    ],
)
def test_invalid_contract_fail_closed(change, native_setup, trino_settings, principal):
    _, contract, path = native_setup
    contract.update(change)
    (path / "employee.json").write_text(json.dumps(contract))
    trino_settings.trino_mysql_source_mode = "system_query"
    with pytest.raises(QueryValidationError, match="private source definition"):
        trino_client.rewrite_query("SELECT * FROM employee", trino_settings)


def test_missing_definition_no_silent_unsecured_fallback(
    native_setup, trino_settings, principal
):
    (native_setup[2] / "employee.json").unlink()
    trino_settings.trino_mysql_source_mode = "system_query"
    with pytest.raises(QueryValidationError):
        trino_client.rewrite_query("SELECT * FROM employee", trino_settings)
    trino_settings.trino_mysql_source_mode = "views"
    assert "system.query" not in trino_client.rewrite_query(
        "SELECT * FROM employee", trino_settings
    )


def test_refresh_only_for_clickhouse_after_validation(
    native_setup, trino_settings, principal, monkeypatch
):
    refresh = MagicMock()
    execute = MagicMock(return_value=([], []))
    monkeypatch.setattr("app.employee_access.ensure_employee_access_fresh", refresh)
    monkeypatch.setattr(trino_client, "_execute_physical_query", execute)
    trino_client.execute_query("SELECT * FROM employee", trino_settings)
    refresh.assert_not_called()
    trino_client.execute_query("SELECT * FROM payroll", trino_settings)
    refresh.assert_called_once_with(trino_settings)
    with pytest.raises(QueryValidationError):
        trino_client.execute_query("SELECT * FROM unknown", trino_settings)
    assert refresh.call_count == 1


def test_refresh_failure_stops_dispatch(
    native_setup, trino_settings, principal, monkeypatch
):
    refresh = MagicMock(side_effect=RuntimeError("refresh unavailable"))
    execute = MagicMock()
    monkeypatch.setattr("app.employee_access.ensure_employee_access_fresh", refresh)
    monkeypatch.setattr(trino_client, "_execute_physical_query", execute)
    with pytest.raises(RuntimeError):
        trino_client.execute_query("SELECT * FROM payroll", trino_settings)
    execute.assert_not_called()


def test_native_metadata_without_trino_view(
    native_setup, trino_settings, principal, monkeypatch
):
    trino_settings.trino_mysql_source_mode = "system_query"
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = [1]
    cursor.description = [
        ("employee_code", "varchar"),
        ("client_code", "varchar"),
        ("private_extra", "bigint"),
    ]
    monkeypatch.setattr(trino_client, "_connect", lambda *args: conn)
    found = trino_metadata.discover(trino_settings, "dbpcm_warehouse", "employee")
    assert set(found["dbpcm_warehouse.employee"]["columns"]) == {
        "employee_code",
        "client_code",
    }
    assert "system.query" in cursor.execute.call_args.args[0]
    assert not any(
        "information_schema" in c.args[0] for c in cursor.execute.call_args_list
    )


def test_discovery_memo_includes_clickhouse_identity(
    native_setup, trino_settings, monkeypatch
):
    discover = MagicMock(return_value={})
    monkeypatch.setattr(trino_metadata, "_discover_live", discover)
    memo = trino_metadata.discovery_memo.set({})
    try:
        for jti in ["one", "two", "one"]:
            tok = current_principal.set(
                Principal(
                    "s",
                    {
                        "proc_center": "pc1",
                        "clientcode": "10R37",
                        "user_name": "same",
                        "jti": jti,
                    },
                )
            )
            try:
                trino_metadata.discover(trino_settings)
            finally:
                current_principal.reset(tok)
    finally:
        trino_metadata.discovery_memo.reset(memo)
    assert discover.call_count == 2


def test_invalid_mode_rejected(trino_settings):
    with pytest.raises(ValidationError):
        type(trino_settings)(
            **{**trino_settings.model_dump(), "trino_mysql_source_mode": "typo"}
        )


def test_group_and_having_aliases(native_setup, trino_settings, principal):
    sql = trino_client.rewrite_query(
        "SELECT toStartOfMonth(pay_date) AS month, sum(amount) AS total "
        "FROM payroll GROUP BY month HAVING total > 10",
        trino_settings,
    )
    tree = sqlglot.parse_one(sql, read="trino")
    assert "DATE_TRUNC('MONTH', pay_date)" in tree.args["group"].sql(dialect="trino")
    assert "COALESCE(SUM(amount), 0) > 10" in tree.args["having"].sql(dialect="trino")


def test_group_alias_scope_is_local(native_setup, trino_settings, principal):
    sql = trino_client.rewrite_query(
        "WITH p AS (SELECT employee_code AS key, sum(amount) AS total "
        "FROM payroll GROUP BY key) SELECT key AS employee, sum(total) AS amount "
        "FROM p GROUP BY employee",
        trino_settings,
    )
    tree = sqlglot.parse_one(sql, read="trino")
    assert tree.args["group"].sql(dialect="trino") == "GROUP BY key"
    inner = tree.args["with_"].expressions[0].this
    assert inner.args["group"].sql(dialect="trino") == "GROUP BY employee_code"


def test_native_join_retry_preserves_policy(
    native_setup, trino_settings, principal, monkeypatch
):
    from tests.test_trino_join_rewrite import connections, error

    trino_settings.trino_mysql_source_mode = "system_query"
    connect, conns = connections(monkeypatch, error())
    trino_client.execute_query(
        "SELECT a.employee_code FROM employee a JOIN employee b ON a.employee_code=b.employee_code",
        trino_settings,
    )
    assert connect.call_count == 2
    first, second = [c.cursor.return_value.execute.call_args.args[0] for c in conns]
    assert "a.employee_code_num = b.employee_code_num" in first
    assert "a.employee_code = b.employee_code" in second
    assert native_strings(first) == native_strings(second)
    assert len(native_strings(second)) == 2


def test_native_query_without_join_change_is_not_retried(
    native_setup, trino_settings, principal, monkeypatch
):
    from app.errors import ClickHouseQueryError
    from tests.test_trino_join_rewrite import connections, error

    trino_settings.trino_mysql_source_mode = "system_query"
    connect, _ = connections(monkeypatch, error())
    with pytest.raises(ClickHouseQueryError):
        trino_client.execute_query("SELECT employee_code FROM employee", trino_settings)
    assert connect.call_count == 1


def test_unprovisioned_native_source_not_discovered(
    native_setup, trino_settings, principal, monkeypatch
):
    _, _, path = native_setup
    (path / "employee.json").unlink()
    trino_settings.trino_mysql_source_mode = "system_query"
    conn = MagicMock()
    conn.cursor.return_value.fetchone.return_value = [1]
    monkeypatch.setattr(trino_client, "_connect", lambda *args: conn)
    assert trino_metadata.discover(trino_settings, "dbpcm_warehouse", "employee") == {}
    assert conn.cursor.return_value.execute.call_count == 1
    with pytest.raises(QueryValidationError):
        trino_client.rewrite_query("SELECT employee_code FROM employee", trino_settings)
