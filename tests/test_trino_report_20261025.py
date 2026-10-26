"""Pinned report failures: day offsets and nullable IF-derived aggregates."""
import pytest
import sqlglot
from app.errors import QueryValidationError
from app.trino_client import _map_clickhouse_functions
from app.trino_semantics import annotate_input_types

CATALOG = {'dbpcm_warehouse.employee': {'columns': {
    'date_of_birth': {'type': 'Nullable(DateTime64(6))'},
    'hire_date': {'type': 'Nullable(DateTime64(6))'},
    'department_name': {'type': 'Nullable(String)'},
    'employee_status': {'type': 'String'},
}}}


def convert(sql):
    tree = sqlglot.parse_one(sql, read='clickhouse')
    annotate_input_types(tree, CATALOG)
    _map_clickhouse_functions(tree)
    return tree.sql(dialect='trino')


def age(column='date_of_birth'):
    return f"toYear(today())-toYear({column})-if(addYears({column},toYear(today())-toYear({column}))>today(),1,0)"


@pytest.mark.parametrize('offset', ["today()-INTERVAL 30 DAY", "today()+INTERVAL -30 DAY", "today()-30", "today()+(-30)", "addDays(today(),-30)", "ADDDAYS(today(),-30)"])
def test_integer_day_offsets(offset):
    sql = convert('SELECT '+offset)
    assert 'DATE_ADD(' in sql and '30' in sql
    assert convert('SELECT count() FROM employee WHERE hire_date >= '+offset)


@pytest.mark.parametrize('sql', [
    'SELECT round(avg('+age()+'),2) FROM employee WHERE date_of_birth IS NOT NULL AND toYear(date_of_birth)!=1900',
    'SELECT department_name,round(avg('+age('hire_date')+'),2) FROM employee GROUP BY department_name',
    'SELECT avg(age),sum(age) FROM (SELECT '+age()+' AS age FROM employee)',
    'WITH ages AS (SELECT '+age()+' AS age FROM employee) SELECT avg(age),sum(age) FROM ages',
    "SELECT avg(if(date_of_birth IS NULL,NULL,toYear(date_of_birth))) FROM employee",
])
def test_if_aggregate_and_derived_types(sql):
    converted = convert(sql)
    assert 'AVG(' in converted
    assert 'NAN()' not in converted  # nullable source retains NULL empty result


@pytest.mark.parametrize('expression', ["addDays(today(),1.5)", "addDays('2024-01-01',1)", "addDays(today())", "addDays(today(),1,'UTC')", "today()-INTERVAL '1.5' DAY", "today()-INTERVAL 1 MONTH", "avg(if(1,1,'text'))", "avg(if(1,unknown_column,0))"])
def test_unproven_types_and_signatures_still_fail_closed(expression):
    with pytest.raises(QueryValidationError):
        convert('SELECT '+expression+' FROM employee')


def test_if_numeric_condition_and_empty_null_semantics():
    import sqlite3
    for expression, expected in [("sum(if(1,3,0))",3), ("sum(if(NULL,3,0))",0),
                                 ("sum(if(1,NULL,3))",None)]:
        sql = sqlglot.transpile(convert('SELECT '+expression), read='trino', write='sqlite')[0]
        with sqlite3.connect(':memory:') as connection:
            assert connection.execute(sql).fetchone()[0] == expected
