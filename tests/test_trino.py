"""Backend isolation, source routing and local provisioning contract."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
import sqlglot
from sqlglot import exp

from app import service, trino_client
from app.config import Settings
from app.errors import ClickHouseUnavailableError, QueryValidationError
from app.principal import Principal, current_principal
from app.semantic_catalog.loader import CatalogValidationError, load_semantic_catalog
from app.shard_routing import resolve_mysql_shard
from scripts.setup_trino_catalogs import render_catalogs, write_catalogs


@pytest.fixture
def trino_settings():
    return Settings(
        _env_file=None,
        trino_enabled=True,
        trino_mysql_view_catalog="",
        trino_security_ready=True,
        trino_http_scheme="http",
        trino_shard_routes={
            "pc1": {"10R37": "mysql_shard_07"},
            "pc2": {"10R37": "mysql_shard_08"},
        },
        allowed_databases="dbpcm_warehouse",
    )


@pytest.fixture
def principal():
    token = current_principal.set(
        Principal(
            "subject",
            {
                "proc_center": "pc1",
                "clientcode": "10R37",
                "user_name": "alice",
            },
        )
    )
    yield
    current_principal.reset(token)


def tables(sql):
    return {
        (t.catalog, t.db, t.name)
        for t in sqlglot.parse_one(sql, read="trino").find_all(exp.Table)
    }


def test_mixed_sources_and_qualified_columns(trino_settings, principal):
    sql = trino_client.rewrite_query(
        "SELECT dbpcm_warehouse.employee.employee_code, p.gross_pay "
        "FROM dbpcm_warehouse.employee JOIN payroll p "
        "ON employee.employee_code = p.employee_code",
        trino_settings,
    )
    assert tables(sql) == {
        ("mysql_shard_07", "dbonline", "employee"),
        ("clickhouse", "dbpcm_warehouse", "payroll"),
    }
    assert "employee.employee_code" in sql
    assert "dbpcm_warehouse.employee.employee_code" not in sql


def test_cte_shadowing_nested_union_and_strings(trino_settings, principal):
    sql = trino_client.rewrite_query(
        "WITH employee AS (SELECT employee_code FROM dbpcm_warehouse.employee) "
        "SELECT employee_code, 'employee' AS label FROM employee "
        "UNION ALL SELECT employee_code, 'employee' FROM "
        "(SELECT employee_code FROM dbpcm_warehouse.employee) x",
        trino_settings,
    )
    assert sql.count('"mysql_shard_07"."dbonline".employee') == 2
    assert "FROM employee AS employee" in sql
    assert sql.count("'employee'") == 2


def test_self_join_preserves_aliases(trino_settings, principal):
    sql = trino_client.rewrite_query(
        "SELECT a.employee_code FROM employee a JOIN employee b "
        "ON a.employee_code = b.employee_code",
        trino_settings,
    )
    assert "AS a" in sql and "AS b" in sql
    assert sql.count('"mysql_shard_07"') == 2


def test_view_catalog_mode(trino_settings, principal):
    trino_settings.trino_mysql_view_catalog = "reporting"
    assert tables(
        trino_client.rewrite_query("SELECT * FROM employee", trino_settings)
    ) == {("reporting", "mysql_shard_07", "employee")}


def test_explain_and_count_translation(trino_settings, principal):
    sql = trino_client.rewrite_query(
        "EXPLAIN SELECT count() FROM employee", trino_settings
    )
    assert sql.startswith("EXPLAIN SELECT COUNT(*)")
    assert "mysql_shard_07" in sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM mysql_shard_08.dbonline.employee",
        "SELECT * FROM unknown",
        "SELECT * FROM scratch.upload",
        "SELECT * FROM system.tables",
        "SELECT * FROM other.employee",
        "SELECT * FROM mysql('remote', 'db', 'employee')",
        "SELECT 1; SELECT 2",
        "SHOW TABLES",
        "EXPLAIN ANALYZE SELECT 1",
        "DELETE FROM employee",
    ],
)
def test_reject_unsupported_or_untrusted_relations(sql, trino_settings, principal):
    with pytest.raises(QueryValidationError):
        trino_client.rewrite_query(sql, trino_settings)


def test_source_flag_validation_and_legacy_default(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text("table: x\ncolumns:\n  id: {type: String}\n")
    assert (
        load_semantic_catalog(tmp_path)["dbpcm_warehouse.x"].get("source", "clickhouse")
        == "clickhouse"
    )
    path.write_text(path.read_text() + "source: typo\n")
    with pytest.raises(CatalogValidationError):
        load_semantic_catalog(tmp_path)


def test_routing_uses_both_claims(trino_settings):
    assert resolve_mysql_shard("pc1", "10R37", trino_settings) == "mysql_shard_07"
    assert resolve_mysql_shard("pc2", "10R37", trino_settings) == "mysql_shard_08"
    with pytest.raises(QueryValidationError, match="No MySQL shard"):
        resolve_mysql_shard("missing", "10R37", trino_settings)
    trino_settings.trino_shard_routes["pc1"]["10R37"] = "mysql_bad; DROP TABLE x"
    with pytest.raises(QueryValidationError):
        resolve_mysql_shard("pc1", "10R37", trino_settings)


def test_identity_missing_and_delimiter_rejected(trino_settings):
    with pytest.raises(QueryValidationError):
        trino_client.request_identity(trino_settings)
    token = current_principal.set(
        Principal(
            "s", {"proc_center": "pc1", "clientcode": "10R37", "user_name": "a|b"}
        )
    )
    try:
        with pytest.raises(QueryValidationError):
            trino_client.request_identity(trino_settings)
    finally:
        current_principal.reset(token)


def test_parallel_requests_do_not_share_route(trino_settings):
    def run(pc):
        token = current_principal.set(
            Principal("s", {"proc_center": pc, "clientcode": "10R37", "user_name": pc})
        )
        try:
            return trino_client.rewrite_query("SELECT * FROM employee", trino_settings)
        finally:
            current_principal.reset(token)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, ["pc1", "pc2"] * 5))
    for i, sql in enumerate(results):
        assert ("mysql_shard_07" if i % 2 == 0 else "mysql_shard_08") in sql


def mock_connection(monkeypatch, batches):
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.description = [("employee_count",)]
    cursor.fetchmany.side_effect = batches
    connect = MagicMock(return_value=conn)
    monkeypatch.setattr(trino_client, "_connect", connect)
    return connect, conn, cursor


def test_execution_identity_and_cleanup(monkeypatch, trino_settings, principal):
    connect, conn, cursor = mock_connection(monkeypatch, [[[4]], []])
    assert trino_client.execute_query(
        "SELECT count() FROM employee", trino_settings
    ) == (["employee_count"], [[4]])
    connect.assert_called_once_with(trino_settings, "pc1|10R37|alice")
    assert "COUNT(*)" in cursor.execute.call_args.args[0]
    cursor.close.assert_called_once()
    conn.close.assert_called_once()


@pytest.mark.parametrize("limit", ["rows", "bytes"])
def test_caps_cancel_query(monkeypatch, trino_settings, principal, limit):
    _, conn, cursor = mock_connection(monkeypatch, [[[1], [2]]])
    if limit == "rows":
        trino_settings.max_result_rows = 1
    else:
        trino_settings.max_result_bytes = 1
    with pytest.raises(QueryValidationError) as exc:
        trino_client.execute_query("SELECT employee_code FROM employee", trino_settings)
    assert exc.value.code == "TRINO_RESULT_LIMIT"
    cursor.close.assert_called_once()
    conn.close.assert_called_once()


def test_security_gate_prevents_connection(monkeypatch, trino_settings, principal):
    connect, _, _ = mock_connection(monkeypatch, [])
    trino_settings.trino_security_ready = False
    with pytest.raises(QueryValidationError) as exc:
        trino_client.execute_query("SELECT * FROM employee", trino_settings)
    assert exc.value.code == "TRINO_SECURITY_NOT_CONFIGURED"
    connect.assert_not_called()


def test_errors_do_not_leak_connector_sql(monkeypatch, trino_settings, principal):
    _, conn, cursor = mock_connection(monkeypatch, [])
    cursor.execute.side_effect = RuntimeError("password=secret select private data")
    with pytest.raises(ClickHouseUnavailableError) as exc:
        trino_client.execute_query("SELECT * FROM employee", trino_settings)
    assert "secret" not in exc.value.message
    conn.close.assert_called_once()


def test_service_flag_disabled_preserves_clickhouse(monkeypatch):
    ch = MagicMock(return_value=(["n"], [[1]]))
    trino = MagicMock()
    monkeypatch.setattr(service, "execute_query", ch)
    monkeypatch.setattr(trino_client, "execute_query", trino)
    settings = Settings(_env_file=None, trino_enabled=False)
    assert service._execute("SELECT 1", settings) == (["n"], [[1]])
    ch.assert_called_once()
    trino.assert_not_called()


def test_service_flag_routes_query_and_explain(monkeypatch, trino_settings, principal):
    trino = MagicMock(return_value=(["n"], [[1]]))
    monkeypatch.setattr(trino_client, "execute_query", trino)
    ch = MagicMock(side_effect=AssertionError("Unexpected ClickHouse call"))
    monkeypatch.setattr(service, "execute_query", ch)
    monkeypatch.setattr(service, "ensure_employee_access_fresh", ch)
    service.run_query("SELECT count() FROM employee", settings=trino_settings)
    service.explain_query("SELECT count() FROM employee", settings=trino_settings)
    assert trino.call_count == 2
    assert trino.call_args.args[0].startswith("EXPLAIN ")
    ch.assert_not_called()


def test_logical_metadata_never_reads_clickhouse(
    monkeypatch, trino_settings, authored_discovery
):
    monkeypatch.setattr(
        service, "execute_query", MagicMock(side_effect=AssertionError("CH metadata"))
    )
    assert service.list_databases(trino_settings) == [{"name": "dbpcm_datawarehouse"}]
    assert any(
        t["name"] == "employee"
        for t in service.list_tables("dbpcm_warehouse", trino_settings)
    )
    schema = service.get_table_schema("dbpcm_warehouse", "employee", trino_settings)
    assert schema["catalogued"] is True
    assert all(c["name"] != "client_code" for c in schema["columns"])


def test_sample_uses_authored_columns(
    monkeypatch, trino_settings, principal, authored_discovery
):
    trino = MagicMock(return_value=(["employee_code"], [["E1"]]))
    monkeypatch.setattr(trino_client, "execute_query", trino)
    monkeypatch.setattr(
        service, "execute_query", MagicMock(side_effect=AssertionError("CH call"))
    )
    service.sample_rows("dbpcm_warehouse", "employee", settings=trino_settings)
    sql = trino.call_args.args[0]
    assert "employee_code" in sql and "`client_code`" not in sql


def test_trino_column_scope_is_still_enforced(
    monkeypatch, trino_settings, principal, authored_discovery
):
    from app.errors import ColumnScopeError
    from app.principal import current_scope

    monkeypatch.setattr("app.catalog.get_settings", lambda: trino_settings)
    trino = MagicMock()
    monkeypatch.setattr(trino_client, "execute_query", trino)
    token = current_scope.set(frozenset({"dbpcm_warehouse.employee.employee_code"}))
    try:
        with pytest.raises(ColumnScopeError):
            service.run_query(
                "SELECT employee_name FROM employee",
                settings=trino_settings,
                include_provenance=True,
            )
    finally:
        current_scope.reset(token)
    trino.assert_not_called()


def test_trino_provenance_uses_logical_names(
    monkeypatch, trino_settings, principal, authored_discovery
):
    monkeypatch.setattr("app.catalog.get_settings", lambda: trino_settings)
    monkeypatch.setattr(
        trino_client,
        "execute_query",
        MagicMock(return_value=(["employee_code"], [["E1"]])),
    )
    result = service.run_query(
        "SELECT employee_code FROM employee",
        settings=trino_settings,
        include_provenance=True,
    )
    assert result["provenance"]["columns"] == [
        ["dbpcm_warehouse.employee", "employee_code"]
    ]


def test_tls_and_authenticated_user_configuration(monkeypatch, trino_settings):
    connect = MagicMock()
    monkeypatch.setattr("trino.dbapi.connect", connect)
    trino_settings.trino_password = "test-only"
    with pytest.raises(QueryValidationError) as exc:
        trino_client._connect(trino_settings, "pc1|10R37|alice")
    assert exc.value.code == "TRINO_TLS_REQUIRED"
    connect.assert_not_called()
    trino_settings.trino_http_scheme = "https"
    trino_client._connect(trino_settings, "pc1|10R37|alice")
    kwargs = connect.call_args.kwargs
    assert kwargs["user"] == "pc1|10R37|alice"
    assert kwargs["auth"] is not None
    assert kwargs["session_properties"]["query_max_run_time"] == "30s"


def test_catalog_generator(tmp_path):
    inventory = {
        "mysql_shards": [
            {"catalog": "mysql_shard_07", "host": "mysql.example", "port": 3306}
        ]
    }
    rendered = render_catalogs(inventory)["mysql_shard_07"]
    assert "jdbc:mysql://mysql.example:3306" in rendered
    assert "${ENV:TRINO_MYSQL_PASSWORD}" in rendered
    paths = write_catalogs(inventory, tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        write_catalogs(inventory, tmp_path)
    assert write_catalogs(inventory, tmp_path, overwrite=True) == paths


@pytest.mark.parametrize(
    "patch",
    [
        {"catalog": "../escape"},
        {"host": "host\nconnection-user=root"},
        {"port": 0},
        {"port": True},
        {"password_env": "BAD}\nfoo"},
    ],
)
def test_generator_rejects_injection(patch):
    source = {"catalog": "mysql_shard_07", "host": "localhost", **patch}
    with pytest.raises(ValueError):
        render_catalogs({"mysql_shards": [source]})


@pytest.fixture
def authored_discovery(monkeypatch):
    from app import trino_metadata

    def fake(settings, database=None, table=None):
        return {
            key: {
                **entry,
                "columns": {
                    name: {**info, "physical_type": "varchar"}
                    for name, info in entry["columns"].items()
                },
            }
            for key, entry in trino_metadata.logical_catalog(settings).items()
            if (
                database is None
                or entry["database"] == trino_metadata.canonical_database(database)
            )
            and (table is None or entry["table"] == table)
        }

    monkeypatch.setattr(trino_metadata, "discover", fake)


def discovery_connection(monkeypatch, rows):
    conn, cursor = MagicMock(), MagicMock()
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = [1]

    def execute(sql, parameters=None):
        cursor.fetchmany.side_effect = (
            [rows, []] if "information_schema" in sql else [[]]
        )

    cursor.execute.side_effect = execute
    connect = MagicMock(return_value=conn)
    monkeypatch.setattr(trino_client, "_connect", connect)
    return connect, conn, cursor


def test_live_discovery_intersects_and_probes(monkeypatch, trino_settings, principal):
    from app import trino_metadata

    trino_settings.trino_mysql_view_catalog = "dbpcm_datawarehouse"
    connect, conn, cursor = discovery_connection(
        monkeypatch,
        [
            ["employee", "employee_code", "varchar"],
            ["employee", "department_name", "varchar(100)"],
            ["employee", "secret_extra", "varchar"],
            ["uncatalogued", "employee_code", "varchar"],
        ],
    )
    result = trino_metadata.discover(trino_settings, "dbpcm_datawarehouse", "employee")
    assert set(result) == {"dbpcm_warehouse.employee"}
    assert set(result["dbpcm_warehouse.employee"]["columns"]) == {
        "employee_code",
        "department_name",
    }
    connect.assert_called_once_with(trino_settings, "pc1|10R37|alice")
    calls = cursor.execute.call_args_list
    assert calls[0].args == ("SELECT 1",)
    assert calls[1].args[1] == ["mysql_shard_07", "employee"]
    assert '"dbpcm_datawarehouse".information_schema.columns' in calls[1].args[0]
    assert (
        '"dbpcm_datawarehouse"."mysql_shard_07"."employee" LIMIT 0' in calls[2].args[0]
    )
    cursor.close.assert_called_once()
    conn.close.assert_called_once()


def test_live_discovery_missing_and_partial_views(
    monkeypatch, trino_settings, principal
):
    discovery_connection(monkeypatch, [["employee", "employee_code", "varchar"]])
    assert service.list_databases(trino_settings) == [{"name": "dbpcm_datawarehouse"}]
    assert [
        x["name"] for x in service.list_tables("dbpcm_datawarehouse", trino_settings)
    ] == ["employee"]
    schema = service.get_table_schema("dbpcm_datawarehouse", "employee", trino_settings)
    assert schema["database"] == "dbpcm_datawarehouse"
    assert [c["name"] for c in schema["columns"]] == ["employee_code"]
    discovery_connection(monkeypatch, [])
    assert service.list_databases(trino_settings) == []
    assert service.list_tables("dbpcm_datawarehouse", trino_settings) == []


def test_discovery_resolves_before_network(monkeypatch, trino_settings, principal):
    connect = MagicMock()
    monkeypatch.setattr(trino_client, "_connect", connect)
    trino_settings.trino_shard_routes = {}
    with pytest.raises(QueryValidationError, match="No MySQL shard"):
        service.list_databases(trino_settings)
    connect.assert_not_called()


def test_discovery_failure_is_sanitized_and_closed(
    monkeypatch, trino_settings, principal
):
    from app import trino_metadata

    _, conn, cursor = discovery_connection(monkeypatch, [])
    cursor.execute.side_effect = RuntimeError("secret endpoint")
    with pytest.raises(ClickHouseUnavailableError) as error:
        trino_metadata.discover(trino_settings)
    assert error.value.code == "TRINO_DISCOVERY_UNAVAILABLE"
    assert "secret" not in str(error.value)
    cursor.close.assert_called_once()
    conn.close.assert_called_once()


def test_public_alias_routes_with_default_view_catalog(trino_settings, principal):
    trino_settings.trino_mysql_view_catalog = Settings(
        _env_file=None
    ).trino_mysql_view_catalog
    assert tables(
        trino_client.rewrite_query(
            "SELECT dbpcm_datawarehouse.employee.employee_code FROM dbpcm_datawarehouse.employee",
            trino_settings,
        )
    ) == {("dbpcm_datawarehouse", "mysql_shard_07", "employee")}


def test_public_alias_preserves_scope_checks(
    monkeypatch, trino_settings, principal, authored_discovery
):
    from app.errors import ColumnScopeError
    from app.principal import current_scope

    monkeypatch.setattr("app.catalog.get_settings", lambda: trino_settings)
    execute = MagicMock(return_value=(["employee_code"], [["E1"]]))
    monkeypatch.setattr(trino_client, "execute_query", execute)
    token = current_scope.set(frozenset({"dbpcm_warehouse.employee.employee_code"}))
    try:
        service.run_query(
            "SELECT employee_code FROM dbpcm_datawarehouse.employee",
            settings=trino_settings,
            include_provenance=True,
        )
        execute.reset_mock()
        with pytest.raises(ColumnScopeError):
            service.run_query(
                "SELECT employee_name FROM dbpcm_datawarehouse.employee",
                settings=trino_settings,
                include_provenance=True,
            )
        execute.assert_not_called()
    finally:
        current_scope.reset(token)


def test_discovery_is_fresh_for_each_tenant(monkeypatch, trino_settings):
    from app.trino_metadata import discover

    trino_settings.trino_mysql_view_catalog = "dbpcm_datawarehouse"
    connect, _, cursor = discovery_connection(
        monkeypatch, [["employee", "employee_code", "varchar"]]
    )
    for pc, shard in [("pc1", "mysql_shard_07"), ("pc2", "mysql_shard_08")]:
        token = current_principal.set(
            Principal(
                "subject",
                {"proc_center": pc, "clientcode": "10R37", "user_name": "alice"},
            )
        )
        try:
            discover(trino_settings, "dbpcm_datawarehouse", "employee")
            assert connect.call_args.args[1] == f"{pc}|10R37|alice"
            assert cursor.execute.call_args_list[-2].args[1] == [shard, "employee"]
        finally:
            current_principal.reset(token)
    assert connect.call_count == 2


def test_public_alias_allowlist_and_explain(
    monkeypatch, trino_settings, principal, authored_discovery
):
    trino_settings.allowed_databases = "dbpcm_datawarehouse"
    trino_settings.trino_mysql_view_catalog = "dbpcm_datawarehouse"
    assert tables(
        trino_client.rewrite_query(
            "SELECT employee_code FROM dbpcm_datawarehouse.employee", trino_settings
        )
    ) == {("dbpcm_datawarehouse", "mysql_shard_07", "employee")}
    assert service.list_tables("dbpcm_datawarehouse", trino_settings)
    execute = MagicMock(return_value=(["plan"], [["plan text"]]))
    monkeypatch.setattr(trino_client, "execute_query", execute)
    service.explain_query(
        "SELECT employee_code FROM dbpcm_datawarehouse.employee",
        settings=trino_settings,
    )
    assert "dbpcm_warehouse.employee" in execute.call_args.args[0]
    assert execute.call_args.args[0].startswith("EXPLAIN")


def test_physical_view_cannot_bypass_routing(trino_settings, principal):
    with pytest.raises(QueryValidationError):
        trino_client.rewrite_query(
            "SELECT * FROM dbpcm_datawarehouse.mysql_shard_08.employee", trino_settings
        )
