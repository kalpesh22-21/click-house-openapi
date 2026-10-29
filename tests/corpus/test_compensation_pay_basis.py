"""Authored compensation queries preserve separate annual bases on the Trino route."""
import re
import sqlite3

import pytest
import sqlglot
from sqlglot import exp

from app.corpus.loader import load_blueprints
from app.semantic_catalog.loader import load_semantic_catalog
from app.trino_client import _map_clickhouse_functions
from app.trino_semantics import annotate_input_types

VALUES = {"order_direction": "'DESC'",'top_n': '10', 'employee_status': "'All'", 'department': "'Sales'", 'employee': "'s1'",
          'salary_population': "'recorded'", 'group_by': "'company'", 'band_width': '10000',
          'salary': '50000', 'comparison': "'above'", 'row_limit': '20',
          'company_salary_avg': '150000', 'company_hourly_avg': '78000'}
BLUEPRINTS = [bp for bp in load_blueprints().values()
              if 'dbpcm_warehouse.employee.hourly_salary' in bp.get('uses', [])]


def convert(sql):
    tree = sqlglot.parse_one(sql, read='clickhouse')
    annotate_input_types(tree, load_semantic_catalog())
    _map_clickhouse_functions(tree)
    return tree


@pytest.mark.parametrize('blueprint', BLUEPRINTS, ids=lambda bp: bp['id'])
def test_compensation_blueprints_have_typed_trino_conversion(blueprint):
    for step in blueprint.get('composes', [blueprint]):
        sql = re.sub(r'\{(\w+)\}', lambda m: VALUES[m[1]], step['sql_template'])
        tree = convert(sql)
        assert tree.sql(dialect='trino')
        # Simple CASE with coerced boolean WHEN clauses was rejected by live Trino.
        for case in tree.find_all(exp.Case):
            assert case.this is None
        assert 'employee_pay_type' in sql
        assert '2080' in sql


def test_salary_and_hourly_zeros_do_not_mix_and_unknowns_stay_null():
    bp = load_blueprints()['bp-average-annual-salary-by-status']
    sql = bp['sql_template'].replace('{employee_status}', "'All'")
    tree = convert(sql)
    for table in tree.find_all(exp.Table):
        table.set('db', None)
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE employee(employee_pay_type TEXT,annual_salary REAL,hourly_salary REAL,employee_status TEXT)')
        db.executemany('INSERT INTO employee VALUES (?,?,?,?)', [
            ('Salary',100000,None,'Active'),('Salary',200000,None,'On Leave'),
            ('Hourly',0,25,'Active'),('Hourly',0,50,'On Leave'),
            ('None',0,None,'Active')])
        result = {row[0]: row[2:] for row in db.execute(tree.sql(dialect='sqlite')).fetchall()}
    assert result == {'Salary': (150000,None), 'Hourly': (78000,37.5), 'None': (None,None)}
