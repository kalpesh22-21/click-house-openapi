"""Synthetic cases from the construct matrix; no private SQL or data."""
import pytest
import sqlglot
from app.errors import QueryValidationError
from app.trino_client import _map_clickhouse_functions
from app.trino_constructs import inline_scalar_constants


def convert(sql):
    tree = sqlglot.parse_one(sql, read="clickhouse")
    inline_scalar_constants(tree)
    _map_clickhouse_functions(tree)
    return tree.sql(dialect="trino")


@pytest.mark.parametrize("expression", [
    "quantileExactLow(0.5)(x)", "quantileExactHighIf(0.5)(x, c)",
    "minIf(x,c)", "maxIf(x,c)", "intDiv(x,25000)",
    "formatReadableQuantity(x)", "arraySort(x -> x, a)",
    "groupUniqArray(10)(x)",
])
def test_unsafe_constructs_rejected(expression):
    with pytest.raises(QueryValidationError) as error:
        convert("SELECT " + expression)
    assert error.value.code == "TRINO_UNSUPPORTED_SQL"


def test_array_composition_and_tuple_positions():
    result = convert("SELECT arrayStringConcat(arrayMap(x -> concat(x.1, '=', x.2), arraySort(groupUniqArray((code, name)))), ' | ') FROM employee")
    assert "ARRAY_JOIN(TRANSFORM(ARRAY_SORT(COALESCE(ARRAY_AGG(DISTINCT" in result
    assert "x[1]" in result and "x[2]" in result
    assert "FILTER(WHERE" in result and "ARRAY[]" in result


def test_day_offset():
    assert convert("SELECT today() - 30") == "SELECT DATE_ADD('day', -30, CURRENT_DATE)"
    assert convert("SELECT today() + 2") == "SELECT DATE_ADD('day', 2, CURRENT_DATE)"


def test_scalar_with_nested_shadow_and_qualified_column():
    result = convert("WITH 25000 AS w SELECT w, e.w, (WITH 100 AS w SELECT w), (SELECT w) FROM employee e")
    assert "25000, e.w, (SELECT 100), (SELECT 25000)" in result
    assert "WITH" not in result


def test_lambda_shadow():
    result = convert("WITH 25000 AS w SELECT arrayMap(w -> w + 1, [1,2]), w")
    assert "w -> w + 1" in result and result.endswith(", 25000")


def test_expression_with_rejected():
    with pytest.raises(QueryValidationError):
        convert("WITH x + 1 AS w SELECT w FROM employee")


def test_table_cte_preserved():
    assert "WITH t AS (SELECT 2)" in convert("WITH 2 AS w, t AS (SELECT w) SELECT * FROM t")


def test_identity_paging_retains_projected_order():
    from app.trino_constructs import preserve_paging_order
    tree = sqlglot.parse_one('SELECT * FROM (SELECT count() AS n FROM employee ORDER BY n DESC) AS page_src LIMIT 25 OFFSET 0', read='clickhouse')
    preserve_paging_order(tree)
    assert tree.args['order'].sql() == 'ORDER BY n DESC'


def test_paging_with_unprojected_order_is_rejected():
    from app.trino_constructs import preserve_paging_order
    tree = sqlglot.parse_one('SELECT * FROM (SELECT x FROM employee ORDER BY y) AS page_src LIMIT 25', read='clickhouse')
    with pytest.raises(QueryValidationError):
        preserve_paging_order(tree)
