"""
Unit tests for the base-table reference extractor (app/sqlparse/provenance.py).

Layer: 1 — Unit (pure logic; inputs are SQL strings; no ClickHouse connection).

`extract_referenced_base_tables(sql)` returns the qualified `database.table` name
of every PHYSICAL BASE TABLE a query reads — the table-level analogue of the
column-provenance USES set, used by the RESTRICT_TO_CATALOGUED_TABLES guardrail to
enforce a catalog allowlist on every transport (including the scope-less path where
column-provenance never runs).

EXCLUDED (never a governed base table): CTE references and subquery aliases
(resolved PER-SCOPE so a decoy CTE name cannot mask a real base table in another
scope), table-valued functions / anonymous sources (`numbers(10)`), and the scratch
database (session-gated, not catalog-gated). Unqualified tables are attributed to
the warehouse default database. Parse failures and blocked statement kinds are
fail-closed (ProvenanceExtractionError), never treated as "reads no tables".
"""

from __future__ import annotations

import pytest

from app.sqlparse import (
    ProvenanceExtractionError,
    extract_referenced_base_tables,
)

_DB = "dbpcm_warehouse"


class TestBaseTableCollection:

    def test_unqualified_table_gets_default_db(self):
        assert extract_referenced_base_tables("SELECT * FROM employee") == frozenset(
            {f"{_DB}.employee"}
        )

    def test_explicit_db_preserved(self):
        assert extract_referenced_base_tables("SELECT * FROM analytics.events") == frozenset(
            {"analytics.events"}
        )

    def test_join_collects_both_sides(self):
        sql = "SELECT * FROM employee e JOIN department d ON e.dept = d.id"
        assert extract_referenced_base_tables(sql) == frozenset(
            {f"{_DB}.employee", f"{_DB}.department"}
        )

    def test_nested_subquery_table_collected(self):
        sql = "SELECT count() FROM (SELECT * FROM payroll) t"
        assert extract_referenced_base_tables(sql) == frozenset({f"{_DB}.payroll"})

    def test_subquery_in_where_collected(self):
        sql = "SELECT e.id FROM employee e WHERE e.id IN (SELECT id FROM ghost)"
        assert extract_referenced_base_tables(sql) == frozenset(
            {f"{_DB}.employee", f"{_DB}.ghost"}
        )


class TestExclusions:

    def test_cte_reference_is_not_a_base_table(self):
        sql = "WITH x AS (SELECT 1 AS a) SELECT a FROM x"
        assert extract_referenced_base_tables(sql) == frozenset()

    def test_cte_body_base_table_is_collected(self):
        sql = "WITH x AS (SELECT id FROM employee) SELECT * FROM x"
        assert extract_referenced_base_tables(sql) == frozenset({f"{_DB}.employee"})

    def test_cte_shadowing_a_real_name_is_not_a_base_table(self):
        # `employee` here is a CTE that shadows the physical table — ClickHouse reads
        # the CTE, so it must NOT be reported as a base-table reference.
        sql = "WITH employee AS (SELECT 1 AS a) SELECT a FROM employee"
        assert extract_referenced_base_tables(sql) == frozenset()

    def test_table_function_is_not_a_base_table(self):
        assert extract_referenced_base_tables("SELECT * FROM numbers(10)") == frozenset()

    def test_scratch_database_is_excluded(self):
        sql = "SELECT * FROM scratch.s_abc_bp_1"
        assert extract_referenced_base_tables(sql) == frozenset()

    def test_scratch_join_with_warehouse_keeps_only_warehouse(self):
        sql = "SELECT * FROM employee e JOIN scratch.s_abc_bp_1 s ON e.id = s.id"
        assert extract_referenced_base_tables(sql) == frozenset({f"{_DB}.employee"})


class TestPerScopeCteResolution:

    def test_inner_cte_does_not_exempt_outer_real_table(self):
        # `ghost` is a CTE only inside the inner subquery's scope; the OUTER FROM
        # `ghost` is the physical table and must be collected. This is the decoy-
        # alias bypass the per-scope resolution guards against.
        sql = (
            "SELECT * FROM ghost "
            "WHERE id IN (WITH ghost AS (SELECT 1 AS id) SELECT id FROM ghost)"
        )
        assert f"{_DB}.ghost" in extract_referenced_base_tables(sql)


class TestFailClosed:

    def test_empty_sql_fails_closed(self):
        with pytest.raises(ProvenanceExtractionError):
            extract_referenced_base_tables("   ")

    def test_unparseable_sql_fails_closed(self):
        with pytest.raises(ProvenanceExtractionError):
            extract_referenced_base_tables("SELECT * FROM WHERE )(")

    def test_multi_statement_fails_closed(self):
        with pytest.raises(ProvenanceExtractionError):
            extract_referenced_base_tables("SELECT * FROM employee; DROP TABLE employee")

    def test_ddl_fails_closed(self):
        with pytest.raises(ProvenanceExtractionError):
            extract_referenced_base_tables("CREATE TABLE t (id Int32)")
