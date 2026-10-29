"""Type/default regression cases; all schemas and data are synthetic."""
import sqlite3

import pytest
import sqlglot

from app.errors import QueryValidationError
from app.trino_client import _map_clickhouse_functions
from app.trino_semantics import annotate_input_types

CATALOG = {
    "dbpcm_warehouse.staff": {"columns": {
        "salary": {"type": "Nullable(Decimal(18,6))"},
        "amount": {"type": "Decimal(18,6)"},
        "name": {"type": "Nullable(String)"},
        "stamp": {"type": "Int64"},
    }},
}


def convert(sql, dialect="trino"):
    tree = sqlglot.parse_one(sql, read="clickhouse")
    annotate_input_types(tree, CATALOG)
    _map_clickhouse_functions(tree)
    return tree.sql(dialect=dialect)


@pytest.mark.parametrize("expression, expected", [
    ("sum(3)", 0),
    ("sum(toNullable(3))", None),
    ("sum(CAST(NULL AS Nullable(Int64)))", None),
    ("sumIf(3,0)", 0),
    ("sumIf(toNullable(3),0)", None),
    ("sumIf(toNullable(3),1)", None),
])
def test_empty_aggregate_results(expression, expected):
    with sqlite3.connect(":memory:") as connection:
        sql = convert(f"SELECT {expression} WHERE 0=1", dialect="sqlite")
        assert connection.execute(sql).fetchone()[0] == expected


@pytest.mark.parametrize("expression, expected", [
    ("sumIf(3,1)", 3), ("sumIf(toNullable(3),1)", 3),
    ("sumIf(toNullable(3),0)", None),
    ("sumIf(CAST(NULL AS Nullable(Int64)),1)", None),
])
def test_conditional_aggregate_results(expression, expected):
    with sqlite3.connect(":memory:") as connection:
        assert connection.execute(convert(f"SELECT {expression}", "sqlite")).fetchone()[0] == expected


@pytest.mark.parametrize("expression, default", [
    ("avg(3)", "NAN()"), ("avgIf(3,0)", "NAN()"),
    ("avg(toNullable(3))", None), ("avgIf(toNullable(3),0)", None),
])
def test_average_empty_defaults(expression, default):
    emitted = convert("SELECT " + expression)
    assert ("NAN()" in emitted) == bool(default)
    assert ("COALESCE(" in emitted) == bool(default)


def test_catalog_nullability_survives_alias_and_derived_scope():
    sql = convert("WITH s AS (SELECT salary AS x, amount AS y FROM staff) SELECT sum(x),sum(y) FROM s")
    assert "SELECT SUM(x), COALESCE(SUM(y), 0)" in sql


@pytest.mark.parametrize("cte", [False, True])
@pytest.mark.parametrize("empty, expected", [(False, (3, 2)), (True, (0, 0))])
def test_aggregate_over_derived_exact_distinct_count(cte, empty, expected):
    inner = "SELECT uniqExact(name) AS n FROM staff"
    if empty:
        inner += " WHERE 0=1"
    inner += " GROUP BY stamp"
    sql = (f"WITH counts AS ({inner}) SELECT sum(n), count() FROM counts" if cte
           else f"SELECT sum(n), count() FROM ({inner}) counts")
    # Duplicate and NULL names must not change the exact count; empty grouped
    # input must retain ClickHouse's zero SUM default across the scope boundary.
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE staff(name TEXT, stamp INTEGER)")
        connection.executemany("INSERT INTO staff VALUES (?, ?)",
                               [("A", 1), ("A", 1), (None, 1), ("B", 2), ("C", 2)])
        assert connection.execute(convert(sql, "sqlite")).fetchone() == expected
    assert "COUNT(DISTINCT name)" in convert(sql)


@pytest.mark.parametrize("predicate", ["name = TRUE", "FALSE != name", "name = false"])
def test_boolean_literal_is_not_a_string_slot_value(predicate):
    with pytest.raises(QueryValidationError, match="entire predicate"):
        convert(f"SELECT name FROM staff WHERE {predicate}")


def test_omitted_predicate_and_literal_string_true_remain_valid():
    assert "WHERE TRUE" in convert("SELECT name FROM staff WHERE TRUE")
    assert "'TRUE'" in convert("SELECT name FROM staff WHERE name = 'TRUE'")


def test_same_name_columns_are_bound_by_source():
    sql = convert("SELECT sum(a.salary),sum(b.amount) FROM staff a JOIN staff b ON a.stamp=b.stamp")
    assert "SUM(a.salary), COALESCE(SUM(b.amount), 0)" in sql


def test_having_alias_expansion_columns_are_typed():
    sql = convert("SELECT sum(amount) FROM staff HAVING sum(amount)>1")
    assert sql.count("COALESCE(SUM(amount), 0)") == 2


@pytest.mark.parametrize("sql", [
    "SELECT sum(unknown) FROM staff",
    "SELECT sum(b.amount) FROM staff a LEFT JOIN staff b ON a.stamp=b.stamp",
    "SELECT sum(CAST(salary AS Float64)) FROM staff",
    "SELECT sumIf(3)", "SELECT avgIf(3,1,2)",
    "SELECT argMax((1,2),1)",
    "SELECT length(unknown) FROM staff",
    "SELECT toDateTime(unknown) FROM staff",
    "SELECT toDateTime(CAST(3 AS Decimal(18,2)))",
    "SELECT toDateTime('2024-01-01 12:34:56.9')",
    "SELECT toDateTime(name) FROM staff",
    "SELECT JSONExtractString(name,'a') FROM staff",
    "SELECT JSONExtractString('{\"a\":1.00}','a')",
    "SELECT JSONExtractString('{\"a\":1,\"a\":2}','a')",
    "SELECT JSONExtractString('[\"a\"]',1)",
])
def test_unknown_or_unverified_semantics_fail_closed(sql):
    with pytest.raises(QueryValidationError) as error:
        convert(sql)
    assert error.value.code == "TRINO_UNSUPPORTED_SQL"
    assert sql not in str(error.value)


@pytest.mark.parametrize("literal, expected", [("'é'", 2), ("'😀'", 4), ("''", 0)])
def test_length_counts_utf8_bytes(literal, expected):
    with sqlite3.connect(":memory:") as connection:
        connection.create_function("TO_UTF8", 1, lambda s: s.encode("utf-8") if s is not None else None)
        assert connection.execute(convert("SELECT length("+literal+")", "sqlite")).fetchone()[0] == expected


def test_array_length_uses_cardinality():
    assert convert("SELECT length([1,2])") == "SELECT CARDINALITY(ARRAY[1, 2])"


@pytest.mark.parametrize("expression,expected", [
    ("JSONExtractString('{}','missing')", ""),
    ("JSONExtractString('{\"a\":null}','a')", ""),
    ("JSONExtractString('{\"a\":\"é😀\"}','a')", "é😀"),
    ("JSONExtractString('{\"a\":{\"b\":\"yes\"}}','a','b')", "yes"),
    ("JSONExtractString('broken','a')", ""),
    ("JSONExtractString(NULL,'a')", None),
])
def test_literal_json_defaults_and_values(expression, expected):
    with sqlite3.connect(":memory:") as connection:
        assert connection.execute(convert("SELECT " + expression, "sqlite")).fetchone()[0] == expected


def test_argmax_skips_null_value_and_key():
    sql = convert("SELECT argMax(name,stamp) FROM staff")
    assert "MAX_BY(name, stamp) FILTER(WHERE NOT name IS NULL AND NOT stamp IS NULL)" in sql
    assert "COALESCE" not in sql


def test_argmax_nonnullable_empty_default():
    assert "COALESCE(MAX_BY('a', 1)" in convert("SELECT argMax('a',1)")


def test_epoch_clamps_and_truncates():
    sql = convert("SELECT toDateTime(stamp) FROM staff")
    assert "FROM_UNIXTIME(TRUNCATE(CAST(GREATEST(0, LEAST(4294967295, stamp)) AS DOUBLE)))" in sql


def test_decimal_average_does_not_round_to_input_scale():
    # Source AVG is Float64; Trino AVG(DECIMAL) otherwise rounds to scale 6.
    assert "AVG(CAST(salary AS DOUBLE))" in convert("SELECT avg(salary) FROM staff")


@pytest.mark.parametrize("source,expected", [
    ("1900-01-01 00:00:00", "1970-01-01 00:00:00"),
    ("2299-01-01 00:00:00", "2106-02-07 06:28:15"),
])
def test_datetime_string_range_saturates(source, expected):
    assert expected in convert(f"SELECT toDateTime('{source}')")


def test_datetime_wrapping_is_not_mistaken_for_numeric_saturation():
    with pytest.raises(QueryValidationError):
        convert("SELECT toDateTime(toDate32('1900-01-01'))")


@pytest.mark.parametrize("value", [float('nan'), float('inf'), -float('inf'), [float('nan')], {'nested': float('inf')}])
def test_nonfinite_results_fail_explicitly_at_json_boundary(value):
    from app.service import _compact_result
    with pytest.raises(QueryValidationError) as error:
        _compact_result(['value'], [[value]], 10)
    assert error.value.code == 'NON_FINITE_RESULT'


def test_finite_and_null_results_are_unchanged():
    from app.service import _compact_result
    assert _compact_result(['value'], [[None], [1.25]], 10)['rows'] == [[None], [1.25]]


@pytest.mark.parametrize('expression, coalesced', [
    ('CASE WHEN stamp > 0 THEN amount ELSE 0 END', True),
    ('CASE WHEN stamp > 0 THEN salary ELSE 0 END', False),
    ('CASE WHEN stamp > 0 THEN amount END', False),
    ('coalesce(salary, 0)', True),
    ('coalesce(salary, amount)', True),
    ('abs(salary)', False),
    ('round(salary, 2)', False),
    ('round(amount, 2)', True),
])
def test_schema_aware_expression_types(expression, coalesced):
    result = convert(f'SELECT sum({expression}) FROM staff')
    assert result.startswith('SELECT COALESCE(SUM(') == coalesced


def test_qualified_copy_does_not_modify_execution_sql():
    tree = sqlglot.parse_one('SELECT s.*, sum(CASE WHEN stamp>0 THEN amount ELSE 0 END) AS total FROM staff s GROUP BY stamp', read='clickhouse')
    before = tree.sql(dialect='clickhouse')
    annotate_input_types(tree, CATALOG)
    assert tree.sql(dialect='clickhouse') == before


def test_schema_resolves_unique_unqualified_column_in_join():
    from app.trino_semantics import value_type
    catalog = {**CATALOG, 'dbpcm_warehouse.other': {'columns': {'id': {'type': 'Int64'}}}}
    tree = sqlglot.parse_one('SELECT sum(amount) FROM staff JOIN other ON stamp=id', read='clickhouse')
    annotate_input_types(tree, catalog)
    assert value_type(tree.expressions[0].this).nullable is False


def test_derived_case_type_survives_cte():
    result = convert('WITH s AS (SELECT CASE WHEN stamp>0 THEN amount ELSE 0 END AS x FROM staff) SELECT sum(x) FROM s')
    assert 'SELECT COALESCE(SUM(x), 0)' in result


@pytest.mark.parametrize('sql', [
    'SELECT sum(coalesce(unknown_function(salary),0)) FROM staff',
    'SELECT sum(CASE WHEN stamp>0 THEN unknown_function(amount) ELSE 0 END) FROM staff',
    'SELECT sum(round(toFloat64(amount),2)) FROM staff',
    'SELECT sum(CASE WHEN a.stamp>0 THEN b.amount ELSE 0 END) FROM staff a LEFT JOIN staff b ON a.stamp=b.stamp',
])
def test_library_inference_does_not_override_semantic_guards(sql):
    with pytest.raises(QueryValidationError):
        convert(sql)


def test_tenure_day_difference_infers_nullable_integer():
    sql = convert("SELECT avg(dateDiff('day',hire_date,today()) / 365.25) FROM (SELECT CAST(NULL AS Nullable(DateTime64(6))) AS hire_date)")
    assert "DATE_DIFF('DAY', CAST(hire_date AS DATE), CAST(CURRENT_DATE AS DATE))" in sql
    assert 'COALESCE(AVG' not in sql


def test_day_difference_timezone_overload_is_not_guessed():
    with pytest.raises(QueryValidationError):
        convert("SELECT dateDiff('day',toDate32('2024-01-01'),today(),'UTC')")


@pytest.mark.parametrize('expression, expected', [
    ('sum(stamp)', [(1, 0), (2, 1), (3, 2)]),
    ('sumIf(stamp, stamp > 1)', [(1, 0), (2, 0), (3, 2)]),
    ('minIf(stamp, stamp > 1)', [(1, 0), (2, 0), (3, 2)]),
    ('maxIf(stamp, stamp > 1)', [(1, 0), (2, 0), (3, 2)]),
    ('sum(salary)', [(1, None), (2, None), (3, 2)]),
])
def test_window_empty_frames_and_conditions(expression, expected):
    sql = convert(f'SELECT stamp, {expression} OVER (ORDER BY stamp ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING) FROM staff ORDER BY stamp', 'sqlite')
    with sqlite3.connect(':memory:') as db:
        db.executescript('CREATE TABLE staff(stamp INTEGER, salary NUMERIC); INSERT INTO staff VALUES (1,NULL),(2,2),(3,3);')
        assert db.execute(sql).fetchall() == expected


def test_partitioned_running_total_preserves_boundaries():
    sql = convert('SELECT stamp, sum(stamp) OVER (PARTITION BY name ORDER BY stamp ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM staff ORDER BY stamp', 'sqlite')
    with sqlite3.connect(':memory:') as db:
        db.executescript("CREATE TABLE staff(stamp INTEGER, name TEXT); INSERT INTO staff VALUES (1,'a'),(2,'b'),(3,'a');")
        assert db.execute(sql).fetchall() == [(1,1),(2,2),(3,4)]


def test_grouped_count_window_total():
    sql = convert('SELECT name, count(), sum(count()) OVER () FROM staff GROUP BY name ORDER BY name', 'sqlite')
    with sqlite3.connect(':memory:') as db:
        db.executescript("CREATE TABLE staff(name TEXT); INSERT INTO staff VALUES ('a'),('a'),('b');")
        assert db.execute(sql).fetchall() == [('a',2,3),('b',1,3)]


def test_named_window_default_wraps_entire_window():
    result = convert('SELECT avgIf(amount,stamp>0) OVER w FROM staff WINDOW w AS (PARTITION BY name ORDER BY stamp ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)')
    assert 'OVER w, NAN())' in result
    assert 'FILTER(' not in result
    assert 'WINDOW w AS (PARTITION BY name ORDER BY stamp' in result


@pytest.mark.parametrize('family', ['Low', 'High'])
@pytest.mark.parametrize('column,default', [('salary',False),('amount',True)])
def test_exact_medians_preserve_values_and_defaults(family,column,default):
    result = convert(f'SELECT quantileExact{family}If(0.5)({column},stamp>0) FROM staff')
    assert 'ELEMENT_AT(ARRAY_SORT(ARRAY_AGG(' in result
    assert 'COUNT(' in result and 'stamp > 0' in result
    assert ('COALESCE(' in result) == default
    assert 'APPROX' not in result and ' AS DOUBLE' not in result


@pytest.mark.parametrize('expression', [
    'quantileExactLow(0.25)(amount)',
    'quantileExactHigh(level)(amount)',
    'quantileExactHigh(0.5)(name)',
    'quantileExactLow(0.5)(unknown)',
    'quantileExactLowIf(0.5)(amount)',
    'quantileExactLow(0.5)(amount) OVER (PARTITION BY name)',
    'quantileExact(0.5)(amount)',
    'quantilesExactLow(0.25,0.5)(amount)',
])
def test_unverified_quantile_signatures_fail_before_execution(expression):
    with pytest.raises(QueryValidationError) as error:
        convert('SELECT '+expression+' FROM staff')
    assert error.value.code == 'TRINO_UNSUPPORTED_SQL'


@pytest.mark.parametrize('fn', ['minIf','maxIf'])
@pytest.mark.parametrize('value,expected', [('3',0),('toNullable(3)',None)])
def test_conditional_extrema_empty_default(fn,value,expected):
    with sqlite3.connect(':memory:') as db:
        assert db.execute(convert(f'SELECT {fn}({value},0)', 'sqlite')).fetchone()[0] == expected


@pytest.mark.parametrize('values,expected', [
    ([], (0,0)), ([7], (7,7)), ([9,1,4], (4,4)),
    ([9,1,4,2], (2,4)), ([2,2,2,9], (2,2)),
    ([9007199254740993,9007199254740995], (9007199254740993,9007199254740995)),
])
def test_exact_median_order_statistics_without_float_rounding(values,expected):
    # Execute emitted Trino scalar/aggregate composition with SQLite adapters
    # for its three array operations. Integer division and FILTER are native.
    import json
    class ArrayAgg:
        def __init__(self):
            self.items = []
        def step(self,value):
            self.items.append(value)
        def finalize(self):
            return json.dumps(self.items)
    with sqlite3.connect(':memory:') as db:
        db.create_aggregate('ARRAY_AGG',1,ArrayAgg)
        db.create_function('ARRAY_SORT',1,lambda a: json.dumps(sorted(json.loads(a))) if a else None)
        db.create_function('ELEMENT_AT',2,lambda a,i: json.loads(a)[i-1] if a and 0<i<=len(json.loads(a)) else None)
        db.create_function('GREATEST',2,max)
        db.execute('CREATE TABLE staff(stamp INTEGER)')
        db.executemany('INSERT INTO staff VALUES (?)', [(v,) for v in values])
        sql = convert('SELECT quantileExactLow(0.5)(stamp),quantileExactHigh(0.5)(stamp) FROM staff')
        assert db.execute(sql).fetchone() == expected


@pytest.mark.parametrize('value,divisor,expected', [(5,2,2),(-5,2,-2),(9007199254740993,2,4503599627370496),(9223372036854775807,1,9223372036854775807),(None,25000,None)])
def test_intdiv_exact_signed_integer_results(value,divisor,expected):
    sql = convert(f'SELECT intDiv(stamp,{divisor}) FROM staff')
    assert 'DOUBLE' not in sql and 'FLOOR' not in sql
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE staff(stamp INTEGER)')
        db.execute('INSERT INTO staff VALUES (?)',(value,))
        assert db.execute(sql).fetchone()[0] == expected


def test_intdiv_decimal_truncates_before_integer_cast_and_division():
    sql = convert('SELECT intDiv(salary,25000) FROM staff')
    assert 'CAST(TRUNCATE(salary) AS BIGINT) / CAST(25000 AS BIGINT)' in sql
    assert 'DOUBLE' not in sql and 'FLOOR' not in sql


@pytest.mark.parametrize('expression', [
    'intDiv(stamp,0)', 'intDiv(stamp,-2)', 'intDiv(stamp,2.5)',
    'intDiv(stamp,stamp)', 'intDiv(toFloat64(amount),2)',
    'intDiv(CAST(1 AS Decimal(30,6)),2)', 'intDiv(CAST(1 AS UInt64),2)',
    'intDiv(unknown,2)', 'intDiv(stamp)', 'intDiv(stamp,2,3)',
])
def test_intdiv_unverified_overloads_fail_before_execution(expression):
    with pytest.raises(QueryValidationError):
        convert('SELECT '+expression+' FROM staff')

@pytest.mark.parametrize('source', ['Date', 'DateTime', 'DateTime64(6)', 'Nullable(Date)'])
def test_to_year_preserves_date_contract(source):
    sql = convert(f"SELECT toYear(CAST('2024-12-31' AS {source}))")
    assert 'YEAR(' in sql and 'TOYEAR' not in sql


@pytest.mark.parametrize('expression', [
    "toYear('2024-01-01')", 'toYear(1700000000)', 'toYear(unknown)',
    "toYear(CAST('2024-01-01' AS DateTime('America/Chicago')))",
    "toYear(CAST('2024-01-01' AS Date), 'UTC')",
])
def test_to_year_rejects_unverified_contracts(expression):
    with pytest.raises(QueryValidationError, match='toYear'):
        convert('SELECT ' + expression)


def test_to_int64_decimal_truncates_and_keeps_nullability():
    sql = convert('SELECT toInt64(salary), sum(toInt64(salary)) FROM staff')
    assert 'CAST(TRUNCATE(salary) AS BIGINT)' in sql
    assert 'DOUBLE' not in sql and 'COALESCE' not in sql


@pytest.mark.parametrize('expression', [
    'toInt64(stamp)', 'toInt64(amount)', 'toInt64(salary)',
    'toInt64(floor(salary / 25000))', 'toInt64(-1.9)',
    'toInt64(CAST(NULL AS Nullable(Float64)))',
])
def test_to_int64_supported_numeric_inputs(expression):
    sql = convert('SELECT ' + expression + ' FROM staff')
    assert 'TOINT64' not in sql
    assert 'AS BIGINT' in sql


@pytest.mark.parametrize('expression', [
    "toInt64('12')", 'toInt64(CAST(1 AS UInt64))',
    'toInt64(CAST(1 AS Decimal(30,6)))', 'toInt64(unknown)',
    'toInt64()', 'toInt64(1,2)',
])
def test_to_int64_unverified_overloads_rejected(expression):
    with pytest.raises(QueryValidationError, match='toInt64'):
        convert('SELECT ' + expression + ' FROM staff')


def test_float_to_int64_has_explicit_range_and_finiteness_guard():
    sql = convert('SELECT toInt64(CAST(stamp AS Float64)) FROM staff')
    assert 'IS_FINITE(' in sql and 'NAN()' in sql
    assert '>= -9.223372036854776E18' in sql
    assert '< 9.223372036854776E18' in sql


def test_to_year_date32_catalog_column():
    tree = sqlglot.parse_one('SELECT toYear(d) FROM dates', read='clickhouse')
    annotate_input_types(tree, {'dbpcm_warehouse.dates': {'columns': {'d': {'type': 'Nullable(Date32)'}}}})
    _map_clickhouse_functions(tree)
    assert tree.sql(dialect='trino') == 'SELECT YEAR(d) FROM dates'


def test_to_int64_minimum_signed_literal():
    assert 'CAST(-9223372036854775808 AS BIGINT)' in convert('SELECT toInt64(-9223372036854775808)')


def test_outer_join_rejection_explains_structural_cause():
    with pytest.raises(QueryValidationError, match="after an outer join"):
        convert("SELECT sum(b.salary) FROM dbpcm_warehouse.staff a LEFT JOIN dbpcm_warehouse.staff b ON a.name=b.name")


@pytest.mark.parametrize("function", ["countDistinct", "COUNTDISTINCT", "CountDistinct"])
@pytest.mark.parametrize("values,expected", [(["A", "A", None, "B"], 2), ([None], 0), ([], 0)])
def test_count_distinct_alias_preserves_duplicates_nulls_and_empty(function, values, expected):
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE staff(name TEXT)")
        connection.executemany("INSERT INTO staff VALUES (?)", [(v,) for v in values])
        assert connection.execute(convert(f"SELECT {function}(name) FROM staff", "sqlite")).fetchone() == (expected,)


@pytest.mark.parametrize("function", ["addYears", "ADDYEARS", "AddYears"])
def test_add_years_current_year_boundary(function):
    assert convert(f"SELECT {function}(toStartOfYear(today()), 1)") == "SELECT DATE_ADD('YEAR', 1, DATE_TRUNC('YEAR', CURRENT_DATE))"


@pytest.mark.parametrize("function", ["date_diff", "dateDiff", "DATE_DIFF"])
def test_day_difference_accepts_now_and_keeps_calendar_boundaries(function):
    tree = sqlglot.parse_one(f"SELECT avg({function}('day', hire_date, now())) FROM employee", read="clickhouse")
    annotate_input_types(tree, {"dbpcm_warehouse.employee": {"columns": {"hire_date": {"type": "Nullable(DateTime64(6))"}}}})
    _map_clickhouse_functions(tree)
    emitted = tree.sql(dialect="trino")
    assert "DATE_DIFF('DAY', CAST(hire_date AS DATE), CAST(CURRENT_TIMESTAMP AS DATE))" in emitted
    assert "AVG(" in emitted


@pytest.mark.parametrize("expression", [
    "addYears(today())", "addYears(today(), 1.5)", "addYears('2024-01-01', 1)",
    "countDistinct()", "countDistinct(name, stamp)", "now('UTC')",
])
def test_new_function_overloads_fail_closed(expression):
    with pytest.raises(QueryValidationError):
        convert(f"SELECT {expression} FROM staff")


@pytest.mark.parametrize("expression, expected", [
    ("sum(3) FILTER(WHERE 0)", 0),
    ("sum(toNullable(3)) FILTER(WHERE 0)", None),
    ("sum(3) FILTER(WHERE 1)", 3),
    ("avg(toNullable(3)) FILTER(WHERE 0)", None),
    ("avg(3) FILTER(WHERE 1)", 3.0),
])
def test_explicit_filter_preserves_aggregate_defaults(expression, expected):
    with sqlite3.connect(":memory:") as connection:
        connection.create_function("nan", 0, lambda: float("nan"))
        assert connection.execute(convert(f"SELECT {expression}", "sqlite")).fetchone() == (expected,)


def test_filtered_nonnullable_average_keeps_nan_default():
    assert "NAN()" in convert("SELECT avg(3) FILTER(WHERE 0)")


@pytest.mark.parametrize("function", ["sum", "avg"])
def test_windowed_filter_uses_case_and_keeps_frame(function):
    sql = convert(f"SELECT {function}(amount) FILTER(WHERE stamp>0) OVER (ORDER BY stamp ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM staff")
    assert "FILTER(" not in sql
    assert "CASE WHEN stamp > 0" in sql
    assert "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW" in sql


@pytest.mark.parametrize("filtered", [False, True])
def test_windowed_argmax_preserves_null_eligibility_and_frame(filtered):
    condition = " FILTER(WHERE stamp>1)" if filtered else ""
    sql = convert(f"SELECT argMax(name,stamp){condition} OVER (ORDER BY stamp ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING) FROM staff")
    assert "MAX_BY(CASE WHEN" in sql
    assert sql.count("NOT name IS NULL AND NOT stamp IS NULL") == 2
    assert "FILTER(" not in sql
    assert "ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING" in sql


@pytest.mark.parametrize("sql, expected", [
    ("SELECT sum(x) FROM (SELECT 1 x UNION ALL SELECT NULL x)", 1),
    ("SELECT sum(x) FROM (SELECT 1 x UNION ALL SELECT NULL x) WHERE 0=1", None),
    ("SELECT sum(x) FROM (SELECT 1 x UNION ALL SELECT 2 x) WHERE 0=1", 0),
    ("SELECT sum(x) FROM (SELECT sumIf(3,1) x)", 3),
    ("SELECT sum(x) FROM (SELECT sum(3) FILTER(WHERE 1) x)", 3),
])
def test_known_derived_aggregate_types(sql, expected):
    with sqlite3.connect(":memory:") as connection:
        assert connection.execute(convert(sql, "sqlite")).fetchone() == (expected,)


def test_outer_join_preserved_side_keeps_known_nullability():
    sql = convert("SELECT sum(a.amount), sum(a.salary) FROM staff a LEFT JOIN staff b ON a.stamp=b.stamp")
    assert "COALESCE(SUM(a.amount), 0)" in sql
    assert "SUM(a.salary)" in sql
    with pytest.raises(QueryValidationError, match="outer join"):
        convert("SELECT sum(b.amount) FROM staff a LEFT JOIN staff b ON a.stamp=b.stamp")


@pytest.mark.parametrize("expression,expected", [
    ("toDate('2024-01-01 05:00:00')", "CAST('2024-01-01' AS DATE)"),
    ("toDateTime64(1700000000,3)", "DATE_ADD('SECOND', 1700000000"),
    ("toDateTime64(-1,6)", "DATE_ADD('SECOND', -1"),
    ("toDateTime64(1700000000.1239,3)", "FROM_UNIXTIME_NANOS"),
    ("toDateTime64(CAST('1700000000.1239' AS Decimal(18,4)),3)", "TRUNCATE("),
    ("toDateTime64('2024-01-01 00:00:00.9999',3)", "'2024-01-01 00:00:00.999'"),
])
def test_temporal_conversions_do_not_emit_invalid_casts(expression, expected):
    assert expected in convert(f"SELECT {expression}")


@pytest.mark.parametrize("expression", [
    "toDate('bad date')", "toDate('2024-01-01T00:00:00+03:00')", "toDate(123)",
    "toDateTime64(1,-1)", "toDateTime64(1,10)", "toDateTime64(1,stamp)",
    "toDateTime64('bad date',3)", "toDateTime64(name,3)", "toDateTime64(unknown,3)",
])
def test_unsupported_temporal_inputs_fail_before_trino(expression):
    with pytest.raises(QueryValidationError):
        convert(f"SELECT {expression} FROM staff")


def test_float_epoch_preserves_integer_ticks_before_decimal_conversion():
    sql = convert("SELECT toDateTime64(1700000000.1234567,9)")
    assert "AS BIGINT) AS DECIMAL(38, 0))" in sql
    assert "* 1000000000" in sql


def test_nested_temporal_mapping_keeps_logical_type():
    sql = convert("SELECT toDate(toDateTime64(1700000000,3))")
    assert "DATE_ADD('SECOND', 1700000000" in sql
    assert "65536" in sql  # toDate wraps typed timestamps to unsigned epoch days.


def test_nested_union_preserves_unknown_projection_positions():
    # An unresolved first projection must not shift the known second projection.
    sql = "SELECT sum(y) FROM (SELECT unknown x, 1 y FROM staff UNION ALL SELECT unknown x, 2 y FROM staff UNION ALL SELECT unknown x, 3 y FROM staff)"
    assert "COALESCE(SUM(y), 0)" in convert(sql)
    with pytest.raises(QueryValidationError):
        convert(sql.replace("sum(y)", "sum(x)"))


@pytest.mark.parametrize('expression,fragment', [
    ("toHour(toDateTime('2024-02-29 23:47:00'))", 'HOUR('),
    ("toMinute(toDateTime('2024-02-29 23:47:00'))", 'MINUTE('),
    ("toDayOfYear(toDate32('2024-02-29'))", 'DAY_OF_YEAR('),
    ("toLastDayOfMonth(toDate32('2024-02-01'))", 'LAST_DAY_OF_MONTH('),
    ('toInt32(2147483648)', 'DECIMAL(38, 0)'),
    ('toInt32(-2147483649)', 'DECIMAL(38, 0)'),
])
def test_calendar_and_int32_function_coverage(expression, fragment):
    assert fragment in convert('SELECT ' + expression)


def test_calendar_parts_keep_nullable_derived_aggregate_contract():
    sql = convert("SELECT avg(part) FROM (SELECT toDayOfYear(CAST(NULL AS Nullable(DateTime64(6)))) AS part)")
    assert 'AVG(CAST(part AS DOUBLE))' in sql
    assert 'COALESCE(AVG' not in sql


def test_relative_day_origin_rejected_with_actionable_guidance():
    with pytest.raises(QueryValidationError, match='implementation-specific origin'):
        convert('SELECT toRelativeDayNum(today())')


@pytest.mark.parametrize('expression', ["toHour('12:30:00')", "toMinute(123)", "toInt32('12')", "toDayOfYear(now(), 'UTC')"])
def test_new_function_overloads_do_not_guess_types(expression):
    with pytest.raises(QueryValidationError):
        convert('SELECT '+expression)


@pytest.mark.parametrize('unit', ['hour', 'minute', 'second'])
def test_subday_datediff_retains_type_through_derived_aggregate(unit):
    sql = convert(f"WITH gaps AS (SELECT dateDiff('{unit}', toDateTime64('2024-02-29 10:59:59.9',6), toDateTime64('2024-02-29 11:00:00.1',6)) AS gap) SELECT avg(gap) FROM gaps")
    assert 'DATE_TRUNC' in sql and 'AVG(CAST(gap AS DOUBLE))' in sql


def test_multi_input_exact_distinct_excludes_any_null():
    sql = convert('SELECT uniqExact(name, stamp) FROM staff')
    assert 'COUNT(DISTINCT' in sql
    assert 'name IS NULL' in sql and 'stamp IS NULL' in sql
    assert '(name, stamp)' in sql


@pytest.mark.parametrize('expression', ['range(0)', 'range(2,2)', 'range(5,2)', 'range(-3,toInt32(2))'])
def test_integer_range_emits_empty_guard_and_sequence(expression):
    sql = convert('SELECT '+expression)
    assert 'SEQUENCE(' in sql and 'ARRAY(BIGINT)' in sql


def test_range_and_row_number_types_survive_derived_queries():
    assert 'BIGINT' in convert('SELECT toInt64(n) FROM (SELECT row_number() OVER () n FROM staff)')
    assert 'INTEGER' in convert('SELECT toInt32(n) FROM (SELECT arrayJoin(range(0,5)) n)')


def test_month_interval_uses_calendar_month_arithmetic():
    assert "DATE_ADD('month', -1" in convert("SELECT toDate32('2024-03-31') - toIntervalMonth(1)")
    with pytest.raises(QueryValidationError, match='arithmetic operand'):
        convert('SELECT toIntervalMonth(1)')


def test_unsigned32_wraps_integer_and_rejects_unverified_float_domain():
    assert '4294967296' in convert('SELECT toUInt32(-1)')
    with pytest.raises(QueryValidationError, match='floating-point'):
        convert('SELECT toUInt32(-12.9)')


def test_stddev_preserves_empty_input_nullability():
    assert 'NAN()' in convert('SELECT stddevPop(stamp) FROM staff')
    assert 'NAN()' not in convert('SELECT stddevPop(salary) FROM staff')


def test_subday_conversion_does_not_round_nanoseconds_before_boundary_counting():
    sql = convert("SELECT dateDiff('second', toDateTime64('2024-02-29 10:59:59.9999999',9), toDateTime64('2024-02-29 11:00:00',9))")
    assert 'TIMESTAMP(9)' in sql
    assert 'TIMESTAMP(6)' not in sql


def test_unsigned32_does_not_wrap_decimal_overflow_that_clickhouse_rejects():
    with pytest.raises(QueryValidationError, match='Decimal'):
        convert("SELECT toUInt32(CAST('-12.9' AS Decimal(18,1)))")
