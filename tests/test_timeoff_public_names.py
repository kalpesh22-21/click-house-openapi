"""Renamed public relations retain physical storage and scope boundaries."""
import pytest
import sqlglot
from sqlglot import exp
from app.table_names import native_query
from app.semantic_catalog.loader import load_semantic_catalog


@pytest.mark.parametrize('public,physical', [('timeoff_requests','accrual_events'),('timeoff_balances','accrual_balances')])
def test_public_catalog_only_exposes_new_name(public, physical):
    catalog = load_semantic_catalog()
    assert 'dbpcm_warehouse.'+public in catalog
    assert 'dbpcm_warehouse.'+physical not in catalog
    sql = native_query(f'SELECT {public}.employee_code FROM dbpcm_warehouse.{public}')
    tree = sqlglot.parse_one(sql, read='clickhouse')
    table = next(tree.find_all(exp.Table))
    assert table.name == physical
    assert table.alias == public
    assert next(tree.find_all(exp.Column)).table == public


def test_native_mapping_preserves_cte_scratch_and_literals():
    sql = "WITH timeoff_requests AS (SELECT 'timeoff_balances' AS label) SELECT label FROM timeoff_requests"
    assert native_query(sql) == sql
    sql = 'SELECT * FROM dbpcm_scratch.timeoff_balances'
    assert native_query(sql) == sql
    sql = native_query("SELECT a.employee_code, b.employee_code FROM dbpcm_warehouse.timeoff_requests a JOIN dbpcm_warehouse.timeoff_balances b ON a.employee_code=b.employee_code")
    tables = list(sqlglot.parse_one(sql, read='clickhouse').find_all(exp.Table))
    assert {(t.name,t.alias) for t in tables} == {('accrual_events','a'),('accrual_balances','b')}


def test_native_mapping_drops_old_database_qualifier_from_column_only():
    sql = native_query('SELECT dbpcm_warehouse.timeoff_requests.employee_code FROM dbpcm_warehouse.timeoff_requests')
    tree = sqlglot.parse_one(sql, read='clickhouse')
    assert next(tree.find_all(exp.Column)).db == ''
    assert next(tree.find_all(exp.Table)).db == 'dbpcm_warehouse'
