"""Backend isolation, source routing and local provisioning contract.

MySQL shard routing follows the paycompy client-code RANGE model: the
authenticated client_code (5-char alphanumeric) is matched inclusively against
the clientstart/clientend ranges of a trusted registry snapshot (written here to
a tmp path per test), filtered by env_type (TRINO_ENV_TYPE) and database
(TRINO_MYSQL_SCHEMA). proc_center remains a required identity claim but is NOT
a routing input.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
import sqlglot
from pydantic import ValidationError
from sqlglot import exp

from app import service, trino_client
from app.config import Settings
from app.errors import (
    ClickHouseQueryError,
    ClickHouseUnavailableError,
    QueryValidationError,
)
from app.principal import Principal, current_principal
from app.semantic_catalog.loader import CatalogValidationError, load_semantic_catalog
from app.shard_routing import (
    _resolve_from_entries,
    invalidate_shard_registry_cache,
    resolve_mysql_shard,
)
from scripts import setup_trino_catalogs
from tests.view_fixtures import TINY_VIEW
from scripts.setup_trino_catalogs import (
    load_env_file,
    load_view_templates,
    render_catalogs,
    render_view_catalog,
    render_views,
    write_catalogs,
    write_view_catalog,
    write_views,
)

# Trusted server-side snapshot fixture. Two non-adjacent PROD ranges exercise
# in-range and out-of-range resolution; QA2 and dbpcm entries exercise the
# env_type/mysqldb filter dimensions (they overlap the PROD ranges and would
# make routing ambiguous if the filters were dropped).
SHARD_SNAPSHOT = {
    "generated_at": "2026-01-01T00:00:00+00:00",
    "source": "tests/test_trino.py fixture",
    "mysql_shards": [
        {
            "catalog": "mysql_shard_prod_01",
            "host": "mysql-a.internal",
            "port": 3306,
            "database": "dbonline",
            "env_type": "PROD",
            "clientstart": "00000",
            "clientend": "10ZZZ",
        },
        {
            "catalog": "mysql_shard_prod_02",
            "host": "mysql-b.internal",
            "port": 3306,
            "database": "dbonline",
            "env_type": "PROD",
            "clientstart": "11000",
            "clientend": "1ZZZZ",
        },
        {
            "catalog": "mysql_shard_qa2_01",
            "host": "mysql-qa.internal",
            "port": 3306,
            "database": "dbonline",
            "env_type": "QA2",
            "clientstart": "00000",
            "clientend": "ZZZZZ",
        },
        {
            "catalog": "mysql_shard_prod_03",
            "host": "mysql-c.internal",
            "port": 3307,
            "database": "dbpcm",
            "env_type": "PROD",
            "clientstart": "00000",
            "clientend": "ZZZZZ",
        },
    ],
    "clickhouse": {"catalog": "clickhouse", "host": "ch.internal", "port": 8123},
}


def _write_snapshot(path, snapshot=None):
    path.write_text(json.dumps(snapshot if snapshot is not None else SHARD_SNAPSHOT), encoding="utf-8")
    return path


@pytest.fixture
def shard_registry_path(tmp_path):
    return _write_snapshot(tmp_path / "shards.json")


@pytest.fixture(autouse=True)
def _fresh_registry_cache():
    """Registry snapshots are process-cached per path; reset around each test."""
    invalidate_shard_registry_cache()
    yield
    invalidate_shard_registry_cache()


def make_settings(registry_path: str, **overrides) -> Settings:
    base = dict(
        _env_file=None,
        trino_enabled=True,
        trino_mysql_view_catalog="",
        trino_security_ready=True,
        trino_http_scheme="http",
        trino_shard_registry_path=registry_path,
        employee_access_enabled=False,
        allowed_databases="dbpcm_warehouse",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def trino_settings(shard_registry_path):
    return make_settings(str(shard_registry_path))


@pytest.fixture
def principal():
    token = current_principal.set(
        Principal(
            "subject",
            {
                "proc_center": "pc1",
                "clientcode": "10R37",
                "user_name": "alice",
                "jti": "alice-jti",
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
        ("mysql_shard_prod_01", "dbonline", "employee"),
        ("", "", ""),  # Trusted TABLE(clickhouse.system.query(...)).
    }
    assert '"clickhouse".system.query' in sql
    assert '`dbpcm_warehouse`.`payroll`' in sql
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
    assert sql.count('"mysql_shard_prod_01"."dbonline".employee') == 2
    assert "FROM employee AS employee" in sql
    assert sql.count("'employee'") == 2


def test_self_join_preserves_aliases(trino_settings, principal):
    sql = trino_client.rewrite_query(
        "SELECT a.employee_code FROM employee a JOIN employee b "
        "ON a.employee_code = b.employee_code",
        trino_settings,
    )
    assert "AS a" in sql and "AS b" in sql
    assert sql.count('"mysql_shard_prod_01"') == 2


def test_view_catalog_mode(trino_settings, principal):
    trino_settings.trino_mysql_view_catalog = "dbpcm_datawarehouse"
    assert tables(
        trino_client.rewrite_query("SELECT * FROM employee", trino_settings)
    ) == {("dbpcm_datawarehouse", "mysql_shard_prod_01", "employee")}


def test_explain_and_count_translation(trino_settings, principal):
    sql = trino_client.rewrite_query(
        "EXPLAIN SELECT count() FROM employee", trino_settings
    )
    assert sql.startswith("EXPLAIN SELECT COUNT(*)")
    assert "mysql_shard_prod_01" in sql


# --- ClickHouse -> Trino function mapping (uniqExact -> COUNT(DISTINCT)) -----
# Logical SQL is validated as ClickHouse dialect, so ClickHouse-only functions
# legitimately reach the transpiler; sqlglot has no Trino mapping for them and
# passes their names through verbatim (uniqExact rendered as UNIQEXACT), dying
# on the coordinator with FUNCTION_NOT_FOUND — this broke
# bp-active-headcount-by-department (TRINO_QUERY_ERROR, invisible server-side
# until the logging tests below). The map must preserve EXACT semantics
# (never approximate), and anything unmapped keeps the existing passthrough
# (the coordinator rejects it loudly).


def test_uniqexact_maps_to_exact_count_distinct(trino_settings, principal):
    sql = trino_client.rewrite_query(
        "SELECT uniqExact(employee_code) FROM employee", trino_settings
    )
    assert "COUNT(DISTINCT employee_code)" in sql
    assert "UNIQEXACT" not in sql.upper()
    # Exact semantics — never silently degraded to an approximation.
    assert "APPROX" not in sql.upper()


def test_uniqexact_nested_in_aggregate_query(trino_settings, principal):
    sql = trino_client.rewrite_query(
        "SELECT department_code, uniqExact(employee_code) AS active_count "
        "FROM employee WHERE employee_status = 'Active' "
        "GROUP BY department_code ORDER BY active_count DESC, department_code",
        trino_settings,
    )
    assert "COUNT(DISTINCT employee_code) AS active_count" in sql
    assert "GROUP BY department_code" in sql
    assert "ORDER BY active_count DESC, department_code" in sql
    assert "UNIQEXACT" not in sql.upper()


def test_unmapped_functions_pass_through_verbatim(trino_settings, principal):
    # A non-mapped ClickHouse aggregate keeps existing behavior: verbatim
    # passthrough, loud coordinator rejection if unsupported.
    sql = trino_client.rewrite_query(
        "SELECT uniqCombined64(employee_code) FROM employee", trino_settings
    )
    assert "UNIQCOMBINED64(employee_code)" in sql
    # The multi-argument form of a mapped name is also left untouched:
    # ClickHouse's uniqExact(a, b) tuple semantics have no one-statement
    # Trino equivalent, so it must NOT be folded into COUNT(DISTINCT).
    sql = trino_client.rewrite_query(
        "SELECT uniqExact(department_code, employee_code) FROM employee",
        trino_settings,
    )
    assert "UNIQEXACT(department_code, employee_code)" in sql


def test_uniqexact_mapping_covers_explain_path(trino_settings, principal):
    sql = trino_client.rewrite_query(
        "EXPLAIN SELECT uniqExact(employee_code) FROM employee", trino_settings
    )
    assert sql.startswith("EXPLAIN")
    assert "COUNT(DISTINCT employee_code)" in sql


# --- Exact-safe cast and predicate-aggregate mappings -------------------------
# The corpus sweep proved sqlglot passes these functions through verbatim and
# they die on the coordinator with FUNCTION_NOT_FOUND (TODATE32 alone has 34
# call sites across 9+ blueprints). Each builder below is asserted by emitting
# a minimal logical query through rewrite_query and checking the stable
# Trino shape; the corpus spellings these protect live in
# app/corpus/data/blueprints/ (bp-department-scorecard,
# bp-compare-employee-paychecks-two-periods et al.).


def test_todate32_maps_to_cast_date_in_sentinel_guard(trino_settings, principal):
    # Mirrors the corpus's missing_date_sentinel guard verbatim.
    sql = trino_client.rewrite_query(
        "SELECT employee_code FROM employee "
        "WHERE toDate32(leave_absence_start) > toDate32('1900-01-01')",
        trino_settings,
    )
    assert "CAST(leave_absence_start AS DATE)" in sql
    assert "CAST('1900-01-01' AS DATE)" in sql
    assert "TODATE32" not in sql.upper()


def test_tofloat64_maps_to_cast_double(trino_settings, principal):
    sql = trino_client.rewrite_query(
        "SELECT toFloat64(annual_salary) / 2080.0 FROM employee", trino_settings
    )
    assert "CAST(annual_salary AS DOUBLE)" in sql
    assert "TOFLOAT64" not in sql.upper()


def test_tostring_maps_to_cast_varchar(trino_settings, principal):
    sql = trino_client.rewrite_query(
        "SELECT toString(employee_code) FROM employee", trino_settings
    )
    assert "CAST(employee_code AS VARCHAR)" in sql
    assert "TOSTRING" not in sql.upper()


def test_touint8_wraps_and_truncates_numeric_inputs(trino_settings, principal):
    # Remainder precedes truncation so large integral values retain low bits.
    sql = trino_client.rewrite_query(
        "SELECT toUInt8(direct_deposit) FROM employee", trino_settings
    )
    assert "TRUNCATE((direct_deposit) % 256)" in sql
    assert "+ 256) % 256 AS INTEGER)" in sql
    assert "TOUINT8" not in sql.upper()


def test_sumif_maps_to_coalesced_filtered_sum(trino_settings, principal):
    # ClickHouse sumIf returns 0 on an empty matching set and the corpus
    # pivot relies on it (bp-compare-employee-paychecks-two-periods computes
    # sumIf(b) - sumIf(a) for line items present in only one period); Trino's
    # bare SUM(...) FILTER would return NULL, so the COALESCE is semantic.
    sql = trino_client.rewrite_query(
        "SELECT type_code, sumIf(amount, register_type = 'R') "
        "- sumIf(amount, register_type = 'A') FROM payroll GROUP BY type_code",
        trino_settings,
    )
    assert "COALESCE(SUM(amount) FILTER(WHERE register_type = 'R'), 0)" in sql
    assert "COALESCE(SUM(amount) FILTER(WHERE register_type = 'A'), 0)" in sql
    assert "SUMIF" not in sql.upper()


def test_avgif_maps_to_filtered_avg_without_coalesce(trino_settings, principal):
    # Documented empty-set delta: ClickHouse avgIf -> NaN, Trino -> NULL; the
    # corpus consumes it as a terminal display column, so no compensation.
    sql = trino_client.rewrite_query(
        "SELECT avgIf(annual_salary, employee_status = 'Active') FROM employee",
        trino_settings,
    )
    assert "AVG(annual_salary) FILTER(WHERE employee_status = 'Active')" in sql
    assert "COALESCE(AVG" not in sql.upper()
    assert "AVGIF" not in sql.upper()


def test_multiif_maps_to_case(trino_settings, principal):
    sql = trino_client.rewrite_query(
        "SELECT multiIf(employee_status = 'Active', 'A', "
        "employee_status = 'On Leave', 'L', 'X') FROM employee",
        trino_settings,
    )
    assert (
        "CASE WHEN employee_status = 'Active' THEN 'A' "
        "WHEN employee_status = 'On Leave' THEN 'L' ELSE 'X' END" in sql
    )
    assert "MULTIIF" not in sql.upper()


def test_addmonths_maps_to_date_add_with_swapped_arguments(trino_settings, principal):
    # ClickHouse (datetime, months) vs Trino date_add(unit, value, datetime):
    # asserting the rendered ORDER pins the swap. sqlglot renders the unit
    # upper-quoted; Trino time units are case-insensitive.
    sql = trino_client.rewrite_query(
        "SELECT addMonths(hire_date, 3) FROM employee", trino_settings
    )
    assert "DATE_ADD('MONTH', 3, hire_date)" in sql
    assert "ADDMONTHS" not in sql.upper()


def test_concatwithseparator_preserves_null_propagation(trino_settings, principal):
    # ClickHouse propagates NULL; Trino's bare CONCAT_WS skips it.
    sql = trino_client.rewrite_query(
        "SELECT concatWithSeparator('-', employee_status, gender) FROM employee",
        trino_settings,
    )
    assert "CONCAT_WS('-', employee_status, gender)" in sql
    assert "IS NULL OR" in sql.upper()
    assert "CONCATWITHSEPARATOR" not in sql.upper()


@pytest.mark.parametrize(
    "sql, verbatim",
    [
        # Wrong-arity calls of MAPPED names stay verbatim: the map never
        # guesses semantics — the coordinator rejects loudly.
        ("SELECT toDate32(hire_date, hire_date) FROM employee",
         "TODATE32(hire_date, hire_date)"),
        ("SELECT toFloat64(annual_salary, 1) FROM employee",
         "TOFLOAT64(annual_salary, 1)"),
        ("SELECT toString(employee_code, employee_code) FROM employee",
         "TOSTRING(employee_code, employee_code)"),
        ("SELECT toUInt8() FROM employee", "TOUINT8()"),
        ("SELECT sumIf(gross_pay) FROM payroll", "SUMIF(gross_pay)"),
        ("SELECT avgIf(annual_salary, employee_status, gender) FROM employee",
         "AVGIF(annual_salary, employee_status, gender)"),
        ("SELECT addMonths(hire_date) FROM employee", "ADDMONTHS(hire_date)"),
        ("SELECT addMonths(hire_date, 1, 2) FROM employee",
         "ADDMONTHS(hire_date, 1, 2)"),
        # multiIf needs ODD arity >= 3 (cond/value pairs + else); even counts
        # are invalid in ClickHouse itself.
        ("SELECT multiIf(employee_status, gender) FROM employee",
         "MULTIIF(employee_status, gender)"),
        ("SELECT multiIf(employee_status = 'A', 'A', gender = 'M', 'M') FROM employee",
         "MULTIIF(employee_status = 'A', 'A', gender = 'M', 'M')"),
        ("SELECT concatWithSeparator('-') FROM employee", "CONCATWITHSEPARATOR('-')"),
    ],
)
def test_arity_mismatch_leaves_mapped_names_untouched(sql, verbatim, trino_settings, principal):
    assert verbatim in trino_client.rewrite_query(sql, trino_settings)


def test_quantileexact_stays_unmapped_by_construction(trino_settings, principal):
    # The parametric double-call parses as exp.ParameterizedAgg, which the
    # mapping walk does not even visit — kept unmapped pending the quantile
    # owner decision (Trino has no exact percentile aggregate).
    sql = trino_client.rewrite_query(
        "SELECT quantileExactLow(0.5)(annual_salary) FROM employee", trino_settings
    )
    assert "QUANTILEEXACTLOW" in sql.upper()
    assert "APPROX_PERCENTILE" not in sql.upper()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM mysql_shard_prod_02.dbonline.employee",
        "SELECT * FROM unknown",
        "SELECT * FROM dbpcm_scratch.upload",
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


# --- client-code RANGE shard resolution --------------------------------------


def test_in_range_client_code_routes_to_its_catalog(trino_settings):
    assert resolve_mysql_shard("10R37", trino_settings) == "mysql_shard_prod_01"
    assert resolve_mysql_shard("12ABC", trino_settings) == "mysql_shard_prod_02"


def test_range_boundaries_are_inclusive(trino_settings):
    assert resolve_mysql_shard("00000", trino_settings) == "mysql_shard_prod_01"
    assert resolve_mysql_shard("10ZZZ", trino_settings) == "mysql_shard_prod_01"
    assert resolve_mysql_shard("11000", trino_settings) == "mysql_shard_prod_02"
    assert resolve_mysql_shard("1ZZZZ", trino_settings) == "mysql_shard_prod_02"


def test_outside_all_ranges_fails_closed(trino_settings):
    # Below the lowest range start (lexicographic prefix sorts first) ...
    with pytest.raises(QueryValidationError) as exc:
        resolve_mysql_shard("0000", trino_settings)
    assert exc.value.code == "MYSQL_SHARD_NOT_FOUND"
    assert exc.value.message == "No MySQL shard route is configured for the authenticated tenant."
    # ... and above the highest range end (only QA2/dbpcm rows cover "ZZZZ*").
    with pytest.raises(QueryValidationError) as exc:
        resolve_mysql_shard("ZZZZ0", trino_settings)
    assert exc.value.code == "MYSQL_SHARD_NOT_FOUND"
    with pytest.raises(QueryValidationError) as exc:
        resolve_mysql_shard("", trino_settings)
    assert exc.value.code == "MYSQL_SHARD_NOT_FOUND"


def test_overlapping_ranges_fail_closed_ambiguous():
    entries = [
        {"catalog": "a_shard", "env_type": "PROD", "database": "dbonline", "clientstart": "00000", "clientend": "FFFFF"},
        {"catalog": "b_shard", "env_type": "PROD", "database": "dbonline", "clientstart": "AAAAA", "clientend": "ZZZZZ"},
    ]
    with pytest.raises(QueryValidationError) as exc:
        _resolve_from_entries("B0000", entries, "PROD", "dbonline")
    assert exc.value.code == "MYSQL_SHARD_AMBIGUOUS"
    # Generic message: registry contents are never echoed to the caller.
    assert "a_shard" not in exc.value.message and "b_shard" not in exc.value.message


def test_env_type_filter(trino_settings):
    # "ZZZZZ" is covered only by the QA2 entry; PROD settings must not see it.
    with pytest.raises(QueryValidationError) as exc:
        resolve_mysql_shard("ZZZZZ", trino_settings)
    assert exc.value.code == "MYSQL_SHARD_NOT_FOUND"
    trino_settings.trino_env_type = "QA2"
    assert resolve_mysql_shard("ZZZZZ", trino_settings) == "mysql_shard_qa2_01"


def test_mysqldb_filter(trino_settings):
    # Default dbonline routing; switching the schema filter switches the shard.
    assert resolve_mysql_shard("10R37", trino_settings) == "mysql_shard_prod_01"
    trino_settings.trino_mysql_schema = "dbpcm"
    assert resolve_mysql_shard("10R37", trino_settings) == "mysql_shard_prod_03"


def test_null_clientstart_entries_are_skipped():
    entries = [
        {"catalog": "null_range", "env_type": "PROD", "database": "dbonline", "clientstart": None, "clientend": None},
    ]
    with pytest.raises(QueryValidationError) as exc:
        _resolve_from_entries("10R37", entries, "PROD", "dbonline")
    assert exc.value.code == "MYSQL_SHARD_NOT_FOUND"


def test_empty_range_strings_never_match(tmp_path):
    snapshot = json.loads(json.dumps(SHARD_SNAPSHOT))
    snapshot["mysql_shards"] = [
        {
            "catalog": "empty_range",
            "host": "mysql-x.internal",
            "port": 3306,
            "database": "dbonline",
            "env_type": "PROD",
            "clientstart": "",
            "clientend": "",
        }
    ]
    path = _write_snapshot(tmp_path / "shards.json", snapshot)
    settings = make_settings(str(path))
    with pytest.raises(QueryValidationError) as exc:
        resolve_mysql_shard("10R37", settings)
    assert exc.value.code == "MYSQL_SHARD_NOT_FOUND"


def test_malicious_catalog_name_rejected(tmp_path):
    snapshot = json.loads(json.dumps(SHARD_SNAPSHOT))
    snapshot["mysql_shards"][0]["catalog"] = "mysql_bad; DROP TABLE x"
    path = _write_snapshot(tmp_path / "shards.json", snapshot)
    settings = make_settings(str(path))
    with pytest.raises(QueryValidationError) as exc:
        resolve_mysql_shard("10R37", settings)
    assert exc.value.code == "MYSQL_SHARD_REGISTRY_INVALID"


@pytest.mark.parametrize(
    "contents",
    [
        None,  # missing file
        "{not json",
        "[]",
        '{"nope": []}',
        '{"mysql_shards": [{"catalog": 7}]}',
        '{"mysql_shards": [{"catalog": "c", "host": "h", "port": "3306", "database": "dbonline", "env_type": "PROD", "clientstart": "00000", "clientend": "00999"}]}',
        '{"mysql_shards": [{"catalog": "c", "host": "h", "port": 3306, "database": "dbonline", "env_type": "PROD", "clientstart": "00000", "clientend": null}]}',
        '{"mysql_shards": "not-a-list"}',
    ],
    ids=["missing", "bad-json", "not-object", "no-shards-key", "bad-field-type", "bad-port-type", "start-without-end", "shards-not-list"],
)
def test_missing_or_invalid_registry_fails_closed(tmp_path, contents):
    path = tmp_path / "shards.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    settings = make_settings(str(path))
    with pytest.raises(QueryValidationError) as exc:
        resolve_mysql_shard("10R37", settings)
    assert exc.value.code == "MYSQL_SHARD_REGISTRY_INVALID"


def test_proc_center_is_not_a_routing_input(trino_settings):
    # A proc_center unknown to any route still resolves: client_code alone routes.
    token = current_principal.set(
        Principal("s", {"proc_center": "pc-unknown", "clientcode": "12ABC", "user_name": "bob"})
    )
    try:
        sql = trino_client.rewrite_query("SELECT * FROM employee", trino_settings)
    finally:
        current_principal.reset(token)
    assert '"mysql_shard_prod_02"' in sql


def test_rewrite_fails_closed_for_unrouted_tenant(trino_settings):
    token = current_principal.set(
        Principal("s", {"proc_center": "pc1", "clientcode": "ZZZZZ", "user_name": "alice"})
    )
    try:
        with pytest.raises(QueryValidationError) as exc:
            trino_client.rewrite_query("SELECT * FROM employee", trino_settings)
        assert exc.value.code == "MYSQL_SHARD_NOT_FOUND"
    finally:
        current_principal.reset(token)


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
    def run(client_code):
        token = current_principal.set(
            Principal("s", {"proc_center": "pc-x", "clientcode": client_code, "user_name": "u"})
        )
        try:
            return trino_client.rewrite_query("SELECT * FROM employee", trino_settings)
        finally:
            current_principal.reset(token)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, ["10R37", "12ABC"] * 5))
    for i, sql in enumerate(results):
        assert ("mysql_shard_prod_01" if i % 2 == 0 else "mysql_shard_prod_02") in sql


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


# --- bp-active-headcount-by-department regression (transpile gap) ------------
# The corpus blueprint's sql_template legitimately uses ClickHouse-dialect
# uniqExact(); it passed ClickHouse-dialect validation, transpiled with the
# name passed through verbatim, and died on the coordinator with
# FUNCTION_NOT_FOUND surfaced only as the generic TRINO_QUERY_ERROR. With the
# function-mapping pass the bound template must transpile cleanly, route both
# views, and reach Trino as the exact COUNT(DISTINCT ...).


def test_blueprint_active_headcount_transpiles_to_exact_count_distinct(
    trino_settings, principal
):
    from app.corpus.loader import load_blueprints

    blueprint = load_blueprints()["bp-active-headcount-by-department"]
    (slot,) = [s for s in blueprint["slots"] if s["name"] == "department"]
    # The omitted-slot path substitutes the slot's optional_pattern verbatim.
    bound_sql = blueprint["sql_template"].replace(
        "{department}", slot["optional_pattern"]
    )
    trino_settings.trino_mysql_view_catalog = "dbpcm_datawarehouse"
    physical = trino_client.rewrite_query(bound_sql, trino_settings)
    assert "COUNT(DISTINCT employee_code)" in physical
    assert "UNIQEXACT" not in physical.upper()
    assert tables(physical) == {
        ("dbpcm_datawarehouse", "mysql_shard_prod_01", "department"),
        ("dbpcm_datawarehouse", "mysql_shard_prod_01", "employee"),
    }


# --- Trino failure logging ----------------------------------------------------
# Transpile/validation rejections and coordinator failures were completely
# silent server-side (diagnosis needed the coordinator ledger). They are now
# logged at ERROR with the wire code + a sanitized failure class only: never
# SQL text, connector messages, identity claims, or row data — matching the
# shard_routing.py precedent (server log carries the diagnosis; the caller
# sees the unchanged generic message + code).

_IDENTITY_VALUES = ("pc1", "10R37", "alice")


def _trino_user_error(message, query_id="20261008_000000_00001_test", name="FUNCTION_NOT_FOUND"):
    from trino.exceptions import TrinoUserError

    error = {
        "errorType": "USER_ERROR",
        "errorName": name,
        "errorCode": {
            "code": 1,
            "name": name,
            "type": "USER_ERROR",
        },
        "message": message,
    }
    return TrinoUserError(error, query_id)


def _error_log_message(caplog):
    records = [
        r
        for r in caplog.records
        if r.name == "app.trino_client" and r.levelno == logging.ERROR
    ]
    assert len(records) == 1
    return records[0].getMessage()


def test_transpile_rejection_is_logged_sanitized(
    trino_settings, principal, caplog
):
    with caplog.at_level(
        logging.ERROR, logger="app.trino_client"
    ), pytest.raises(QueryValidationError) as exc:
        trino_client.rewrite_query("SHOW TABLES", trino_settings)
    assert exc.value.code == "TRINO_UNSUPPORTED_SQL"  # wire contract unchanged
    msg = _error_log_message(caplog)
    assert "code=TRINO_UNSUPPORTED_SQL" in msg
    assert "failure_class=" in msg
    assert "SHOW TABLES" not in msg  # no SQL text in the log
    for identity in _IDENTITY_VALUES:
        assert identity not in msg


def test_coordinator_rejection_is_logged_sanitized(
    monkeypatch, trino_settings, principal, caplog
):
    _, conn, cursor = mock_connection(monkeypatch, [])
    cursor.execute.side_effect = _trino_user_error(
        "Function not registered: uniqexact; auth pc1|10R37|alice"
    )
    with caplog.at_level(
        logging.ERROR, logger="app.trino_client"
    ), pytest.raises(ClickHouseQueryError) as exc:
        trino_client.execute_query(
            "SELECT uniqCombined64(employee_code) FROM employee", trino_settings
        )
    assert exc.value.code == "TRINO_QUERY_ERROR"  # wire contract unchanged
    msg = _error_log_message(caplog)
    assert "code=TRINO_QUERY_ERROR" in msg
    assert "trino_error=FUNCTION_NOT_FOUND" in msg
    assert "query_id=20261008_000000_00001_test" in msg
    # The connector's raw message (SQL fragments, identities, credentials)
    # must never reach the log.
    assert "Function not registered" not in msg
    for identity in _IDENTITY_VALUES:
        assert identity not in msg
    conn.close.assert_called_once()


def test_coordinator_rejection_carries_rewritten_hint(
    monkeypatch, trino_settings, principal
):
    # Item-2 wiring: the caller receives rewrite_trino_error's compact,
    # hint-bearing message under the unchanged TRINO_QUERY_ERROR code —
    # mirroring the ClickHouse self-correction loop (app/ch_errors.py).
    _, conn, cursor = mock_connection(monkeypatch, [])
    cursor.execute.side_effect = _trino_user_error(
        "line 1:8: Column 'hire_dat' cannot be resolved", name="COLUMN_NOT_FOUND"
    )
    with pytest.raises(ClickHouseQueryError) as exc:
        trino_client.execute_query("SELECT hire_dat FROM employee", trino_settings)
    assert exc.value.code == "TRINO_QUERY_ERROR"  # wire contract unchanged
    msg = exc.value.message
    assert msg.startswith("COLUMN_NOT_FOUND.")
    assert "hire_dat" not in msg
    assert "Hint:" in msg and "getTableSchema" in msg
    # query_id stays log-side only.
    assert "20261008_000000_00001_test" not in msg
    # The pre-Item-2 generic sentence must never resurface.
    assert "Trino rejected the query." not in msg
    conn.close.assert_called_once()


def test_coordinator_unavailable_is_logged_sanitized(
    monkeypatch, trino_settings, principal, caplog
):
    _, conn, cursor = mock_connection(monkeypatch, [])
    cursor.execute.side_effect = RuntimeError(
        "password=secret select private data"
    )
    with caplog.at_level(
        logging.ERROR, logger="app.trino_client"
    ), pytest.raises(ClickHouseUnavailableError) as exc:
        trino_client.execute_query(
            "SELECT employee_code FROM employee", trino_settings
        )
    assert exc.value.code == "TRINO_UNAVAILABLE"  # wire contract unchanged
    msg = _error_log_message(caplog)
    assert "code=TRINO_UNAVAILABLE" in msg
    assert "failure_class=RuntimeError" in msg
    assert "secret" not in msg and "private data" not in msg
    for identity in _IDENTITY_VALUES:
        assert identity not in msg
    conn.close.assert_called_once()


def test_service_flag_disabled_preserves_clickhouse(monkeypatch):
    ch = MagicMock(return_value=(["n"], [[1]]))
    trino = MagicMock()
    monkeypatch.setattr(service, "execute_query", ch)
    monkeypatch.setattr(trino_client, "execute_query", trino)
    settings = Settings(_env_file=None, trino_enabled=False, employee_access_enabled=False)
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
        "generated_at": "2026-01-01T00:00:00+00:00",
        "source": "tests/test_trino.py fixture",
        "mysql_shards": [
            {
                "catalog": "mysql_shard_prod_01",
                "host": "mysql.example",
                "port": 3306,
                "database": "dbonline",
                "env_type": "PROD",
                "clientstart": "00000",
                "clientend": "0A000",
            }
        ],
    }
    rendered = render_catalogs(inventory)["mysql_shard_prod_01"]
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
    source = {"catalog": "mysql_shard_prod_01", "host": "localhost", **patch}
    with pytest.raises(ValueError):
        render_catalogs({"mysql_shards": [source]})


def test_mysql_catalog_url_converts_zero_dates_to_null():
    # QA2 rows carry '0000-00-00' date/datetime values; without this JDBC
    # query param the MySQL connector aborts any query materializing them with
    # "Zero date value prohibited". Mapping them to NULL preserves the row.
    inventory = {
        "mysql_shards": [
            {"catalog": "mysql_shard_qa2_01", "host": "mysql.example", "port": 3306}
        ]
    }
    rendered = render_catalogs(inventory)["mysql_shard_qa2_01"]
    url_line = next(
        line for line in rendered.splitlines() if line.startswith("connection-url=")
    )
    assert "zeroDateTimeBehavior=convertToNull" in url_line
    # The URL must stay a well-formed jdbc:mysql://host:port?... line.
    assert re.fullmatch(
        r"connection-url=jdbc:mysql://[A-Za-z0-9][A-Za-z0-9.-]*:[0-9]+"
        r"\?zeroDateTimeBehavior=convertToNull",
        url_line,
    )
    # The param belongs to the MySQL branch only: clickhouse URLs keep their
    # own inventory/.env-derived shape.
    ch = render_catalogs(
        {**inventory, "clickhouse": {"catalog": "clickhouse", "host": "ch.internal"}}
    )["clickhouse"]
    assert "zeroDateTimeBehavior" not in ch


# --- clickhouse catalog .env derivation --------------------------------------
# render_catalogs(inventory, env) derives the clickhouse connection-url from
# CLICKHOUSE_HOST/PORT/SECURE/DATABASE (per-key override of the inventory
# clickhouse block). Only those four keys are consumed — everything else in
# .env, credentials above all, must never reach generated files.

CH_INVENTORY = {
    "clickhouse": {"catalog": "clickhouse", "host": "ch.internal", "port": 8123}
}


def test_clickhouse_catalog_env_override_wins(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CLICKHOUSE_HOST=ch.env.internal\nCLICKHOUSE_PORT=9440\n", encoding="utf-8"
    )
    env = load_env_file(env_file)
    inventory = {
        "mysql_shards": [
            {"catalog": "mysql_shard_prod_01", "host": "mysql.inv.internal", "port": 3306}
        ],
        **CH_INVENTORY,
    }
    rendered = render_catalogs(inventory, env=env)
    ch = rendered["clickhouse"]
    assert "connection-url=jdbc:clickhouse://ch.env.internal:9440\n" in ch
    assert "derived from .env" in ch
    # The env file must not disturb MySQL catalogs.
    assert (
        "connection-url=jdbc:mysql://mysql.inv.internal:3306"
        "?zeroDateTimeBehavior=convertToNull\n"
    ) in rendered["mysql_shard_prod_01"]


@pytest.mark.parametrize(
    "secure,expected_query",
    [
        ("true", "?ssl=true&sslmode=none"),
        ("TRUE", "?ssl=true&sslmode=none"),
        ("1", "?ssl=true&sslmode=none"),
        ("yes", "?ssl=true&sslmode=none"),
        ("on", "?ssl=true&sslmode=none"),
        ("false", ""),
        ("0", ""),
        ("no", ""),
        ("off", ""),
    ],
)
def test_clickhouse_catalog_secure_variants(secure, expected_query):
    env = {"CLICKHOUSE_DATABASE": "dbpcm_warehouse", "CLICKHOUSE_SECURE": secure}
    ch = render_catalogs(CH_INVENTORY, env=env)["clickhouse"]
    # JDBC URL form: path first, then query.
    expected = (
        f"connection-url=jdbc:clickhouse://ch.internal:8123/dbpcm_warehouse{expected_query}"
    )
    assert f"{expected}\n" in ch


def test_clickhouse_catalog_secure_toggle_without_database():
    env = {"CLICKHOUSE_SECURE": "true"}
    ch = render_catalogs(CH_INVENTORY, env=env)["clickhouse"]
    assert "connection-url=jdbc:clickhouse://ch.internal:8123?ssl=true&sslmode=none\n" in ch


def test_clickhouse_catalog_database_only_when_non_empty():
    ch = render_catalogs(CH_INVENTORY, env={"CLICKHOUSE_DATABASE": "dbpcm_warehouse"})[
        "clickhouse"
    ]
    assert "connection-url=jdbc:clickhouse://ch.internal:8123/dbpcm_warehouse\n" in ch
    for blank in ("", "   "):
        ch = render_catalogs(CH_INVENTORY, env={"CLICKHOUSE_DATABASE": blank})["clickhouse"]
        assert "connection-url=jdbc:clickhouse://ch.internal:8123\n" in ch


def test_clickhouse_catalog_inventory_fallback():
    # No env at all, an empty env, and an env file lacking the CLICKHOUSE_*
    # keys all render straight from inventory with the inventory header note.
    for env in (None, {}, {"UNRELATED_KEY": "x"}):
        ch = render_catalogs(CH_INVENTORY, env=env)["clickhouse"]
        assert "connection-url=jdbc:clickhouse://ch.internal:8123\n" in ch
        assert "derived from the inventory clickhouse block" in ch
        assert "derived from .env" not in ch


@pytest.mark.parametrize(
    "env,label",
    [
        ({"CLICKHOUSE_HOST": "bad host.internal"}, "CLICKHOUSE_HOST"),
        ({"CLICKHOUSE_HOST": "ch.internal/evil"}, "CLICKHOUSE_HOST"),
        ({"CLICKHOUSE_PORT": "8123?x=y"}, "CLICKHOUSE_PORT"),
        ({"CLICKHOUSE_PORT": "abc"}, "CLICKHOUSE_PORT"),
        ({"CLICKHOUSE_PORT": "0"}, "CLICKHOUSE_PORT"),
        ({"CLICKHOUSE_PORT": "65536"}, "CLICKHOUSE_PORT"),
        ({"CLICKHOUSE_DATABASE": "bad-name"}, "CLICKHOUSE_DATABASE"),
        ({"CLICKHOUSE_DATABASE": "9lives"}, "CLICKHOUSE_DATABASE"),
    ],
)
def test_clickhouse_catalog_rejects_env_garbage(env, label):
    with pytest.raises(ValueError, match=f"Invalid {label} in .env"):
        render_catalogs(CH_INVENTORY, env=env)


def test_clickhouse_catalog_never_writes_secrets(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "CLICKHOUSE_HOST=ch.env.internal",
                "CLICKHOUSE_PASSWORD=t0p-secret-ch-pw",
                "TRINO_CLICKHOUSE_PASSWORD=an0ther-secret-pw",
                "API_KEY=yet-an0ther-secret",
            ]
        ),
        encoding="utf-8",
    )
    ch = render_catalogs(CH_INVENTORY, env=load_env_file(env_file))["clickhouse"]
    assert "jdbc:clickhouse://ch.env.internal:8123" in ch
    for secret in ("t0p-secret-ch-pw", "an0ther-secret-pw", "yet-an0ther-secret"):
        assert secret not in ch
    # Credential lines remain Trino environment references, never literals.
    assert "${ENV:TRINO_CLICKHOUSE_USER}" in ch
    assert "${ENV:TRINO_CLICKHOUSE_PASSWORD}" in ch


def test_load_env_file_parser(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# a comment line",
                "",
                "   ",
                "export CLICKHOUSE_HOST=\"quoted-host\"",
                "CLICKHOUSE_PORT='9090'",
                "CLICKHOUSE_HOST=last-wins",
                "CLICKHOUSE_SECURE= true ",
                "NO_EQUALS_LINE",
                "=orphan-value",
            ]
        ),
        encoding="utf-8",
    )
    env = load_env_file(env_file)
    assert env == {
        "CLICKHOUSE_HOST": "last-wins",  # duplicate keys: last occurrence wins
        "CLICKHOUSE_PORT": "9090",
        "CLICKHOUSE_SECURE": "true",
    }


def test_write_catalogs_passes_env_through(tmp_path):
    rendered = write_catalogs(
        CH_INVENTORY,
        tmp_path,
        env={"CLICKHOUSE_HOST": "ch.env.internal", "CLICKHOUSE_PORT": "9440"},
    )
    assert len(rendered) == 1
    assert "jdbc:clickhouse://ch.env.internal:9440" in rendered[0].read_text(
        encoding="utf-8"
    )


# Synthetic templates keep provisioning tests independent of private views.
VIEW_TEMPLATE = TINY_VIEW.replace(".mini AS", ".employee AS")

VIEW_INVENTORY = {
    "mysql_shards": [
        {
            "catalog": "mysql_shard_qa2_01",
            "host": "mysql-qa.internal",
            "port": 3306,
            "proc_center": "pc_a",
        },
        {
            "catalog": "mysql_shard_qa2_02",
            "host": "mysql-qa2.internal",
            "port": 3306,
            "proc_center": "pc_b",
        },
    ],
    "clickhouse": {"catalog": "clickhouse", "host": "ch.internal", "port": 8123},
}


def test_views_render_two_shards_differ_only_by_identity():
    rendered = render_views(VIEW_INVENTORY, {"employee": VIEW_TEMPLATE})
    a = rendered[("mysql_shard_qa2_01", "employee")]
    b = rendered[("mysql_shard_qa2_02", "employee")]
    assert a != b
    # Every difference between the two renders is the shard catalog or the
    # proc-center literal — the SQL body (and its RLS) is identical.
    assert a.replace("mysql_shard_qa2_01", "@").replace("pc_a", "#") == b.replace(
        "mysql_shard_qa2_02", "@"
    ).replace("pc_b", "#")
    for sql in (a, b):
        assert "split_part(CURRENT_USER, '|', 1)" not in sql
        assert "split_part(CURRENT_USER, '|', 2)" in sql
        assert "split_part(CURRENT_USER, '|', 3)" in sql
        assert "dbpcm_datawarehouse.mysql_shard_qa2_" in sql


def test_views_placeholders_fully_substituted():
    a = render_views(VIEW_INVENTORY, {"employee": VIEW_TEMPLATE})[
        ("mysql_shard_qa2_01", "employee")
    ]
    assert "{{" not in a and "}}" not in a
    assert "mysql_shard_07" not in a
    # Exact occurrence counts: substitution is global (comments included).
    assert a.count("mysql_shard_qa2_01") == VIEW_TEMPLATE.count("{{SHARD_CATALOG}}")
    assert a.count("pc_a") == VIEW_TEMPLATE.count("{{PROC_CENTER}}")
    # The proc-center literal lands exactly in the output column.
    assert a.count("'pc_a'") == 1
    assert "'pc_a' AS proc_center" in a


def test_views_proc_center_entry_beats_flag_flag_fills_missing():
    rendered = render_views(
        VIEW_INVENTORY, {"employee": VIEW_TEMPLATE}, proc_center="pc_flag"
    )
    assert "'pc_a' AS proc_center" in rendered[("mysql_shard_qa2_01", "employee")]
    assert "'pc_b' AS proc_center" in rendered[("mysql_shard_qa2_02", "employee")]
    no_keys = {
        "mysql_shards": [
            {"catalog": entry["catalog"], "host": entry["host"], "port": 3306}
            for entry in VIEW_INVENTORY["mysql_shards"]
        ]
    }
    rendered = render_views(no_keys, {"employee": VIEW_TEMPLATE}, proc_center="pc_flag")
    for shard in ("mysql_shard_qa2_01", "mysql_shard_qa2_02"):
        sql = rendered[(shard, "employee")]
        assert sql.count("'pc_flag'") == 1 and "'pc_flag' AS proc_center" in sql


def test_views_missing_proc_center_fails_loud_naming_shard():
    inventory = {
        "mysql_shards": [
            {"catalog": "mysql_shard_qa2_01", "host": "mysql-qa.internal", "port": 3306}
        ]
    }
    with pytest.raises(ValueError) as exc:
        render_views(inventory, {"employee": VIEW_TEMPLATE})
    assert "mysql_shard_qa2_01" in str(exc.value)
    assert "proc_center" in str(exc.value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("view_catalog", "Reporting"),  # must be [a-z][a-z0-9_]*
        ("view_catalog", "dbpcm_datawarehouse;drop"),
        ("proc_center", "pc-1"),
        ("proc_center", "1pc"),
        ("proc_center", "pc one"),
    ],
)
def test_views_invalid_identifiers_rejected(key, value):
    inventory = VIEW_INVENTORY
    if key == "proc_center":
        # Per-entry keys would win over the flag; strip them so the render-time
        # default is the value under test.
        inventory = {
            "mysql_shards": [
                {"catalog": e["catalog"], "host": e["host"], "port": e["port"]}
                for e in VIEW_INVENTORY["mysql_shards"]
            ]
        }
    with pytest.raises(ValueError, match="Invalid"):
        render_views(inventory, {"employee": VIEW_TEMPLATE}, **{key: value})


def test_views_invalid_shard_catalog_rejected():
    inventory = {
        "mysql_shards": [
            {"catalog": "MySQL_shard", "host": "h", "port": 3306, "proc_center": "pc1"}
        ]
    }
    with pytest.raises(ValueError, match="shard catalog"):
        render_views(inventory, {"employee": VIEW_TEMPLATE})


@pytest.mark.parametrize(
    "stale",
    ["mysql_shard_07", "'pc1'", "split_part(CURRENT_USER, '|', 1)"],
    ids=["old-catalog", "old-proc-literal", "dropped-guard"],
)
def test_views_stale_template_rejected(stale):
    with pytest.raises(ValueError, match="template"):
        render_views(
            VIEW_INVENTORY, {"employee": VIEW_TEMPLATE + "\n" + stale + "\n"}
        )


def test_views_missing_rls_pin_rejected():
    mini = (
        "CREATE VIEW dbpcm_datawarehouse.{{SHARD_CATALOG}}.e AS "
        "SELECT '{{PROC_CENTER}}' AS proc_center;"
    )
    with pytest.raises(ValueError, match="RLS pin"):
        render_views(VIEW_INVENTORY, {"employee": mini})


def test_views_view_catalog_must_match_template_ddl():
    # Advisory guard: a --view-catalog the template DDL does not target would
    # emit a <catalog>.properties the rendered views never land in — fail loud
    # at render time naming the mismatch instead of drifting silently.
    with pytest.raises(ValueError) as exc:
        render_views(
            VIEW_INVENTORY, {"employee": VIEW_TEMPLATE}, view_catalog="analytics"
        )
    message = str(exc.value)
    assert "analytics" in message and "employee" in message
    assert "CREATE SCHEMA IF NOT EXISTS analytics." in message
    # The committed template targets `dbpcm_datawarehouse`, so the default still renders.
    assert render_views(VIEW_INVENTORY, {"employee": VIEW_TEMPLATE})[
        ("mysql_shard_qa2_01", "employee")
    ]


def test_views_unresolved_placeholder_rejected():
    with pytest.raises(ValueError, match="unresolved placeholders"):
        render_views(
            VIEW_INVENTORY, {"employee": VIEW_TEMPLATE + "\n-- {{UNKNOWN}}\n"}
        )


def test_render_view_catalog_memory_connector():
    props = render_view_catalog("dbpcm_datawarehouse")
    assert "connector.name=memory" in props
    assert "DEV-ONLY" in props and "RAM" in props
    with pytest.raises(ValueError):
        render_view_catalog("Reporting-Up")


def test_write_views_and_view_catalog(tmp_path):
    inventory = {
        "mysql_shards": [
            {"catalog": "mysql_shard_qa2_01", "host": "h", "port": 3306}
        ]
    }
    rendered_root = tmp_path / "views" / "rendered"
    paths = write_views(
        inventory,
        {"employee": VIEW_TEMPLATE},
        rendered_root,
        proc_center="pc1",
    )
    assert paths == [rendered_root / "mysql_shard_qa2_01" / "employee.sql"]
    sql = paths[0].read_text(encoding="utf-8")
    assert "'pc1' AS proc_center" in sql and sql.count("'pc1'") == 1
    assert "split_part(CURRENT_USER, '|', 1)" not in sql
    assert sql.count("split_part(CURRENT_USER, '|', 2)") == 1
    with pytest.raises(ValueError, match="already exist"):
        write_views(
            inventory, {"employee": VIEW_TEMPLATE}, rendered_root, proc_center="pc1"
        )
    write_views(
        inventory,
        {"employee": VIEW_TEMPLATE},
        rendered_root,
        proc_center="pc1",
        overwrite=True,
    )
    props = write_view_catalog("dbpcm_datawarehouse", tmp_path / "catalog")
    assert props.name == "dbpcm_datawarehouse.properties"
    assert "connector.name=memory" in props.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        write_view_catalog("dbpcm_datawarehouse", tmp_path / "catalog")


def test_views_empty_or_absent_views_dir_renders_nothing(tmp_path):
    assert load_view_templates(tmp_path / "missing") == {}
    empty = tmp_path / "empty"
    empty.mkdir()
    assert load_view_templates(empty) == {}
    assert (
        write_views(VIEW_INVENTORY, {}, tmp_path / "rendered", proc_center="pc1")
        == []
    )


def _run_setup(tmp_path, inventory, *extra, out_name="catalog"):
    snapshot = tmp_path / "shards.json"
    snapshot.write_text(json.dumps(inventory), encoding="utf-8")
    setup_trino_catalogs.main(
        [
            "--inventory",
            str(snapshot),
            "--output-dir",
            str(tmp_path / out_name),
            "--env-file",
            str(tmp_path / "absent.env"),
            *extra,
        ]
    )
    return tmp_path / out_name


def test_main_renders_views_and_view_catalog(tmp_path):
    views_src = tmp_path / "views"
    views_src.mkdir()
    (views_src / "employee.txt").write_text(VIEW_TEMPLATE, encoding="utf-8")
    catalog_dir = _run_setup(
        tmp_path,
        {
            "mysql_shards": [
                {"catalog": "mysql_shard_qa2_01", "host": "mysql-qa.internal", "port": 3306}
            ],
            **CH_INVENTORY,
        },
        "--views-dir",
        str(views_src),
        "--proc-center",
        "pc1",
    )
    sql_path = views_src / "rendered" / "mysql_shard_qa2_01" / "employee.sql"
    sql = sql_path.read_text(encoding="utf-8")
    assert "'pc1' AS proc_center" in sql
    assert "split_part(CURRENT_USER, '|', 1)" not in sql
    assert "dbpcm_datawarehouse.mysql_shard_qa2_01.employee" in sql
    props = (catalog_dir / "dbpcm_datawarehouse.properties").read_text(encoding="utf-8")
    assert "connector.name=memory" in props
    # Source catalogs are unaffected by view rendering.
    assert (catalog_dir / "mysql_shard_qa2_01.properties").is_file()
    assert (catalog_dir / "clickhouse.properties").is_file()


def test_main_views_dir_absent_or_empty_skips_views(tmp_path):
    inventory = {
        "mysql_shards": [
            {"catalog": "mysql_shard_qa2_01", "host": "mysql-qa.internal", "port": 3306}
        ],
        **CH_INVENTORY,
    }
    # Absent views dir: rc 0, source catalogs only, NO view catalog — and
    # no --proc-center needed (nothing renders, so nothing resolves it).
    catalog_dir = _run_setup(
        tmp_path, inventory, "--views-dir", str(tmp_path / "nope"), out_name="catalog_a"
    )
    assert (catalog_dir / "mysql_shard_qa2_01.properties").is_file()
    assert (catalog_dir / "clickhouse.properties").is_file()
    assert not (catalog_dir / "dbpcm_datawarehouse.properties").exists()
    assert not (tmp_path / "nope" / "rendered").exists()
    # Empty views dir behaves identically.
    empty = tmp_path / "views_empty"
    empty.mkdir()
    catalog_dir = _run_setup(
        tmp_path, inventory, "--views-dir", str(empty), out_name="catalog_b"
    )
    assert not (catalog_dir / "dbpcm_datawarehouse.properties").exists()


def test_main_missing_proc_center_exits_1_naming_shard(tmp_path, capsys):
    views_src = tmp_path / "views"
    views_src.mkdir()
    (views_src / "employee.txt").write_text(VIEW_TEMPLATE, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _run_setup(
            tmp_path,
            {
                "mysql_shards": [
                    {
                        "catalog": "mysql_shard_qa2_01",
                        "host": "mysql-qa.internal",
                        "port": 3306,
                    }
                ]
            },
            "--views-dir",
            str(views_src),
        )
    assert exc.value.code == 1
    assert "mysql_shard_qa2_01" in capsys.readouterr().err


def test_get_table_schema_gates_backend_notes_per_active_backend(
    monkeypatch, trino_settings, principal, authored_discovery
):
    """Service-level wiring: getTableSchema carries expense's trino_notes only
    when the serving deployment's active backend IS Trino. The reverse leg
    (trino disabled) uses the overlay directly — no live ClickHouse needed."""
    # Trino deployment: the trino-scoped note surfaces.
    schema = service.get_table_schema("dbpcm_datawarehouse", "expense", trino_settings)
    assert "notes" in schema
    assert any("physical type" in n for n in schema["notes"])
    # A table without scoped notes (employee) gains no key in any mode.
    employee_schema = service.get_table_schema(
        "dbpcm_datawarehouse", "employee", trino_settings
    )
    assert "notes" not in employee_schema

    # ClickHouse deployment (unit-test-style Settings; no live CH I/O): the
    # expense catalog projection must contain NO trace of the trino note.
    ch_settings = make_settings(trino_settings.trino_shard_registry_path, trino_enabled=False)
    assert ch_settings.trino_enabled is False
    from app.semantic_catalog.overlay import build_table_schema_response

    response = build_table_schema_response(
        database="dbpcm_warehouse",
        table="expense",
        introspected_columns=[{"name": "expense_id", "type": "UInt64", "comment": ""}],
        catalog=load_semantic_catalog(),
        scope=None,
        trino_enabled=ch_settings.trino_enabled,
    )
    assert "notes" not in response
    assert "physical type" not in json.dumps(response)


# --- Live discovery + public-database alias (ported from fork 59ad12a; adapted
# to the client-code range registry model — proc_center is NOT a routing input) ---
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
    assert calls[1].args[1] == ["mysql_shard_prod_01", "employee"]
    assert '"dbpcm_datawarehouse".information_schema.columns' in calls[1].args[0]
    assert (
        '"dbpcm_datawarehouse"."mysql_shard_prod_01".employee AS metadata_source LIMIT 0'
        in calls[2].args[0]
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


def test_discovery_resolves_before_network(monkeypatch, tmp_path, principal):
    connect = MagicMock()
    monkeypatch.setattr(trino_client, "_connect", connect)
    settings = make_settings(
        str(_write_snapshot(tmp_path / "empty.json", {"mysql_shards": []}))
    )
    with pytest.raises(QueryValidationError, match="No MySQL shard"):
        service.list_databases(settings)
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
    # Track the shipped default without constructing a bare Settings() — full
    # construction is environment-coupled (employee-access validation).
    trino_settings.trino_mysql_view_catalog = Settings.model_fields[
        "trino_mysql_view_catalog"
    ].default
    assert tables(
        trino_client.rewrite_query(
            "SELECT dbpcm_datawarehouse.employee.employee_code FROM dbpcm_datawarehouse.employee",
            trino_settings,
        )
    ) == {("dbpcm_datawarehouse", "mysql_shard_prod_01", "employee")}


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
    # Routing diversity comes from client_code alone (proc_center is NOT a
    # routing input): 10R37 -> prod_01, 12ABC -> prod_02 per the registry ranges.
    for pc, code, shard in [
        ("pc1", "10R37", "mysql_shard_prod_01"),
        ("pc2", "12ABC", "mysql_shard_prod_02"),
    ]:
        token = current_principal.set(
            Principal(
                "subject",
                {"proc_center": pc, "clientcode": code, "user_name": "alice"},
            )
        )
        try:
            discover(trino_settings, "dbpcm_datawarehouse", "employee")
            assert connect.call_args.args[1] == f"{pc}|{code}|alice"
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
    ) == {("dbpcm_datawarehouse", "mysql_shard_prod_01", "employee")}
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
            "SELECT * FROM dbpcm_datawarehouse.mysql_shard_prod_02.employee",
            trino_settings,
        )


# --- Lazy shard resolution (post-cherry-pick review, MAJOR) -------------------
# discover() must mirror rewrite_query's laziness: the MySQL shard is resolved
# only when the candidate set actually contains a mysql-source entry. A tenant
# with NO registry route must still reach clickhouse-source relations, while a
# mysql-source candidate still fails CLOSED before any network I/O.


def test_discovery_resolves_shard_lazily_for_clickhouse_only_schema(
    monkeypatch, tmp_path, principal
):
    """Empty mysql_shards + clickhouse-source table: discovery succeeds, no lockout."""
    settings = make_settings(
        str(_write_snapshot(tmp_path / "empty.json", {"mysql_shards": []}))
    )
    connect, _, cursor = discovery_connection(
        monkeypatch,
        [
            ["payroll", "employee_code", "varchar"],
            ["payroll", "amount", "decimal(18,2)"],
        ],
    )
    schema = service.get_table_schema("dbpcm_warehouse", "payroll", settings)
    assert schema["catalogued"] is True
    assert [c["name"] for c in schema["columns"]] == ["employee_code", "amount"]
    # The empty registry was never consulted: only the clickhouse catalog was probed.
    connect.assert_called_once_with(settings, "pc1|10R37|alice")
    info_calls = [
        c
        for c in cursor.execute.call_args_list
        if "information_schema" in c.args[0]
    ]
    assert [c.args[1] for c in info_calls] == [["dbpcm_warehouse", "payroll"]]

    # A mysql-source candidate still fails CLOSED, before any network I/O.
    connect.reset_mock()
    with pytest.raises(QueryValidationError) as exc:
        service.get_table_schema("dbpcm_datawarehouse", "employee", settings)
    assert exc.value.code == "MYSQL_SHARD_NOT_FOUND"
    connect.assert_not_called()


# --- Per-request discovery memoization (post-cherry-pick review, HIGH) --------
# The transports bind trino_metadata.discovery_memo next to the principal for
# the life of one request, so one MCP operation pays ONE live discovery sweep
# (previously up to three) and later shapes project from it in memory. Failures
# are never cached; the memo dies with the request context; another identity in
# the same context never inherits a sweep.


@pytest.fixture
def request_memo():
    """Bind the per-request discovery memo exactly like the MCP middleware does."""
    from app import trino_metadata

    token = trino_metadata.discovery_memo.set({})
    yield
    trino_metadata.discovery_memo.reset(token)


def _full_sweep_rows(settings):
    """information_schema rows covering one column of every allowed catalog table."""
    from app import trino_metadata

    return [
        [entry["table"], next(iter(entry["columns"])), "varchar"]
        for entry in trino_metadata.logical_catalog(settings).values()
    ]


def test_memo_scoped_get_table_schema_makes_one_connect(
    monkeypatch, trino_settings, principal, request_memo
):
    """Scoped getTableSchema: 1 Trino connect (narrow read + provenance catalog)."""
    from app.principal import current_scope

    monkeypatch.setattr("app.catalog.get_settings", lambda: trino_settings)
    connect, _, _ = discovery_connection(
        monkeypatch, _full_sweep_rows(trino_settings)
    )
    token = current_scope.set(
        frozenset({"dbpcm_warehouse.employee.employee_code"})
    )
    try:
        schema = service.get_table_schema(
            "dbpcm_datawarehouse", "employee", trino_settings
        )
    finally:
        current_scope.reset(token)
    assert connect.call_count == 1
    assert schema["database"] == "dbpcm_datawarehouse"
    assert [c["name"] for c in schema["columns"]] == ["employee_code"]


def test_memo_sample_rows_makes_one_discovery_and_one_execution(
    monkeypatch, trino_settings, principal, request_memo
):
    """sampleRows on the RLS-hidden employee view: 1 discovery + 1 execution."""
    monkeypatch.setattr("app.catalog.get_settings", lambda: trino_settings)
    connect, _, _ = discovery_connection(
        monkeypatch, _full_sweep_rows(trino_settings)
    )
    execute = MagicMock(return_value=(["employee_code"], [["E1"]]))
    monkeypatch.setattr(trino_client, "execute_query", execute)
    result = service.sample_rows(
        "dbpcm_datawarehouse",
        "employee",
        settings=trino_settings,
        include_provenance=True,
    )
    assert connect.call_count == 1
    execute.assert_called_once()
    assert result["provenance"]["columns"] == [
        ["dbpcm_warehouse.employee", "employee_code"]
    ]


def test_memo_run_query_makes_one_discovery_and_one_execution(
    monkeypatch, trino_settings, principal, request_memo
):
    """runQuery with provenance: still 1 discovery + 1 execution connection."""
    monkeypatch.setattr("app.catalog.get_settings", lambda: trino_settings)
    connect, _, _ = discovery_connection(
        monkeypatch, _full_sweep_rows(trino_settings)
    )
    execute = MagicMock(return_value=(["n"], [[1]]))
    monkeypatch.setattr(trino_client, "execute_query", execute)
    service.run_query(
        "SELECT employee_code FROM employee",
        settings=trino_settings,
        include_provenance=True,
    )
    assert connect.call_count == 1
    execute.assert_called_once()


def test_memo_skips_failures_and_dies_with_context(
    monkeypatch, trino_settings, principal
):
    from app import trino_metadata

    rows = _full_sweep_rows(trino_settings)
    token = trino_metadata.discovery_memo.set({})
    try:
        connect, _, cursor = discovery_connection(monkeypatch, rows)
        cursor.execute.side_effect = RuntimeError("boom")
        with pytest.raises(ClickHouseUnavailableError):
            trino_metadata.discover(trino_settings)
        assert connect.call_count == 1
        # Failures are NOT cached: an in-request retry probes Trino afresh ...
        connect, _, _ = discovery_connection(monkeypatch, rows)
        trino_metadata.discover(trino_settings)
        assert connect.call_count == 1
        # ... and only THEN do later shapes project from the memo in memory.
        trino_metadata.discover(trino_settings, "dbpcm_datawarehouse", "employee")
        trino_metadata.catalog_schema(trino_settings)
        assert connect.call_count == 1
    finally:
        trino_metadata.discovery_memo.reset(token)
    # The memo dies with its context: unbound callers stay one-shot-per-call.
    connect, _, _ = discovery_connection(monkeypatch, rows)
    trino_metadata.discover(trino_settings, "dbpcm_datawarehouse", "employee")
    trino_metadata.discover(trino_settings, "dbpcm_datawarehouse", "employee")
    assert connect.call_count == 2


def test_memo_preserves_identity_isolation(monkeypatch, trino_settings):
    from app import trino_metadata

    trino_settings.trino_mysql_view_catalog = "dbpcm_datawarehouse"
    rows = _full_sweep_rows(trino_settings)
    token = trino_metadata.discovery_memo.set({})
    try:
        connect, _, cursor = discovery_connection(monkeypatch, rows)
        # Routing diversity comes from client_code: 10R37 -> prod_01, 12ABC -> prod_02.
        for code, shard in [
            ("10R37", "mysql_shard_prod_01"),
            ("12ABC", "mysql_shard_prod_02"),
        ]:
            principal_token = current_principal.set(
                Principal(
                    "s",
                    {"proc_center": "pc1", "clientcode": code, "user_name": "alice", "jti": "alice-jti"},
                )
            )
            try:
                trino_metadata.discover(
                    trino_settings, "dbpcm_datawarehouse", "employee"
                )
            finally:
                current_principal.reset(principal_token)
        # Two identities = two sweeps: a memo NEVER crosses identities.
        assert connect.call_count == 2
        assert [c.args[1] for c in connect.call_args_list] == [
            "pc1|10R37|alice",
            "pc1|12ABC|alice",
        ]
        mysql_info_calls = [
            c
            for c in cursor.execute.call_args_list
            if "information_schema" in c.args[0] and c.args[1][0] != "dbpcm_warehouse"
        ]
        assert [c.args[1][0] for c in mysql_info_calls] == [
            "mysql_shard_prod_01",
            "mysql_shard_prod_02",
        ]
        # A repeat under the FIRST identity is memo-served (same request, same identity).
        principal_token = current_principal.set(
            Principal(
                "s", {"proc_center": "pc1", "clientcode": "10R37", "user_name": "alice", "jti": "alice-jti"}
            )
        )
        try:
            trino_metadata.discover(trino_settings, "dbpcm_datawarehouse", "employee")
        finally:
            current_principal.reset(principal_token)
        assert connect.call_count == 2
    finally:
        trino_metadata.discovery_memo.reset(token)


# --- Boot-time validation of Trino routing identifiers (review follow-up) -----
# trino_mysql_schema / trino_clickhouse_catalog / trino_mysql_view_catalog are
# interpolated into physical SQL names; a value that fails validate_identifier
# now fails Settings() construction instead of the first query.


def test_trino_mysql_view_catalog_rejected_at_boot():
    # The reviewer's shape: construction itself fails, not the first request —
    # and it fails even with TRINO_ENABLED unset (a misconfig is a misconfig).
    with pytest.raises(ValidationError, match="trino_mysql_view_catalog"):
        Settings(
            _env_file=None,
            employee_access_enabled=False,
            trino_mysql_view_catalog="Bad;Name",
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("trino_mysql_view_catalog", "1lead_digit"),
        ("trino_mysql_view_catalog", "Upper_Case"),
        ("trino_clickhouse_catalog", "bad-name"),
        ("trino_mysql_schema", "bad name"),
        ("trino_mysql_schema", 'db"online'),
    ],
)
def test_trino_routing_identifier_rejected_at_boot(field, value):
    # _env_file=None skips .env; the required ClickHouse identity comes from the
    # pytest env (conftest), so construction exercises ONLY the value under test.
    with pytest.raises(ValidationError, match=field):
        Settings(_env_file=None, employee_access_enabled=False, **{field: value})


def test_trino_routing_identifiers_accept_valid_values():
    # Empty view catalog stays legal (direct source-catalog routing).
    assert (
        Settings(
            _env_file=None, employee_access_enabled=False, trino_mysql_view_catalog=""
        ).trino_mysql_view_catalog
        == ""
    )
    settings = Settings(
        _env_file=None,
        employee_access_enabled=False,
        trino_mysql_view_catalog="custom_view_catalog",
        trino_clickhouse_catalog="ch_federated",
        trino_mysql_schema="dbreplica_2",
    )
    assert settings.trino_mysql_view_catalog == "custom_view_catalog"
    assert settings.trino_clickhouse_catalog == "ch_federated"
    assert settings.trino_mysql_schema == "dbreplica_2"
