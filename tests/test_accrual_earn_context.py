"""Authored current-client earning context: effective dates and client isolation.

SQLite exercises the relational selection on typed fixture values; Trino-specific
routing and parameter binding are covered in test_catalog_context.
"""
import sqlite3

import pytest
import sqlglot

from app.semantic_catalog import get_semantic_catalog
from app.semantic_catalog.export import build_catalog_export


@pytest.fixture
def authored_entry():
    entry = get_semantic_catalog()["dbpcm_warehouse.timeoff_requests"]
    if not entry["columns"]["earn_code"].get("context_sql"):
        pytest.skip("Optional local accrual context SQL is not configured")
    return entry


@pytest.fixture
def database():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE acrl_ee_acrl (clientcode TEXT, accrual_baseid INTEGER, status INTEGER);
        CREATE TABLE acrl_effdt_acrl_definition (
            clientcode TEXT, base_id INTEGER, effid INTEGER, effdate TEXT, is_deleted INTEGER
        );
        CREATE TABLE acrl_definition_accrual_earnings (effid INTEGER, earncode TEXT);
        CREATE TABLE cl_earn (clientcode TEXT, earncode TEXT, earndesc TEXT);
        INSERT INTO acrl_ee_acrl VALUES
            ('A',1,1), ('A',1,1), ('A',2,0), ('B',1,1), ('A',3,1);
        INSERT INTO acrl_effdt_acrl_definition VALUES
            ('A',1,10,'2000-01-01',0), ('A',1,11,'2001-01-01',0),
            ('A',1,12,'2001-01-01',0), ('A',1,13,'2999-01-01',0),
            ('A',1,14,'2002-01-01',1), ('A',2,20,'2000-01-01',0),
            ('B',1,30,'2000-01-01',0), ('A',3,40,'2999-01-01',0);
        INSERT INTO acrl_definition_accrual_earnings VALUES
            (10,'OLD'), (11,'TIE'), (12,'VAC'), (12,'PTO'), (12,'VAC'),
            (13,'FUTURE'), (14,'DELETED'), (20,'INACTIVE'), (30,'SICK'), (40,'FUTURE_ONLY');
        INSERT INTO cl_earn VALUES
            ('A','VAC','Vacation'), ('A','PTO','Paid time off'),
            ('A','OLD','Old'), ('A','TIE','Earlier same-day definition'),
            ('A','FUTURE','Future'), ('A','DELETED','Deleted'),
            ('A','INACTIVE','Inactive'), ('B','SICK','Sick leave'),
            ('B','VAC','Other client vacation'), ('A','FUTURE_ONLY','Not yet effective');
    """)
    yield conn
    conn.close()


@pytest.mark.parametrize("client,expected", [
    ("A", [("active_accrual_earnings", "PTO", "Paid time off"),
           ("active_accrual_earnings", "VAC", "Vacation")]),
    ("B", [("active_accrual_earnings", "SICK", "Sick leave")]),
    ("A' OR '1'='1", []),
])
def test_current_active_client_earnings(database, client, expected, authored_entry):
    entry = authored_entry
    sql = entry["columns"]["earn_code"]["context_sql"]
    # Flags in this fixture are integers: SQLite CAST matches Trino TRY_CAST.
    rendered = sqlglot.transpile(sql, read="trino", write="sqlite")[0]
    assert database.execute(rendered, {"clientcode": client}).fetchall() == expected


def test_authored_context_sql_is_not_exported():
    exported = build_catalog_export()["catalog"]["dbpcm_warehouse.timeoff_requests"]
    assert "context_sql" not in exported["columns"]["earn_code"]


def test_authored_lookup_uses_trino_caller_identity_and_bound_client(monkeypatch, authored_entry):
    from unittest.mock import MagicMock
    from app.catalog_context import execute_context_sql
    from app.config import Settings
    from app.principal import Principal, current_principal

    settings = Settings(_env_file=None, trino_enabled=True, trino_host="test",
                        trino_security_ready=True, employee_access_enabled=False)
    entry = authored_entry
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.description = [("context_key",), ("earn_code",), ("earn_description",)]
    cursor.fetchmany.side_effect = [[], ]
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr("app.trino_client._connect", connect)
    monkeypatch.setattr("app.shard_routing.resolve_mysql_shard", lambda client, _: "routed_shard")
    token = current_principal.set(Principal("alice", {
        "proc_center": "pc", "clientcode": "A'quoted", "user_name": "alice",
    }))
    try:
        execute_context_sql(entry["columns"]["earn_code"]["context_sql"], entry, settings)
    finally:
        current_principal.reset(token)
    connect.assert_called_once_with(settings, "pc|A'quoted|alice", mysql_catalog="routed_shard")
    rendered, parameters = cursor.execute.call_args.args
    assert parameters == ["A'quoted", "A'quoted"]
    assert "A'quoted" not in rendered
    assert '"routed_shard"."dbonline".cl_earn' in rendered
    cursor.close.assert_called_once()
    connection.close.assert_called_once()
