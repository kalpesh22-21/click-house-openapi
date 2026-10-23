"""Age-query regressions: nullable DOB composition, calendar parts and year boundaries."""
import pytest
import sqlglot
from app.errors import QueryValidationError
from app.trino_client import _map_clickhouse_functions
from app.trino_semantics import annotate_input_types

CATALOG = {'dbpcm_warehouse.employee': {'columns': {
    'employee_code': {'type': 'String'}, 'department_name': {'type': 'Nullable(String)'},
    'employee_status': {'type': 'String'}, 'date_of_birth': {'type': 'Nullable(DateTime64(6))'},
}}}
AGE = "toYear(today()) - toYear(date_of_birth) - CASE WHEN addYears(toDate32(date_of_birth), toYear(today()) - toYear(date_of_birth)) > today() THEN 1 ELSE 0 END"
SOURCE = "FROM dbpcm_warehouse.employee WHERE employee_status = 'Active' AND date_of_birth IS NOT NULL AND toDate32(date_of_birth) > toDate32('1900-01-01')"
SHAPES = [
    "SELECT multiIf(age<20,'<20',age<30,'20-29',age<40,'30-39',age<50,'40-49',age<60,'50-59','60+') AS band, count(DISTINCT employee_code), round(avg(age),0), round((quantileExactLow(0.5)(toFloat64(age))+quantileExactHigh(0.5)(toFloat64(age)))/2,0) FROM (SELECT employee_code, DATE_DIFF(YEAR,date_of_birth,now()) AS age " + SOURCE + ") GROUP BY band ORDER BY min(age)",
    "SELECT toMonth(toDate(date_of_birth)) AS m,toDayOfMonth(toDate(date_of_birth)) AS d,count() " + SOURCE + " GROUP BY m,d",
    "SELECT avg("+AGE+") "+SOURCE,
    "SELECT department_name,avg("+AGE.replace('THEN 1','THEN 0')+") "+SOURCE+" GROUP BY department_name",
    "SELECT avg(age) FROM (SELECT "+AGE+" AS age "+SOURCE+")",
    "SELECT department_name, round(avg(DATE_DIFF(YEAR,date_of_birth,now())),0) "+SOURCE+" GROUP BY department_name",
    "WITH emp AS (SELECT department_name,CAST(DATE_DIFF(YEAR,CAST(date_of_birth AS DateTime64(0)),now()) AS Int64) AS age "+SOURCE+") SELECT department_name,round(CAST(avg(age) AS Float64),0) FROM emp GROUP BY department_name",
]

def convert(sql):
    tree=sqlglot.parse_one(sql,read='clickhouse')
    annotate_input_types(tree,CATALOG)
    _map_clickhouse_functions(tree)
    return tree.sql(dialect='trino')

@pytest.mark.parametrize('sql',SHAPES,ids=['F1','F2','F3','F4','F5','F6','F7'])
def test_reported_age_shapes_convert(sql):
    result=convert(sql)
    assert result
    assert convert(sqlglot.parse_one(sql, read='clickhouse').sql(dialect='clickhouse'))
    # Retain the report's original timestamp-birthday expression as well as
    # the corrected calendar-date expression recommended by the knowledge doc.
    assert convert(sql.replace('addYears(toDate32(date_of_birth),', 'addYears(date_of_birth,'))
    assert 'DATE_DIFF(\'YEAR\'' not in result.upper()

@pytest.mark.parametrize('where',["", "WHERE date_of_birth IS NOT NULL OR employee_status='Active'"])
def test_nonnullable_cast_still_rejects_unproven_nulls(where):
    with pytest.raises(QueryValidationError):
        convert('SELECT avg(CAST(date_of_birth AS DateTime64(0))) FROM employee '+where)

def test_null_filter_does_not_change_nullable_average_empty_default():
    assert 'NAN()' not in convert('SELECT avg(toYear(date_of_birth)) FROM employee WHERE date_of_birth IS NOT NULL')

def test_calendar_parts_reject_timezone_overloads():
    with pytest.raises(QueryValidationError):
        convert("SELECT toMonth(date_of_birth,'America/Chicago') FROM employee")

@pytest.mark.parametrize('value,expected', [('2.5',2),('3.5',4),('-2.5',-2),('-3.5',-4),('2.4',2)])
def test_whole_year_float_rounding_preserves_ties_to_even(value,expected):
    import sqlite3
    sql=convert(f'SELECT round(toFloat64({value}),0)')
    sqlite=sqlglot.transpile(sql,read='trino',write='sqlite')[0]
    with sqlite3.connect(':memory:') as connection:
        assert connection.execute(sqlite).fetchone()[0] == expected

def test_f7_timestamp_scale_reduction_truncates_instead_of_rounding():
    result=convert(SHAPES[-1])
    assert "DATE_TRUNC('SECOND', date_of_birth)" in result
    assert "1970-01-01 00:00:00" in result

def test_multiif_age_branches_keep_nullable_type_through_aggregate():
    result=convert('SELECT avg(multiIf(employee_status=\'Active\',toYear(date_of_birth),NULL)) FROM employee')
    assert 'AVG(' in result and 'NAN()' not in result
