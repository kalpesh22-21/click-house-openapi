"""Request-local column enrichment, source routing, and complete-or-error results."""

import copy
import json
from unittest.mock import MagicMock

import pytest

from app import catalog_context as context, service
from app.catalog_scope import CatalogScope
from app.config import Settings
from app.errors import ClickHouseUnavailableError
from app.principal import Principal, current_principal
from app.semantic_catalog.export import build_catalog_export_from


SQL = "SELECT category AS context_key, code FROM earning_codes WHERE client = :clientcode"


@pytest.fixture
def settings():
    return Settings(_env_file=None, trino_enabled=True, trino_host="test",
                    trino_security_ready=True, employee_access_enabled=False,
                    trino_catalog_check_enabled=False)


@pytest.fixture
def principal():
    user = Principal("alice", {"clientcode": "tenant-a", "proc_center": "pc", "user_name": "alice"})
    token = current_principal.set(user)
    yield user
    current_principal.reset(token)


@pytest.fixture
def catalog(monkeypatch):
    entry = {
        "database": "dbpcm_warehouse", "table": "time_off_request", "source": "mysql",
        "description": "Accrual events", "columns": {
            "earn_code": {"type": "String", "context_sql": SQL},
            "hours": {"type": "Float64", "context_sql": "SELECT 'units' AS context_key, 'hours' AS unit"},
            "tenant": {"type": "String", "context_sql": "SELECT 'private' AS context_key, 'secret' AS value"},
        }, "mcp_projection": {"hidden_columns": ["tenant"]},
    }
    catalog = {"dbpcm_warehouse.time_off_request": entry}
    monkeypatch.setattr(service, "get_semantic_catalog", lambda: catalog)
    monkeypatch.setattr("app.trino_metadata.get_semantic_catalog", lambda: catalog)
    monkeypatch.setattr("app.shard_routing.resolve_mysql_shard", lambda code, _: "shard_a" if code == "tenant-a" else "shard_b")
    return catalog


def test_schema_enriches_only_requested_visible_columns(monkeypatch, settings, principal, catalog):
    original = copy.deepcopy(catalog)
    execute = MagicMock(return_value=(["context_key", "code"], [["vacation", "VAC"], ["vacation", "VAC2"], ["timeoff", "PTO"]]))
    monkeypatch.setattr(context, "execute_context_sql", execute)
    response = service.get_table_schema("dbpcm_datawarehouse", "time_off_request", settings, columns=["earn_code"])
    assert response["columns"][0]["context"] == {
        "vacation": [{"code": "VAC"}, {"code": "VAC2"}], "timeoff": [{"code": "PTO"}],
    }
    execute.assert_called_once_with(SQL, catalog["dbpcm_warehouse.time_off_request"], settings)
    assert "context_sql" not in json.dumps(response)
    assert response["context_provenance"] == {"version": 1, "columns": [["dbpcm_warehouse.time_off_request", "earn_code"]]}
    assert catalog == original


def test_scope_trim_precedes_context(monkeypatch, settings, principal, catalog):
    from dataclasses import replace
    token = current_principal.set(replace(principal, catalog_scope=CatalogScope(columns=frozenset({"dbpcm_warehouse.time_off_request.earn_code"}))))
    monkeypatch.setattr("app.catalog.get_settings", lambda: settings)
    execute = MagicMock(return_value=(["context_key", "code"], []))
    monkeypatch.setattr(context, "execute_context_sql", execute)
    try:
        response = service.get_table_schema("dbpcm_datawarehouse", "time_off_request", settings)
        assert [c["name"] for c in response["columns"]] == ["earn_code"]
        assert response["columns"][0]["context"] == {}
        execute.assert_called_once()
    finally:
        current_principal.reset(token)


def test_listings_and_queries_do_not_enrich(monkeypatch, settings, principal, catalog):
    execute = MagicMock(side_effect=AssertionError("context SQL executed"))
    monkeypatch.setattr(context, "execute_context_sql", execute)
    monkeypatch.setattr(service, "_execute", lambda *a, **kw: (["code"], [["VAC"]]))
    assert service.list_databases(settings)
    assert service.list_tables("dbpcm_datawarehouse", settings)
    service.run_query("SELECT earn_code FROM time_off_request", settings=settings)
    service.explain_query("SELECT earn_code FROM time_off_request", settings=settings)
    execute.assert_not_called()


def test_export_omits_sql_without_running_it(monkeypatch, catalog):
    original = copy.deepcopy(catalog)
    execute = MagicMock(side_effect=AssertionError("export enriched columns"))
    monkeypatch.setattr(context, "execute_context_sql", execute)
    exported = build_catalog_export_from(catalog, "sha")
    assert "context_sql" not in json.dumps(exported)
    assert "earning_codes" not in json.dumps(exported)
    assert catalog == original
    execute.assert_not_called()


@pytest.mark.parametrize("result", [
    (["code"], [["VAC"]]), (["context_key", "code", "code"], []),
    (["context_key", "code"], [[None, "VAC"]]),
    (["context_key", "code"], [["vacation"]]),
])
def test_invalid_context_results_fail(monkeypatch, settings, principal, catalog, result):
    monkeypatch.setattr(context, "execute_context_sql", lambda *a: result)
    with pytest.raises(ClickHouseUnavailableError) as exc:
        service.get_table_schema("dbpcm_datawarehouse", "time_off_request", settings, columns=["earn_code"])
    assert exc.value.code == "CATALOG_CONTEXT_UNAVAILABLE"


def test_backend_failure_is_not_empty_or_private_sql(monkeypatch, settings, principal, catalog):
    monkeypatch.setattr(context, "execute_context_sql", MagicMock(side_effect=RuntimeError("secret SQL token")))
    with pytest.raises(ClickHouseUnavailableError) as exc:
        service.get_table_schema("dbpcm_datawarehouse", "time_off_request", settings, columns=["earn_code"])
    assert "secret" not in str(exc.value)
    assert exc.value.__suppress_context__


def test_context_routing_and_parameters_are_request_local(settings, principal, catalog):
    entry = catalog["dbpcm_warehouse.time_off_request"]
    sql, params, shard = context._compile(SQL, entry, settings)
    assert '"shard_a"."dbonline".earning_codes' in sql
    assert "tenant-a" not in sql and "?" in sql and params == ["tenant-a"]
    assert shard == "shard_a"
    token = current_principal.set(Principal("bob", {"clientcode": "tenant-b", "proc_center": "pc", "user_name": "bob"}))
    try:
        sql, params, shard = context._compile(SQL, entry, settings)
        assert '"shard_b"' in sql and params == ["tenant-b"] and shard == "shard_b"
    finally:
        current_principal.reset(token)


@pytest.mark.parametrize("sql", [
    "DELETE FROM earning_codes", "SELECT 1; SELECT 2", "SELECT * FROM other_catalog.other_schema.codes",
    "SELECT * FROM url('https://example.com')", "SELECT :missing AS context_key, 1 AS code",
])
def test_bad_context_sql_never_connects(monkeypatch, settings, principal, catalog, sql):
    connect = MagicMock(side_effect=AssertionError("connected"))
    monkeypatch.setattr("app.trino_client._connect", connect)
    with pytest.raises(Exception):
        context.execute_context_sql(sql, catalog["dbpcm_warehouse.time_off_request"], settings)
    connect.assert_not_called()


def test_driver_binds_in_rendered_order_and_closes(monkeypatch, settings, principal, catalog):
    conn, cursor = MagicMock(), MagicMock()
    conn.cursor.return_value = cursor
    cursor.description = [("context_key",), ("code",)]
    cursor.fetchmany.side_effect = [[["vacation", "VAC"]], []]
    connect = MagicMock(return_value=conn)
    monkeypatch.setattr("app.trino_client._connect", connect)
    sql = "WITH codes AS (SELECT :user_name AS code) SELECT :clientcode AS context_key, code FROM codes"
    columns, rows = context.execute_context_sql(sql, catalog["dbpcm_warehouse.time_off_request"], settings)
    assert cursor.execute.call_args.args[1] == ["alice", "tenant-a"]
    assert "LIMIT" not in cursor.execute.call_args.args[0]
    assert rows == [["vacation", "VAC"]]
    connect.assert_called_once_with(settings, "pc|tenant-a|alice", mysql_catalog="shard_a")
    cursor.close.assert_called_once()
    conn.close.assert_called_once()


@pytest.mark.parametrize("limit", ["rows", "bytes"])
def test_oversized_context_fails_without_truncation(monkeypatch, settings, principal, catalog, limit):
    if limit == "rows":
        settings.catalog_context_max_rows = 1
    else:
        settings.catalog_context_max_bytes = 1
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchmany.side_effect = [[["vacation", "VAC"], ["timeoff", "PTO"]]]
    monkeypatch.setattr("app.trino_client._connect", lambda *a, **kw: conn)
    with pytest.raises(ValueError, match="result limit"):
        context.execute_context_sql(SQL, catalog["dbpcm_warehouse.time_off_request"], settings)
    cursor.close.assert_called_once()
    conn.close.assert_called_once()


@pytest.mark.parametrize("source", ["mysql", "clickhouse"])
def test_direct_clickhouse_skips_column_context(monkeypatch, settings, catalog, source):
    settings.trino_enabled = False
    entry = {**catalog["dbpcm_warehouse.time_off_request"], "source": source}
    execute = MagicMock(side_effect=AssertionError("lookup must not execute"))
    monkeypatch.setattr(context, "execute_context_sql", execute)
    response = {"columns": [{"name": "earn_code", "type": "String"}]}
    context.enrich_column_context(response, entry, settings)
    execute.assert_not_called()
    assert response == {"columns": [{"name": "earn_code", "type": "String"}]}


def test_direct_context_execution_requires_trino(monkeypatch, settings, catalog):
    settings.trino_enabled = False
    compile_sql = MagicMock(side_effect=AssertionError("must not compile or route"))
    monkeypatch.setattr(context, "_compile", compile_sql)
    with pytest.raises(ValueError, match="Column context requires Trino"):
        context.execute_context_sql(SQL, catalog["dbpcm_warehouse.time_off_request"], settings)
    compile_sql.assert_not_called()


def test_no_principal_cannot_enrich(settings, catalog):
    token = current_principal.set(None)
    try:
        with pytest.raises(ValueError, match="authenticated"):
            context.execute_context_sql(SQL, catalog["dbpcm_warehouse.time_off_request"], settings)
    finally:
        current_principal.reset(token)


def test_literal_colons_are_not_parameters(settings, principal, catalog):
    sql, params, _ = context._compile(
        "SELECT ':not_a_claim' AS context_key, :clientcode AS code",
        catalog["dbpcm_warehouse.time_off_request"], settings,
    )
    assert "':not_a_claim'" in sql and params == ["tenant-a"]


def test_security_gate_precedes_routing(monkeypatch, settings, principal, catalog):
    settings.trino_security_ready = False
    route = MagicMock(side_effect=AssertionError("routing before security gate"))
    monkeypatch.setattr("app.shard_routing.resolve_mysql_shard", route)
    with pytest.raises(ValueError, match="security"):
        context.execute_context_sql(SQL, catalog["dbpcm_warehouse.time_off_request"], settings)
    route.assert_not_called()


def test_rest_preserves_context_and_excludes_sql(monkeypatch, test_client, auth_headers):
    client, _, _ = test_client
    monkeypatch.setattr("app.routers.schema.svc_get_table_schema", lambda *a: {
        "database": "dbpcm_warehouse", "table": "time_off_request", "columns": [{
            "name": "earn_code", "type": "String", "comment": "",
            "context": {"vacation": [{"code": "VAC"}]}, "context_sql": "PRIVATE SQL",
        }],
    })
    response = client.get("/tables/schema?database=dbpcm_warehouse&table=time_off_request", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["columns"][0]["context"] == {"vacation": [{"code": "VAC"}]}
    assert "PRIVATE SQL" not in response.text and "context_sql" not in response.text


def test_mcp_context_error_has_actionable_stage():
    from app.model_errors import model_error_payload
    payload = model_error_payload(ClickHouseUnavailableError("private backend detail", "CATALOG_CONTEXT_UNAVAILABLE"))
    assert payload["code"] == "CATALOG_CONTEXT_UNAVAILABLE"
    assert payload["retry"] == "after_operator_action"
    assert "private" not in json.dumps(payload)


@pytest.mark.parametrize("value", [None, "", "   ", {}, []])
def test_loader_rejects_invalid_context_sql(tmp_path, value):
    import yaml
    from app.semantic_catalog.loader import load_semantic_catalog, CatalogValidationError
    (tmp_path / "table.yaml").write_text(yaml.safe_dump({
        "database": "db", "table": "events",
        "columns": {"code": {"type": "String", "context_sql": value}},
    }))
    with pytest.raises(CatalogValidationError, match="context_sql"):
        load_semantic_catalog(tmp_path)


def test_trino_clickhouse_context_uses_native_tenant_settings(settings, principal, catalog):
    import sqlglot
    from sqlglot import exp
    entry = {**catalog["dbpcm_warehouse.time_off_request"], "source": "clickhouse"}
    principal.claims["jti"] = "request-alice"
    rendered, parameters, shard = context._compile(SQL, entry, settings)
    tree = sqlglot.parse_one(rendered, read="trino")
    native = next(l.this for l in tree.find_all(exp.Literal) if l.is_string and "SETTINGS" in l.this)
    assert "`dbpcm_warehouse`.`earning_codes`" in native
    assert "`final` = 1" in native
    assert "request-alice" in native and "tenant-a" in native
    assert parameters == ["tenant-a"] and shard is None


def test_context_catalogued_mysql_relation_expands_template(tmp_path, monkeypatch, settings, principal, catalog):
    from tests.source_helpers import write_mysql_sources
    write_mysql_sources(tmp_path)
    settings.trino_mysql_sources_dir = str(tmp_path)
    monkeypatch.setattr("app.semantic_catalog.get_semantic_catalog", lambda: catalog)
    rendered, parameters, shard = context._compile(
        "SELECT earn_code AS context_key, hours FROM time_off_request", catalog["dbpcm_warehouse.time_off_request"], settings
    )
    assert '"shard_a".dbonline.time_off_request' in rendered
    assert "'tenant-a'" in rendered
    assert "CURRENT_USER" not in rendered
    assert parameters == [] and shard == "shard_a"


@pytest.fixture(autouse=True)
def _authorized_session_for_route_tests(prevalidated_session_scope):
    """Session-scope auth is tested separately in test_session_catalog."""
