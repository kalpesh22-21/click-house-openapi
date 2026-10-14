"""Semantic regression cases for generated arithmetic and NULL guards.

SQLite executes the portable arithmetic/CASE subset here; these tests do not
replace live Trino integration tests. The synthetic concat_ws follows Trino's
NULL-skipping contract to ensure the emitted guard restores source semantics.
"""

import math
import sqlite3

import pytest
import sqlglot

from app.errors import QueryValidationError
from app.trino_client import _map_clickhouse_functions


def translate(expression):
    tree = sqlglot.parse_one(f"SELECT {expression}", read="clickhouse")
    _map_clickhouse_functions(tree)
    return tree.sql(dialect="trino")


@pytest.fixture
def connection():
    with sqlite3.connect(":memory:") as conn:
        conn.create_function("truncate", 1, lambda x: math.trunc(x) if x is not None else None)
        conn.create_function("concat_ws", -1, lambda sep, *xs: None if sep is None else sep.join(x for x in xs if x is not None))
        yield conn


@pytest.mark.parametrize("value,expected", [
    (0, 0), (255, 255), (256, 0), (257, 1), (-1, 255), (-256, 0),
    (-257, 255), (1.9, 1), (-1.9, 255), (255.9, 255), (256.9, 0),
    (9223372036854775807, 255), (-9223372036854775808, 0), (None, None),
])
def test_uint8_numeric_values(connection, value, expected):
    sql = translate("toUInt8(x)") + " FROM (SELECT ? AS x)"
    assert connection.execute(sql, (value,)).fetchone()[0] == expected


@pytest.mark.parametrize("literal,expected", [("'256'", 0), ("'+1'", 1), ("'18446744073709551615'", 255), ("true", 1), ("false", 0)])
def test_uint8_integer_strings_and_booleans(connection, literal, expected):
    assert connection.execute(translate(f"toUInt8({literal})")).fetchone()[0] == expected


@pytest.mark.parametrize("literal", ["'-1'", "'1.9'", "'NaN'", "'0x10'", "'secret value'"])
def test_uint8_invalid_string_literals_fail_before_dispatch(literal):
    with pytest.raises(QueryValidationError) as err:
        translate(f"toUInt8({literal})")
    assert err.value.code == "TRINO_UNSUPPORTED_SQL"
    assert literal not in err.value.message


@pytest.mark.parametrize("args,expected", [
    ("'-', 'a', 'b'", "a-b"), ("'-', 'a', NULL", None),
    ("'-', NULL, 'b'", None), ("NULL, 'a', 'b'", None),
])
def test_concat_preserves_null_arguments(connection, args, expected):
    assert connection.execute(translate(f"concatWithSeparator({args})")).fetchone()[0] == expected


def test_nested_mapping_is_applied_inside_null_guard():
    sql = translate("concatWithSeparator('-', toString(toUInt8(256)), 'x')")
    assert "TOSTRING" not in sql and "TOUINT8" not in sql
    assert "THEN NULL" in sql and "TRUNCATE" in sql


@pytest.mark.parametrize("expression,expected", [
    ("1 = 1", 1), ("((1 = 1))", 1), ("1 = 0", 0), ("1 AND NOT 0", 1),
    ("0.5 - 256.9", 0), ("(255 + 2) * 3", 3),
])
def test_uint8_expression_precedence(connection, expression, expected):
    assert connection.execute(translate(f"toUInt8({expression})")).fetchone()[0] == expected


@pytest.mark.parametrize("function", ["sumIf(3, flag)", "avgIf(3, flag)", "multiIf(flag, 'yes', 'no')"])
def test_numeric_conditions_are_boolean(function):
    assert "CAST(flag AS BOOLEAN)" in translate(function)


def test_logical_numeric_flags_are_converted_recursively():
    sql = translate("sumIf(3, flag AND NOT other_flag)")
    assert "CAST(flag AS BOOLEAN)" in sql
    assert "NOT CAST(other_flag AS BOOLEAN)" in sql


@pytest.mark.parametrize("value,expected", [(0, "no"), (1, "yes"), (2, "yes"), (-1, "yes"), (None, "no")])
def test_multiif_numeric_truth_values(connection, value, expected):
    sql = translate("multiIf(flag, 'yes', 'no')") + " FROM (SELECT ? AS flag)"
    assert connection.execute(sql, (value,)).fetchone()[0] == expected
