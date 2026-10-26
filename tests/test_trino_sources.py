"""Source policy rendering is server-owned, tenant-bound, and fail closed."""

from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from app.config import Settings
from app.errors import QueryValidationError
from app.principal import Principal, current_principal
from app.trino_sources import clickhouse_source, mysql_source


MYSQL_SQL = """
SELECT e.employee_code, SPLIT_PART(CURRENT_USER, '|', 1) AS proc_center
FROM {{SHARD_CATALOG}}.dbonline.actual_events e
WHERE e.clientcode = SPLIT_PART(CURRENT_USER, '|', 2)
AND EXISTS (SELECT 1 FROM {{SHARD_CATALOG}}.dbonline.on_eeaccess a
 WHERE a.clientcode = e.clientcode AND a.eecode = e.employee_code
 AND a.username = SPLIT_PART(CURRENT_USER, '|', 3))
"""
ENTRY = {"database": "dbpcm_warehouse", "table": "timeoff_requests", "source": "mysql"}


@pytest.fixture
def source_settings(tmp_path):
    (tmp_path / "timeoff_requests.txt").write_text(MYSQL_SQL)
    return Settings(_env_file=None, employee_access_enabled=False,
                    trino_mysql_sources_dir=str(tmp_path))


@pytest.mark.parametrize("client,username", [("A", "alice"), ("B", "bob"), ("A", "o'brien\\test"), ("A", "a' OR '1'='1")])
def test_mysql_renders_identity_as_literals(source_settings, client, username):
    relation = mysql_source(ENTRY, "online__OKC__01__dbonline", ("pc", client, username), source_settings)
    sql = relation.sql(dialect="trino")
    tree = sqlglot.parse_one("SELECT * FROM " + sql + " AS events", read="trino")
    assert {(t.catalog, t.db, t.name) for t in tree.find_all(exp.Table)} == {
        ("online__OKC__01__dbonline", "dbonline", "actual_events"),
        ("online__OKC__01__dbonline", "dbonline", "on_eeaccess"),
    }
    assert not list(tree.find_all(exp.CurrentUser))
    assert username in {l.this for l in tree.find_all(exp.Literal) if l.is_string}
    assert client in {l.this for l in tree.find_all(exp.Literal) if l.is_string}
    assert not list(tree.find_all(exp.Or))


def test_rendering_never_reuses_another_user(source_settings):
    first = mysql_source(ENTRY, "shard_a", ("pc", "A", "alice"), source_settings).sql(dialect="trino")
    second = mysql_source(ENTRY, "shard_b", ("pc", "B", "bob"), source_settings).sql(dialect="trino")
    assert "shard_a" not in second and "'alice'" not in second
    assert "shard_b" not in first and "'bob'" not in first


@pytest.mark.parametrize("definition", [None, "", "CREATE VIEW x AS SELECT 1", "SELECT 1; SELECT 2",
    "SELECT * FROM other.dbonline.actual_events", "SELECT * FROM {{SHARD_CATALOG}}.other.actual_events",
    "SELECT * FROM {{UNKNOWN}}.dbonline.actual_events", "SELECT * FROM actual_events",
    "SELECT * FROM TABLE(other.system.query(query => 'SELECT 1'))"])
def test_invalid_or_missing_sources_do_not_fall_back(source_settings, definition):
    path = Path(source_settings.trino_mysql_sources_dir) / "timeoff_requests.txt"
    if definition is None:
        path.unlink()
    else:
        path.write_text(definition)
    with pytest.raises(QueryValidationError) as caught:
        mysql_source(ENTRY, "shard_a", ("pc", "A", "alice"), source_settings)
    assert caught.value.code == "TRINO_SOURCE_DEFINITION_INVALID"
    assert str(path) not in str(caught.value)


def test_labor_spelling_uses_supplied_labour_file(source_settings):
    path = Path(source_settings.trino_mysql_sources_dir) / "labour_allocation.txt"
    path.write_text(MYSQL_SQL)
    assert mysql_source({**ENTRY, "table": "labor_allocation"}, "shard_a", ("pc", "A", "alice"), source_settings)


def test_lowercase_identifier_placeholders(source_settings):
    path = Path(source_settings.trino_mysql_sources_dir) / "timeoff_requests.txt"
    path.write_text("SELECT * FROM {{shard}}.{{database}}.actual_events WHERE clientcode = {{CLIENT_CODE}}")
    sql = mysql_source(ENTRY, "shard_a", ("pc", "A", "alice"), source_settings).sql(dialect="trino")
    assert '"shard_a"."dbonline".actual_events' in sql and "'A'" in sql


def test_clickhouse_native_query_preserves_final_identity_and_caps(source_settings):
    jti = "alice'\\identity"
    token = current_principal.set(Principal("alice", {"clientcode": "A", "proc_center": "pc", "jti": jti}))
    try:
        source_settings.clickhouse_select_final = False
        relation = clickhouse_source({"database": "dbpcm_warehouse", "table": "payroll"}, source_settings)
        outer = sqlglot.parse_one("SELECT * FROM " + relation.sql(dialect="trino"), read="trino")
        native_sql = next(l.this for l in outer.find_all(exp.Literal) if l.is_string)
        native = sqlglot.parse_one(native_sql, read="clickhouse")
        assert "`final` = 1" in native_sql
        assert "readonly = 2" in native_sql and "max_execution_time" in native_sql
        assert jti in {l.this for l in native.find_all(exp.Literal) if l.is_string}
        assert "paycom_authenticated_user" in native_sql
        assert {(t.db, t.name) for t in native.find_all(exp.Table)} == {("dbpcm_warehouse", "payroll")}
    finally:
        current_principal.reset(token)


def test_clickhouse_missing_identity_is_closed(source_settings):
    token = current_principal.set(None)
    try:
        with pytest.raises(QueryValidationError, match="identity"):
            clickhouse_source({"database": "dbpcm_warehouse", "table": "payroll"}, source_settings)
    finally:
        current_principal.reset(token)


def test_unsupported_mysql_catalog_entries_are_archived():
    from app.semantic_catalog.loader import load_semantic_catalog
    catalog = load_semantic_catalog()
    assert {entry["table"] for entry in catalog.values() if entry.get("source") == "mysql"} == {
        "employee", "department", "timeoff_requests", "timeoff_balances", "expense", "labor_allocation",
    }
    archive = Path(__file__).parents[1] / "app/semantic_catalog/archive"
    archived = load_semantic_catalog(archive)
    assert len(archived) == 6
    assert not (catalog.keys() & archived.keys())


def test_public_timeoff_name_accepts_existing_private_template_filename(source_settings):
    directory = Path(source_settings.trino_mysql_sources_dir)
    (directory / 'timeoff_requests.txt').rename(directory / 'accrual_events.txt')
    sql = mysql_source(ENTRY, 'shard_a', ('pc', 'A', 'alice'), source_settings).sql(dialect='trino')
    assert 'actual_events' in sql
    assert "'alice'" in sql
