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
        assert 'pay_type' in sql
        assert '2080' in sql


def test_salary_and_hourly_zeros_do_not_mix_and_unknowns_stay_null():
    bp = load_blueprints()['bp-average-annual-salary-by-status']
    sql = bp['sql_template'].replace('{employee_status}', "'All'")
    tree = convert(sql)
    for table in tree.find_all(exp.Table):
        table.set('db', None)
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE employee(pay_type TEXT,annual_salary REAL,hourly_salary REAL,employee_status TEXT)')
        db.executemany('INSERT INTO employee VALUES (?,?,?,?)', [
            ('Salary',100000,None,'Active'),('Salary',200000,None,'On Leave'),
            ('Hourly',0,25,'Active'),('Hourly',0,50,'On Leave'),
            ('None',0,None,'Active')])
        result = {row[0]: row[2:] for row in db.execute(tree.sql(dialect='sqlite')).fetchall()}
    assert result == {'Salary': (150000,None), 'Hourly': (78000,37.5), 'None': (None,None)}


# A zero is not recorded compensation: an Hourly employee with hourly_salary 0 or a Salary
# employee with annual_salary 0 has no rate on record. Ranking, threshold and average
# blueprints exclude them (like NULL); only bp-salary-exact-median keeps an explicit
# `salary_population: recorded` opt-in, applied in its WHERE clause.
ZERO_RATE_ROWS = [
    ('s1', 'Salaried One', 'Sales', 'Salary', 100000, None, 'Active'),
    ('s2', 'Salaried Two', 'Sales', 'Salary', 200000, None, 'Active'),
    ('s0', 'Salary No Rate', 'Sales', 'Salary', 0, None, 'Active'),
    ('h1', 'Hourly One', 'Sales', 'Hourly', 0, 20, 'Active'),
    ('h2', 'Hourly Two', 'Sales', 'Hourly', 0, 30, 'Active'),
    ('h0', 'Hourly No Rate', 'Sales', 'Hourly', 0, 0, 'Active'),
]


def run_sqlite(blueprint_id, omit=(), **values):
    bp = load_blueprints()[blueprint_id]
    sql = bp['sql_template']
    for name in omit:  # an omitted optional slot: its optional_pattern replaces the predicate
        sql = re.sub(r'[\w.]+ = \{' + name + r'\}', 'TRUE', sql)
    sql = re.sub(r'\{(\w+)\}', lambda m: values.get(m[1], VALUES.get(m[1], '')), sql)
    tree = convert(sql)
    for table in tree.find_all(exp.Table):
        table.set('db', None)
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE employee(employee_code TEXT,employee_name TEXT,department_name TEXT,'
                   'pay_type TEXT,annual_salary REAL,hourly_salary REAL,employee_status TEXT)')
        db.executemany('INSERT INTO employee VALUES (?,?,?,?,?,?,?)', ZERO_RATE_ROWS)
        return db.execute(tree.sql(dialect='sqlite')).fetchall()


def test_a_lowest_paid_ranking_skips_employees_with_no_recorded_rate():
    rows = run_sqlite('bp-compensation-ranking', order_direction="'ASC'", top_n='1')
    assert [(r[0], r[2]) for r in rows] == [('Hourly', 'h1'), ('Salary', 's1')]


def test_a_below_threshold_count_does_not_count_a_zero_rate_as_paid_below_it():
    rows = run_sqlite('bp-salary-threshold-count', omit=('employee',), comparison="'below'", salary='150000')
    counts = {r[0]: r[-1] for r in rows}
    assert counts == {'Hourly': 2, 'Salary': 1}  # h0 and s0 have no rate on record


def test_averages_ignore_zero_rates_and_zero_salaries():
    rows = run_sqlite('bp-average-annual-salary-by-status', employee_status="'All'")
    result = {r[0]: r[2:] for r in rows}
    assert result == {'Salary': (150000, None), 'Hourly': (52000, 25)}


def test_no_compensation_branch_treats_a_zero_rate_as_pay():
    unguarded = re.compile(r"pay_type\s*=\s*'(?:Salary'\s+THEN\s+(?:\w+\.)?annual_salary|Hourly'\s+THEN\s+(?:\w+\.)?hourly_salary)\b")
    offenders = []
    for bp in load_blueprints().values():
        steps = bp.get('composes', [bp])
        if any(unguarded.search(step.get('sql_template', '')) for step in steps):
            offenders.append(bp['id'])
    # bp-salary-exact-median keeps the raw branches and filters zeros through salary_population.
    assert offenders == ['bp-salary-exact-median']
    median = load_blueprints()['bp-salary-exact-median']
    assert "{salary_population} = 'recorded' OR" in re.sub(r'\s+', ' ', median['sql_template'])
    population = next(s for s in median['slots'] if s['name'] == 'salary_population')
    assert population['description'].startswith('Use positive')


@pytest.mark.parametrize('bid', ['bp-median-annual-salary', 'bp-median-salary-by-position-level',
                                 'bp-comp-range-exceptions', 'bp-workforce-kpi-bundle', 'bp-department-scorecard'])
def test_annual_salary_figures_count_only_salaried_employees_with_a_salary(bid):
    bp = load_blueprints()[bid]
    sql = re.sub(r'\s+', ' ', bp['sql_template'])
    assert "pay_type = 'Salary' AND annual_salary > 0" in sql
    assert 'dbpcm_warehouse.employee.pay_type' in bp['uses']
