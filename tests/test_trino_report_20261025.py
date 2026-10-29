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


@pytest.mark.parametrize('expression', ["toStartOfMonth(hire_date)", "toStartOfMonth(max(hire_date))"])
def test_month_truncation_survives_logical_sql_roundtrip(expression):
    original = 'SELECT '+expression+' FROM employee'
    rendered = sqlglot.parse_one(original, read='clickhouse').sql(dialect='clickhouse')
    assert convert(rendered) == convert(original)


@pytest.mark.parametrize('expression', ["dateTrunc('bogus',today())", "dateTrunc('month','2024-01-01')", "dateTrunc('month',today(),'UTC')", "dateTrunc(employee_code,today())"])
def test_date_trunc_invalid_signatures_fail_before_dispatch(expression):
    with pytest.raises(QueryValidationError):
        convert('SELECT '+expression+' FROM employee')


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


@pytest.mark.parametrize('node_type', [sqlglot.exp.DateTrunc, sqlglot.exp.TimestampTrunc])
@pytest.mark.parametrize('unit,value,zone', [
    ('bogus', sqlglot.exp.CurrentDate(), None),
    ('day', sqlglot.exp.Literal.string('not-a-date'), None),
    ('day', sqlglot.exp.CurrentDate(), sqlglot.exp.Literal.string('UTC')),
])
def test_typed_truncation_is_guarded_independently_of_parser_version(node_type, unit, value, zone):
    tree = sqlglot.exp.select(node_type(this=value.copy(), unit=sqlglot.exp.Literal.string(unit), zone=zone))
    annotate_input_types(tree, CATALOG)
    with pytest.raises(QueryValidationError, match='constant unit'):
        _map_clickhouse_functions(tree)


@pytest.mark.parametrize('function', ['toStartOfMonth', 'toStartOfDay', 'toStartOfHour', 'toMonday'])
def test_calendar_truncation_does_not_admit_untyped_strings(function):
    with pytest.raises(QueryValidationError):
        convert(f"SELECT {function}('not-a-date')")


@pytest.mark.parametrize('unit', ['day', 'year', 'employee_code'])
def test_original_truncation_unit_cannot_be_coerced_from_a_column(unit):
    from app.trino_client import normalize_logical_sql
    with pytest.raises(QueryValidationError, match='quoted constant'):
        normalize_logical_sql(f'SELECT dateTrunc({unit}, today())')
    assert normalize_logical_sql("SELECT 'dateTrunc(day,today())' AS text")
    assert normalize_logical_sql("SELECT dateTrunc(/*comment*/ 'day',today())")


@pytest.mark.parametrize('digits', range(1, 10))
def test_iso_fraction_validation_and_scale_truncation_are_version_independent(digits):
    from app.trino_client import _parse_iso_literal
    fraction = '123456789'[:digits]
    literal = '2024-02-29 12:34:56.' + fraction
    assert _parse_iso_literal(literal).microsecond == int((fraction+'000000')[:6])
    rendered = convert(f"SELECT toDateTime64('{literal}', 3)")
    assert '2024-02-29 12:34:56.' + fraction[:3] in rendered
    assert "'2024-02-29'" in convert(f"SELECT toDate('{literal}')")


@pytest.mark.parametrize('literal', ['2023-02-29 12:34:56.9', '2024-02-29 25:00:00.9999', '2024-02-29 12:34:56.1234567890', '2024-02-29T12:34:56.9Z'])
def test_iso_fraction_validation_still_rejects_invalid_dates_and_overloads(literal):
    for function in [f"toDate('{literal}')", f"toDateTime64('{literal}',3)"]:
        with pytest.raises(QueryValidationError):
            convert('SELECT '+function)
