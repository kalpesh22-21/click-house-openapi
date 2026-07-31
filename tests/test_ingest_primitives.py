"""Unit tests for the ingest primitives (app.ingest_primitives).

Pure-function coverage — no ClickHouse client or live server is required:
infer_column_type, coerce, validate_identifier, validate_ch_type,
compare_schemas, build_create_table_sql, parse_csv_sample, infer_schema,
infer_schema_streaming, _sanitize_column_name, _sanitize_columns,
_parse_columns_json, and _escape_sql_string.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

# ---------------------------------------------------------------------------
# Helpers under test
# ---------------------------------------------------------------------------

from app.ingest_primitives import (
    build_create_table_sql,
    coerce,
    compare_schemas,
    infer_column_type,
    infer_schema,
    infer_schema_streaming,
    parse_csv_sample,
    validate_ch_type,
    validate_identifier,
)


# ===========================================================================
# validate_identifier
# ===========================================================================


class TestValidateIdentifier:
    def test_accepts_simple_name(self):
        assert validate_identifier("my_table") == "my_table"

    def test_accepts_name_with_digits(self):
        assert validate_identifier("table123") == "table123"

    def test_accepts_leading_underscore(self):
        assert validate_identifier("_private") == "_private"

    def test_accepts_mixed_case(self):
        assert validate_identifier("MyDatabase") == "MyDatabase"

    def test_rejects_leading_digit(self):
        with pytest.raises(ValueError, match="Invalid identifier"):
            validate_identifier("1col")

    def test_rejects_hyphen(self):
        with pytest.raises(ValueError, match="Invalid identifier"):
            validate_identifier("col-name")

    def test_rejects_semicolon_injection(self):
        with pytest.raises(ValueError, match="Invalid identifier"):
            validate_identifier("a; DROP TABLE users")

    def test_rejects_backtick(self):
        with pytest.raises(ValueError, match="Invalid identifier"):
            validate_identifier("`bad`")

    def test_rejects_dot(self):
        with pytest.raises(ValueError, match="Invalid identifier"):
            validate_identifier("db.table")

    def test_rejects_space(self):
        with pytest.raises(ValueError, match="Invalid identifier"):
            validate_identifier("col name")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="Invalid identifier"):
            validate_identifier("")


# ===========================================================================
# validate_ch_type
# ===========================================================================


class TestValidateChType:
    @pytest.mark.parametrize("type_str", [
        "Int8", "Int16", "Int32", "Int64",
        "UInt8", "UInt16", "UInt32", "UInt64",
        "Float32", "Float64",
        "String", "Bool",
        "Date", "Date32", "DateTime",
        "DateTime64(3)",
        "DateTime64(6, 'UTC')",
        "Nullable(Int64)",
        "Nullable(Float64)",
        "Nullable(String)",
        "Nullable(Date)",
        "Nullable(DateTime)",
        "LowCardinality(String)",
        "LowCardinality(Nullable(String))",
    ])
    def test_accepts_valid_types(self, type_str):
        assert validate_ch_type(type_str) == type_str.strip()

    @pytest.mark.parametrize("type_str", [
        "TEXT",
        "VARCHAR(255)",
        "INTEGER",
        "BLOB",
        "Array(String)",
        "Map(String, Int64)",
        "Tuple(Int64, String)",
        "DROP TABLE foo",
        "; malicious",
        "Int64; DROP",
    ])
    def test_rejects_invalid_types(self, type_str):
        with pytest.raises(ValueError, match="Disallowed ClickHouse type"):
            validate_ch_type(type_str)

    def test_strips_surrounding_whitespace(self):
        assert validate_ch_type("  Int64  ") == "Int64"


# ===========================================================================
# infer_column_type
# ===========================================================================


class TestInferColumnType:
    def test_all_integers_returns_int64(self):
        assert infer_column_type(["1", "2", "3", "100"]) == "Int64"

    def test_mixed_int_and_float_returns_float64(self):
        assert infer_column_type(["1", "2.5", "3"]) == "Float64"

    def test_all_floats_returns_float64(self):
        assert infer_column_type(["1.1", "2.2", "3.3"]) == "Float64"

    def test_datetime_values_returns_datetime(self):
        assert infer_column_type(["2024-01-01 12:00:00", "2024-06-15 08:30:00"]) == "DateTime"

    def test_datetime_iso_t_format(self):
        assert infer_column_type(["2024-01-01T12:00:00", "2024-06-15T08:30:00"]) == "DateTime"

    def test_date_values_returns_date(self):
        assert infer_column_type(["2024-01-01", "2024-06-15"]) == "Date"

    def test_datetime_with_fractional_seconds(self):
        assert infer_column_type(
            ["2024-01-01 12:00:00.123", "2024-06-15 08:30:00.5"]
        ) == "DateTime"

    def test_datetime_with_timezone_offset(self):
        assert infer_column_type(
            ["2024-01-01T12:00:00+05:30", "2024-06-15T08:30:00-04:00"]
        ) == "DateTime"

    def test_datetime_without_seconds(self):
        assert infer_column_type(["2024-01-01 12:00", "2024-06-15 08:30"]) == "DateTime"

    def test_datetime_slash_separator(self):
        assert infer_column_type(["2024/01/01 12:00:00", "2024/06/15 08:30:00"]) == "DateTime"

    def test_datetime_mixed_iso_variants(self):
        # Mix of space, T, fractional, and offset forms in one column.
        assert infer_column_type([
            "2024-01-01 12:00:00",
            "2024-06-15T08:30:00.250",
            "2024-12-31T23:59:59Z",
        ]) == "DateTime"

    def test_date_us_mdy_format(self):
        # US month/day/year slash dates.
        assert infer_column_type(["01/15/2024", "12/31/2024"]) == "Date"

    def test_date_us_mdy_unpadded(self):
        assert infer_column_type(["1/5/2024", "12/31/2024"]) == "Date"

    def test_datetime_us_mdy_format(self):
        assert infer_column_type(["01/15/2024 10:30:00", "12/31/2024 23:59:59"]) == "DateTime"

    def test_mixed_types_returns_string(self):
        assert infer_column_type(["hello", "world", "123abc"]) == "String"

    def test_empty_column_returns_string(self):
        assert infer_column_type([]) == "String"

    def test_all_empty_values_returns_string(self):
        assert infer_column_type(["", "", ""]) == "String"

    def test_nullable_when_some_empty_int(self):
        assert infer_column_type(["1", "", "3"]) == "Nullable(Int64)"

    def test_nullable_when_some_empty_float(self):
        assert infer_column_type(["1.5", "", "3.2"]) == "Nullable(Float64)"

    def test_nullable_when_some_empty_datetime(self):
        assert infer_column_type(["2024-01-01 12:00:00", ""]) == "Nullable(DateTime)"

    def test_nullable_when_some_empty_date(self):
        assert infer_column_type(["2024-01-01", ""]) == "Nullable(Date)"

    def test_string_not_wrapped_nullable_when_empty(self):
        # Empty strings are valid String values — no Nullable wrapping.
        assert infer_column_type(["hello", "", "world"]) == "String"

    def test_date_not_mistaken_for_datetime(self):
        # Bare date "2024-01-01" must not be inferred as DateTime.
        result = infer_column_type(["2024-01-01"])
        assert result == "Date"

    def test_negative_int_still_int64(self):
        assert infer_column_type(["-1", "-200", "0"]) == "Int64"

    def test_scientific_notation_is_float64(self):
        assert infer_column_type(["1e5", "2.5e3"]) == "Float64"


# ===========================================================================
# coerce
# ===========================================================================


class TestCoerce:
    # Int types.
    def test_coerce_int64(self):
        assert coerce("42", "Int64") == 42

    def test_coerce_uint32(self):
        assert coerce("100", "UInt32") == 100

    def test_coerce_int_empty_returns_none(self):
        assert coerce("", "Int64") is None

    def test_coerce_int_invalid_returns_none(self):
        assert coerce("abc", "Int64") is None

    # Float types.
    def test_coerce_float64(self):
        assert coerce("3.14", "Float64") == pytest.approx(3.14)

    def test_coerce_float_empty_returns_none(self):
        assert coerce("", "Float64") is None

    def test_coerce_float_invalid_returns_none(self):
        assert coerce("not_a_float", "Float64") is None

    # Bool.
    def test_coerce_bool_true_string(self):
        assert coerce("true", "Bool") is True

    def test_coerce_bool_false_string(self):
        assert coerce("false", "Bool") is False

    def test_coerce_bool_1(self):
        assert coerce("1", "Bool") is True

    def test_coerce_bool_0(self):
        assert coerce("0", "Bool") is False

    def test_coerce_bool_empty_returns_none(self):
        assert coerce("", "Bool") is None

    # DateTime.
    def test_coerce_datetime_space_format(self):
        result = coerce("2024-01-15 10:30:00", "DateTime")
        assert isinstance(result, datetime)
        assert result == datetime(2024, 1, 15, 10, 30, 0)

    def test_coerce_datetime_t_format(self):
        result = coerce("2024-01-15T10:30:00", "DateTime")
        assert isinstance(result, datetime)
        assert result == datetime(2024, 1, 15, 10, 30, 0)

    def test_coerce_datetime_empty_returns_none(self):
        assert coerce("", "DateTime") is None

    def test_coerce_datetime_invalid_returns_none(self):
        assert coerce("not-a-date", "DateTime") is None

    def test_coerce_datetime64_same_as_datetime(self):
        result = coerce("2024-01-15 10:30:00", "DateTime64(3)")
        assert isinstance(result, datetime)

    def test_coerce_datetime_fractional_seconds(self):
        result = coerce("2024-01-15 10:30:00.250", "DateTime")
        assert isinstance(result, datetime)
        assert result == datetime(2024, 1, 15, 10, 30, 0, 250000)

    def test_coerce_datetime_with_offset_normalized_to_utc(self):
        # +05:30 offset → converted to naive UTC.
        result = coerce("2024-01-15T10:30:00+05:30", "DateTime")
        assert isinstance(result, datetime)
        assert result.tzinfo is None
        assert result == datetime(2024, 1, 15, 5, 0, 0)

    def test_coerce_datetime_without_seconds(self):
        result = coerce("2024-01-15 10:30", "DateTime")
        assert result == datetime(2024, 1, 15, 10, 30, 0)

    def test_coerce_datetime_slash_separator(self):
        result = coerce("2024/01/15 10:30:00", "DateTime")
        assert result == datetime(2024, 1, 15, 10, 30, 0)

    def test_coerce_date_us_mdy(self):
        # US month/day/year: 03/04/2024 is March 4, not April 3.
        assert coerce("03/04/2024", "Date") == date(2024, 3, 4)

    def test_coerce_datetime_us_mdy(self):
        assert coerce("1/5/2024 10:30:00", "DateTime") == datetime(2024, 1, 5, 10, 30, 0)

    def test_coerce_date_invalid_us_month_returns_none(self):
        # Month 13 is invalid under US m/d/y ordering → rejected.
        assert coerce("13/01/2024", "Date") is None

    # Date.
    def test_coerce_date(self):
        result = coerce("2024-06-01", "Date")
        assert isinstance(result, date)
        assert result == date(2024, 6, 1)

    def test_coerce_date_empty_returns_none(self):
        assert coerce("", "Date") is None

    def test_coerce_date32(self):
        result = coerce("2024-06-01", "Date32")
        assert isinstance(result, date)

    # Nullable.
    def test_coerce_nullable_int64_value(self):
        assert coerce("99", "Nullable(Int64)") == 99

    def test_coerce_nullable_int64_empty_returns_none(self):
        assert coerce("", "Nullable(Int64)") is None

    def test_coerce_nullable_float64_value(self):
        assert coerce("1.5", "Nullable(Float64)") == pytest.approx(1.5)

    def test_coerce_nullable_float64_empty_returns_none(self):
        assert coerce("", "Nullable(Float64)") is None

    def test_coerce_nullable_datetime_value(self):
        result = coerce("2024-01-01 00:00:00", "Nullable(DateTime)")
        assert isinstance(result, datetime)

    # String.
    def test_coerce_string_passthrough(self):
        assert coerce("hello world", "String") == "hello world"

    def test_coerce_string_empty_returns_empty_string(self):
        # Fix #8: empty string for plain String type returns "" (empty string is
        # a valid String value).  Only Nullable(String) returns None for empty.
        assert coerce("", "String") == ""

    # LowCardinality.
    def test_coerce_low_cardinality_string(self):
        assert coerce("cat", "LowCardinality(String)") == "cat"


# ===========================================================================
# parse_csv_sample
# ===========================================================================


class TestParseCsvSample:
    def test_parses_header_and_rows(self):
        csv_bytes = b"id,name,value\n1,Alice,10.5\n2,Bob,20.0\n"
        header, sample, total = parse_csv_sample(csv_bytes)
        assert header == ["id", "name", "value"]
        assert len(sample) == 2
        assert total == 2

    def test_respects_sample_limit(self):
        rows = "\n".join(f"{i},name{i}" for i in range(1, 102))
        csv_bytes = f"id,name\n{rows}\n".encode()
        header, sample, total = parse_csv_sample(csv_bytes, sample_rows=50)
        assert len(sample) == 50
        assert total == 101

    def test_empty_csv_returns_empty(self):
        header, sample, total = parse_csv_sample(b"")
        assert header == []
        assert sample == []
        assert total == 0

    def test_header_only_returns_zero_rows(self):
        header, sample, total = parse_csv_sample(b"id,name\n")
        assert header == ["id", "name"]
        assert total == 0

    def test_strips_bom(self):
        # UTF-8 BOM prefix.
        csv_bytes = b"\xef\xbb\xbfid,name\n1,Alice\n"
        header, _, _ = parse_csv_sample(csv_bytes)
        assert header[0] == "id"


# ===========================================================================
# infer_schema_streaming  (full-scan type inference)
# ===========================================================================


class TestInferSchemaStreaming:
    def test_basic_types(self):
        csv_bytes = b"id,name,score\n1,Alice,9.5\n2,Bob,8.0\n"
        header, types, total = infer_schema_streaming(csv_bytes)
        assert header == ["id", "name", "score"]
        assert types == ["Int64", "String", "Float64"]
        assert total == 2

    def test_detects_null_appearing_after_first_rows(self):
        # An int column whose first empty cell is deep in the file must be
        # inferred as Nullable — a leading-sample approach would miss it.
        rows = "".join(f"{i},{i*2}\n" for i in range(1, 2001))
        rows += "2001,\n"  # first empty cell in the second column, well past row 1000
        csv_bytes = ("a,b\n" + rows).encode()
        _, types, total = infer_schema_streaming(csv_bytes)
        assert total == 2001
        assert types[0] == "Int64"
        assert types[1] == "Nullable(Int64)"

    def test_detects_float_widening_after_first_rows(self):
        # A column that looks like Int for 1500 rows but has a decimal later.
        rows = "".join(f"{i}\n" for i in range(1, 1501))
        rows += "1.5\n"
        csv_bytes = ("v\n" + rows).encode()
        _, types, _ = infer_schema_streaming(csv_bytes)
        assert types[0] == "Float64"

    def test_nullable_float(self):
        csv_bytes = b"x\n1.5\n\n3.2\n"
        _, types, _ = infer_schema_streaming(csv_bytes)
        assert types[0] == "Nullable(Float64)"

    def test_all_empty_column_is_string(self):
        csv_bytes = b"a,b\n1,\n2,\n"
        _, types, _ = infer_schema_streaming(csv_bytes)
        assert types == ["Int64", "String"]

    def test_short_rows_treated_as_empty(self):
        # Second row is missing the trailing column → counts as an empty cell.
        csv_bytes = b"a,b\n1,2\n3\n"
        _, types, _ = infer_schema_streaming(csv_bytes)
        assert types[1] == "Nullable(Int64)"

    def test_empty_csv(self):
        assert infer_schema_streaming(b"") == ([], [], 0)

    def test_invalid_utf8_raises(self):
        with pytest.raises(ValueError, match="not valid UTF-8"):
            infer_schema_streaming(b"a\n\xff\xfe\n")


# ===========================================================================
# compare_schemas
# ===========================================================================


class TestCompareSchemas:
    def _inferred(self, cols: list[tuple[str, str]]) -> list[dict]:
        return [{"name": n, "suggested_type": t} for n, t in cols]

    def _existing(self, cols: list[tuple[str, str]]) -> list[dict]:
        return [{"name": n, "type": t} for n, t in cols]

    def test_identical_schemas_match(self):
        cols = ["id", "name"]
        inferred = self._inferred([("id", "Int64"), ("name", "String")])
        existing = self._existing([("id", "Int64"), ("name", "String")])
        matches, diff = compare_schemas(cols, inferred, existing)
        assert matches is True
        assert diff["missing_in_csv"] == []
        assert diff["extra_in_csv"] == []
        assert diff["type_mismatches"] == []

    def test_nullable_treated_as_matching_base(self):
        # Inferred Nullable(Int64) vs existing Int64 should match (same base).
        cols = ["id"]
        inferred = self._inferred([("id", "Nullable(Int64)")])
        existing = self._existing([("id", "Int64")])
        matches, diff = compare_schemas(cols, inferred, existing)
        assert matches is True

    def test_missing_column_detected(self):
        # Table has 'extra_col' not in CSV.
        cols = ["id"]
        inferred = self._inferred([("id", "Int64")])
        existing = self._existing([("id", "Int64"), ("extra_col", "String")])
        matches, diff = compare_schemas(cols, inferred, existing)
        assert matches is False
        assert "extra_col" in diff["missing_in_csv"]

    def test_extra_column_in_csv_detected(self):
        cols = ["id", "new_col"]
        inferred = self._inferred([("id", "Int64"), ("new_col", "String")])
        existing = self._existing([("id", "Int64")])
        matches, diff = compare_schemas(cols, inferred, existing)
        assert matches is False
        assert "new_col" in diff["extra_in_csv"]

    def test_type_mismatch_detected(self):
        cols = ["id"]
        inferred = self._inferred([("id", "Float64")])
        existing = self._existing([("id", "Int64")])
        matches, diff = compare_schemas(cols, inferred, existing)
        assert matches is False
        assert len(diff["type_mismatches"]) == 1
        mismatch = diff["type_mismatches"][0]
        assert mismatch["name"] == "id"
        assert mismatch["csv_type"] == "Float64"
        assert mismatch["table_type"] == "Int64"


# ===========================================================================
# build_create_table_sql
# ===========================================================================


class TestBuildCreateTableSql:
    def test_basic_create_with_order_by(self):
        sql = build_create_table_sql(
            database="mydb",
            table="events",
            columns=[{"name": "id", "type": "Int64"}, {"name": "ts", "type": "DateTime"}],
            order_by=["id"],
        )
        assert "CREATE TABLE `mydb`.`events`" in sql
        assert "`id` Int64" in sql
        assert "`ts` DateTime" in sql
        assert "ENGINE = MergeTree" in sql
        assert "ORDER BY (`id`)" in sql

    def test_create_with_empty_order_by_uses_tuple(self):
        sql = build_create_table_sql(
            database="mydb",
            table="raw",
            columns=[{"name": "data", "type": "String"}],
            order_by=[],
        )
        assert "ORDER BY tuple()" in sql

    def test_create_with_multiple_order_by_columns(self):
        sql = build_create_table_sql(
            database="db",
            table="t",
            columns=[
                {"name": "user_id", "type": "Int64"},
                {"name": "event_time", "type": "DateTime"},
            ],
            order_by=["user_id", "event_time"],
        )
        assert "ORDER BY (`user_id`, `event_time`)" in sql

    def test_rejects_invalid_database_identifier(self):
        with pytest.raises(ValueError):
            build_create_table_sql(
                database="bad-db",
                table="t",
                columns=[{"name": "x", "type": "Int64"}],
                order_by=[],
            )

    def test_rejects_invalid_column_name(self):
        with pytest.raises(ValueError):
            build_create_table_sql(
                database="db",
                table="t",
                columns=[{"name": "col; DROP TABLE", "type": "Int64"}],
                order_by=[],
            )

    def test_rejects_invalid_column_type(self):
        with pytest.raises(ValueError):
            build_create_table_sql(
                database="db",
                table="t",
                columns=[{"name": "x", "type": "TEXT"}],
                order_by=[],
            )

    def test_rejects_empty_columns(self):
        with pytest.raises(ValueError, match="empty"):
            build_create_table_sql("db", "t", [], [])

    def test_nullable_column_type_in_ddl(self):
        sql = build_create_table_sql(
            database="db",
            table="t",
            columns=[{"name": "score", "type": "Nullable(Float64)"}],
            order_by=[],
        )
        assert "`score` Nullable(Float64)" in sql


# ===========================================================================
# Additional gap-coverage tests
# ===========================================================================


# ---------------------------------------------------------------------------
# validate_identifier — additional injection patterns not yet covered
# ---------------------------------------------------------------------------


class TestValidateIdentifierAdditional:
    def test_rejects_paren_injection(self):
        """a) ENGINE=Log AS SELECT 1 must be rejected."""
        with pytest.raises(ValueError, match="Invalid identifier"):
            validate_identifier("a) ENGINE=Log AS SELECT 1")

    def test_rejects_newline(self):
        with pytest.raises(ValueError, match="Invalid identifier"):
            validate_identifier("col\nDROP")

    def test_rejects_slash(self):
        with pytest.raises(ValueError, match="Invalid identifier"):
            validate_identifier("db/table")

    def test_accepts_single_letter(self):
        assert validate_identifier("x") == "x"

    def test_accepts_single_underscore(self):
        assert validate_identifier("_") == "_"


# ---------------------------------------------------------------------------
# validate_ch_type — additional whitelist entries not yet covered
# ---------------------------------------------------------------------------


class TestValidateChTypeAdditional:
    @pytest.mark.parametrize("type_str", [
        "Nullable(Bool)",
        "Nullable(Date32)",
        "DateTime64(6, 'America/New_York')",
        "DateTime64(0)",
        "LowCardinality(Nullable(String))",
        "UInt64",
        "Int8",
    ])
    def test_accepts_additional_valid_types(self, type_str):
        assert validate_ch_type(type_str) == type_str.strip()

    @pytest.mark.parametrize("type_str", [
        "LowCardinality(Int64)",
        "Nullable(LowCardinality(String))",
        "Int128",
        "FixedString(10)",
        "Array(String)",
        "Map(String, Int64)",
        "Tuple(Int64, String)",
    ])
    def test_rejects_additional_invalid_types(self, type_str):
        with pytest.raises(ValueError, match="Disallowed ClickHouse type"):
            validate_ch_type(type_str)


# ---------------------------------------------------------------------------
# infer_column_type — additional edge cases
# ---------------------------------------------------------------------------


class TestInferColumnTypeAdditional:
    def test_datetime_with_z_suffix(self):
        """ISO 8601 with trailing Z must be inferred as DateTime."""
        assert infer_column_type(["2024-01-01T12:00:00Z"]) == "DateTime"

    def test_single_int_value(self):
        assert infer_column_type(["42"]) == "Int64"

    def test_single_float_value(self):
        assert infer_column_type(["3.14"]) == "Float64"

    def test_single_date_value(self):
        assert infer_column_type(["2024-06-01"]) == "Date"

    def test_negative_float(self):
        assert infer_column_type(["-1.5", "-2.3"]) == "Float64"

    def test_nullable_datetime_with_z_suffix(self):
        """Nullable wrapping when empty present alongside Z-suffix datetime."""
        assert infer_column_type(["2024-01-01T12:00:00Z", ""]) == "Nullable(DateTime)"

    def test_mixed_int_and_string_returns_string(self):
        """When mix of ints and non-numeric strings, result is String."""
        assert infer_column_type(["1", "two", "3"]) == "String"

    def test_all_zeros(self):
        """All-zero int column is Int64, not Float64."""
        assert infer_column_type(["0", "0", "0"]) == "Int64"

    def test_single_empty_value(self):
        """Single empty value with no non-empty values defaults to String."""
        assert infer_column_type([""]) == "String"


# ---------------------------------------------------------------------------
# coerce — additional edge cases not yet covered
# ---------------------------------------------------------------------------


class TestCoerceAdditional:
    def test_coerce_bool_true_uppercase(self):
        assert coerce("TRUE", "Bool") is True

    def test_coerce_bool_false_uppercase(self):
        assert coerce("FALSE", "Bool") is False

    def test_coerce_bool_invalid_value_returns_none(self):
        assert coerce("yes", "Bool") is None

    def test_coerce_bool_invalid_string_returns_none(self):
        assert coerce("maybe", "Bool") is None

    def test_coerce_datetime_with_z_suffix(self):
        result = coerce("2024-01-15T10:30:00Z", "DateTime")
        assert isinstance(result, datetime)
        assert result == datetime(2024, 1, 15, 10, 30, 0)

    def test_coerce_datetime_fallback_to_date_at_midnight(self):
        """A date-only string coerced to DateTime returns midnight datetime."""
        result = coerce("2024-01-15", "DateTime")
        assert isinstance(result, datetime)
        assert result == datetime(2024, 1, 15, 0, 0, 0)

    def test_coerce_datetime64_with_precision(self):
        result = coerce("2024-01-15 10:30:00", "DateTime64(6)")
        assert isinstance(result, datetime)

    def test_coerce_low_cardinality_nullable_string_value(self):
        assert coerce("cat", "LowCardinality(Nullable(String))") == "cat"

    def test_coerce_low_cardinality_nullable_string_empty_returns_none(self):
        assert coerce("", "LowCardinality(Nullable(String))") is None

    def test_coerce_unknown_type_passthrough(self):
        """Unknown/unrecognised type falls through to str passthrough."""
        assert coerce("some_value", "CUSTOM_TYPE") == "some_value"

    def test_coerce_nullable_string_value_passthrough(self):
        assert coerce("hello", "Nullable(String)") == "hello"

    def test_coerce_nullable_string_empty_returns_none(self):
        assert coerce("", "Nullable(String)") is None

    def test_coerce_nullable_bool_true(self):
        assert coerce("true", "Nullable(Bool)") is True

    def test_coerce_nullable_date_value(self):
        result = coerce("2024-06-01", "Nullable(Date)")
        assert isinstance(result, date)
        assert result == date(2024, 6, 1)

    def test_coerce_nullable_date_empty_returns_none(self):
        assert coerce("", "Nullable(Date)") is None

    def test_coerce_int_negative(self):
        assert coerce("-42", "Int64") == -42

    def test_coerce_float_negative(self):
        assert coerce("-3.14", "Float64") == pytest.approx(-3.14)


# ---------------------------------------------------------------------------
# compare_schemas — additional edge cases
# ---------------------------------------------------------------------------


class TestCompareSchemasAdditional:
    def _inferred(self, cols):
        return [{"name": n, "suggested_type": t} for n, t in cols]

    def _existing(self, cols):
        return [{"name": n, "type": t} for n, t in cols]

    def test_table_nullable_csv_plain_matches(self):
        """Table has Int64, CSV infers Int64 — should match."""
        cols = ["id"]
        inferred = self._inferred([("id", "Int64")])
        existing = self._existing([("id", "Nullable(Int64)")])
        matches, diff = compare_schemas(cols, inferred, existing)
        assert matches is True

    def test_both_nullable_same_base_matches(self):
        """Both sides Nullable(Int64) — should match."""
        cols = ["id"]
        inferred = self._inferred([("id", "Nullable(Int64)")])
        existing = self._existing([("id", "Nullable(Int64)")])
        matches, diff = compare_schemas(cols, inferred, existing)
        assert matches is True

    def test_both_empty_matches(self):
        """Empty CSV and empty table schema — vacuously match."""
        matches, diff = compare_schemas([], [], [])
        assert matches is True
        assert diff["missing_in_csv"] == []
        assert diff["extra_in_csv"] == []
        assert diff["type_mismatches"] == []

    def test_type_mismatch_diff_contains_correct_types(self):
        """schema_diff includes csv_type and table_type for mismatched columns."""
        cols = ["score"]
        inferred = self._inferred([("score", "Int64")])
        existing = self._existing([("score", "String")])
        matches, diff = compare_schemas(cols, inferred, existing)
        assert matches is False
        assert len(diff["type_mismatches"]) == 1
        mm = diff["type_mismatches"][0]
        assert mm["csv_type"] == "Int64"
        assert mm["table_type"] == "String"

    def test_multiple_mismatches_detected(self):
        cols = ["a", "b"]
        inferred = self._inferred([("a", "Int64"), ("b", "Float64")])
        existing = self._existing([("a", "String"), ("b", "String")])
        matches, diff = compare_schemas(cols, inferred, existing)
        assert matches is False
        assert len(diff["type_mismatches"]) == 2

    def test_extra_and_missing_simultaneously(self):
        """CSV has a column the table does not, and table has one CSV does not."""
        cols = ["id", "new_col"]
        inferred = self._inferred([("id", "Int64"), ("new_col", "String")])
        existing = self._existing([("id", "Int64"), ("old_col", "String")])
        matches, diff = compare_schemas(cols, inferred, existing)
        assert matches is False
        assert "new_col" in diff["extra_in_csv"]
        assert "old_col" in diff["missing_in_csv"]


# ---------------------------------------------------------------------------
# Fix #8: coerce() — String empty -> "", Nullable(String) empty -> None
# ---------------------------------------------------------------------------


class TestCoerceStringEmpty:
    """Empty string for String type returns "" not None (fix #8)."""

    def test_string_empty_returns_empty_string(self):
        assert coerce("", "String") == ""

    def test_nullable_string_empty_returns_none(self):
        assert coerce("", "Nullable(String)") is None

    def test_string_nonempty_passthrough(self):
        assert coerce("hello", "String") == "hello"

    def test_low_cardinality_string_empty_returns_empty_string(self):
        """LowCardinality(String) delegates to String; empty -> ""."""
        assert coerce("", "LowCardinality(String)") == ""

    def test_low_cardinality_nullable_string_empty_returns_none(self):
        """LowCardinality(Nullable(String)) empty -> None."""
        assert coerce("", "LowCardinality(Nullable(String))") is None

    def test_int64_empty_still_returns_none(self):
        """Empty for non-String, non-Nullable types still returns None."""
        assert coerce("", "Int64") is None

    def test_float64_empty_still_returns_none(self):
        assert coerce("", "Float64") is None

    def test_datetime_empty_still_returns_none(self):
        assert coerce("", "DateTime") is None


# ---------------------------------------------------------------------------
# Fix #9: UInt coercion rejects negative values
# ---------------------------------------------------------------------------


class TestUIntCoercion:
    """UInt* types must reject negative values (fix #9)."""

    def test_uint8_negative_returns_none(self):
        assert coerce("-1", "UInt8") is None

    def test_uint16_negative_returns_none(self):
        assert coerce("-100", "UInt16") is None

    def test_uint32_negative_returns_none(self):
        assert coerce("-999", "UInt32") is None

    def test_uint64_negative_returns_none(self):
        assert coerce("-1", "UInt64") is None

    def test_uint8_zero_is_valid(self):
        assert coerce("0", "UInt8") == 0

    def test_uint32_positive_is_valid(self):
        assert coerce("255", "UInt32") == 255

    def test_uint64_large_positive_is_valid(self):
        assert coerce("18446744073709551615", "UInt64") == 18446744073709551615

    def test_int64_negative_is_still_valid(self):
        """Signed Int64 must still accept negative values."""
        assert coerce("-42", "Int64") == -42

    def test_uint_invalid_string_returns_none(self):
        assert coerce("abc", "UInt32") is None


# ===========================================================================
# New tests: column name sanitization, COMMENT DDL, analyze report shape
# ===========================================================================

from app.ingest_primitives import (
    _sanitize_column_name,
    _sanitize_columns,
    _parse_columns_json,
    _escape_sql_string,
    _SAFE_IDENTIFIER_RE,
)


# ---------------------------------------------------------------------------
# _sanitize_column_name
# ---------------------------------------------------------------------------


class TestSanitizeColumnName:
    """Unit tests for _sanitize_column_name."""

    def test_order_date(self):
        assert _sanitize_column_name("Order Date") == "order_date"

    def test_total_usd(self):
        assert _sanitize_column_name("Total (USD)") == "total_usd"

    def test_leading_digit(self):
        assert _sanitize_column_name("2024 Sales") == "_2024_sales"

    def test_only_dashes_returns_column(self):
        assert _sanitize_column_name("  --  ") == "column"

    def test_simple_lowercase(self):
        assert _sanitize_column_name("name") == "name"

    def test_mixed_case_lowercased(self):
        assert _sanitize_column_name("MyColumn") == "mycolumn"

    def test_already_snake_case(self):
        assert _sanitize_column_name("user_id") == "user_id"

    def test_special_chars_replaced(self):
        result = _sanitize_column_name("col!@#$%")
        assert result == "col"

    def test_multiple_spaces_collapsed(self):
        assert _sanitize_column_name("a  b") == "a_b"

    def test_leading_trailing_stripped(self):
        assert _sanitize_column_name("  hello  ") == "hello"

    def test_empty_string_returns_column(self):
        assert _sanitize_column_name("") == "column"

    def test_all_spaces_returns_column(self):
        assert _sanitize_column_name("   ") == "column"

    def test_digit_only_gets_prefix(self):
        assert _sanitize_column_name("123") == "_123"

    def test_unicode_replaced(self):
        # Non-ascii letters become underscores, then collapsed/stripped.
        result = _sanitize_column_name("café")
        assert _SAFE_IDENTIFIER_RE.match(result) is not None

    def test_result_always_matches_ident_re(self):
        cases = [
            "Order Date", "Total (USD)", "2024 Sales", "  --  ",
            "myCol", "123abc", "", "   ", "café", "col!name",
            "  leading", "trailing  ", "__double__", "a.b.c",
        ]
        for case in cases:
            result = _sanitize_column_name(case)
            assert _SAFE_IDENTIFIER_RE.match(result) is not None, (
                f"_sanitize_column_name({case!r}) = {result!r} does not match _IDENT_RE"
            )

    def test_slash_replaced(self):
        assert _sanitize_column_name("revenue/sales") == "revenue_sales"

    def test_tab_replaced(self):
        assert _sanitize_column_name("col\tname") == "col_name"


# ---------------------------------------------------------------------------
# _sanitize_columns (de-duplication)
# ---------------------------------------------------------------------------


class TestSanitizeColumns:
    def test_dedupes_collisions(self):
        header = ["Col A", "col_a", "Col-A"]
        result = _sanitize_columns(header)
        # All three sanitize to "col_a" → should be col_a, col_a_2, col_a_3.
        assert result[0] == "col_a"
        assert result[1] == "col_a_2"
        assert result[2] == "col_a_3"

    def test_preserves_order_and_length(self):
        header = ["id", "name", "value"]
        result = _sanitize_columns(header)
        assert len(result) == 3

    def test_no_collision_unchanged(self):
        header = ["id", "name", "score"]
        result = _sanitize_columns(header)
        assert result == ["id", "name", "score"]

    def test_first_occurrence_keeps_base(self):
        header = ["A B", "a_b"]
        result = _sanitize_columns(header)
        assert result[0] == "a_b"
        assert result[1] == "a_b_2"

    def test_suffix_skips_taken_names(self):
        # "a", "a" → should be "a", "a_2"
        # But if there is also "a_2" in the header, the third "a" should be "a_3".
        header = ["a", "a_2", "a"]
        result = _sanitize_columns(header)
        assert result[0] == "a"
        assert result[1] == "a_2"
        assert result[2] == "a_3"

    def test_empty_header_returns_empty(self):
        assert _sanitize_columns([]) == []

    def test_single_element(self):
        assert _sanitize_columns(["My Col"]) == ["my_col"]

    def test_collision_on_empty_names(self):
        # All empty strings → each becomes "column", then "column_2", etc.
        result = _sanitize_columns(["", "", ""])
        assert result[0] == "column"
        assert result[1] == "column_2"
        assert result[2] == "column_3"


# ---------------------------------------------------------------------------
# _create_table: COMMENT emitted when description is set
# ---------------------------------------------------------------------------


class TestCreateTableEmitsComment:
    """build_create_table_sql emits COMMENT for columns with a description."""

    def test_column_with_description_has_comment(self):
        sql = build_create_table_sql(
            database="mydb",
            table="events",
            columns=[
                {"name": "id", "type": "Int64", "description": "Primary key"},
                {"name": "ts", "type": "DateTime", "description": None},
            ],
            order_by=["id"],
        )
        assert "COMMENT 'Primary key'" in sql
        # Column without description must not have COMMENT.
        assert sql.count("COMMENT") == 1

    def test_column_without_description_has_no_comment(self):
        sql = build_create_table_sql(
            database="mydb",
            table="t",
            columns=[{"name": "val", "type": "String"}],
            order_by=[],
        )
        assert "COMMENT" not in sql

    def test_empty_string_description_has_no_comment(self):
        sql = build_create_table_sql(
            database="mydb",
            table="t",
            columns=[{"name": "val", "type": "String", "description": ""}],
            order_by=[],
        )
        assert "COMMENT" not in sql

    def test_multiple_descriptions(self):
        sql = build_create_table_sql(
            database="mydb",
            table="t",
            columns=[
                {"name": "a", "type": "Int64", "description": "First column"},
                {"name": "b", "type": "String", "description": "Second column"},
                {"name": "c", "type": "Float64"},
            ],
            order_by=[],
        )
        assert "COMMENT 'First column'" in sql
        assert "COMMENT 'Second column'" in sql
        assert sql.count("COMMENT") == 2


# ---------------------------------------------------------------------------
# _parse_columns_json: accepts optional description, rejects non-string
# ---------------------------------------------------------------------------


class TestParseColumnsJson:
    def test_accepts_entry_without_description(self):
        result = _parse_columns_json('[{"name": "id", "type": "Int64"}]')
        assert result[0]["name"] == "id"
        assert result[0]["description"] is None

    def test_accepts_entry_with_string_description(self):
        result = _parse_columns_json(
            '[{"name": "id", "type": "Int64", "description": "Primary key"}]'
        )
        assert result[0]["description"] == "Primary key"

    def test_accepts_entry_with_null_description(self):
        result = _parse_columns_json(
            '[{"name": "id", "type": "Int64", "description": null}]'
        )
        assert result[0]["description"] is None

    def test_accepts_entry_with_empty_string_description(self):
        result = _parse_columns_json(
            '[{"name": "id", "type": "Int64", "description": ""}]'
        )
        assert result[0]["description"] == ""

    def test_rejects_non_string_description_int(self):
        with pytest.raises(ValueError, match="description"):
            _parse_columns_json(
                '[{"name": "id", "type": "Int64", "description": 42}]'
            )

    def test_rejects_non_string_description_bool(self):
        with pytest.raises(ValueError, match="description"):
            _parse_columns_json(
                '[{"name": "id", "type": "Int64", "description": true}]'
            )

    def test_rejects_non_string_description_list(self):
        with pytest.raises(ValueError, match="description"):
            _parse_columns_json(
                '[{"name": "id", "type": "Int64", "description": []}]'
            )

    def test_rejects_invalid_json(self):
        with pytest.raises(ValueError, match="Invalid columns JSON"):
            _parse_columns_json("not-json")

    def test_rejects_invalid_identifier(self):
        with pytest.raises(ValueError, match="Invalid identifier"):
            _parse_columns_json('[{"name": "col; DROP", "type": "Int64"}]')

    def test_rejects_invalid_type(self):
        with pytest.raises(ValueError, match="Disallowed ClickHouse type"):
            _parse_columns_json('[{"name": "col", "type": "TEXT"}]')

    def test_multiple_columns_all_optional_description(self):
        result = _parse_columns_json(
            '[{"name": "a", "type": "Int64"}, '
            '{"name": "b", "type": "String", "description": "B col"}, '
            '{"name": "c", "type": "Float64", "description": null}]'
        )
        assert result[0]["description"] is None
        assert result[1]["description"] == "B col"
        assert result[2]["description"] is None


# ---------------------------------------------------------------------------
# FIX 2 — _escape_sql_string control-character hygiene
# ---------------------------------------------------------------------------


class TestEscapeSqlStringControlChars:
    """_escape_sql_string must handle control characters and keep existing escapes."""

    def test_newline_escaped(self):
        assert _escape_sql_string("line1\nline2") == "line1\\nline2"

    def test_carriage_return_escaped(self):
        assert _escape_sql_string("line1\rline2") == "line1\\rline2"

    def test_crlf_escaped(self):
        result = _escape_sql_string("line1\r\nline2")
        assert result == "line1\\r\\nline2"

    def test_null_byte_stripped(self):
        assert _escape_sql_string("before\x00after") == "beforeafter"

    def test_null_byte_only_becomes_empty(self):
        assert _escape_sql_string("\x00") == ""

    def test_multiple_null_bytes_stripped(self):
        assert _escape_sql_string("\x00a\x00b\x00") == "ab"

    def test_backslash_still_escaped_first(self):
        # A backslash must become \\ (not interact with newline escaping).
        assert _escape_sql_string("C:\\path") == "C:\\\\path"

    def test_single_quote_still_escaped(self):
        assert _escape_sql_string("it's") == "it\\'s"

    def test_backslash_before_newline_order(self):
        # "\\n" in the input: backslash is escaped first → "\\\\n",
        # then "\n" is NOT present so no further newline substitution on that.
        # But a literal newline \n comes from the string, tested separately.
        result = _escape_sql_string("a\nb")
        assert result == "a\\nb"

    def test_injection_payload_stays_safe(self):
        """Classic SQL injection payload stays safely within the literal.

        The payload "'); DROP TABLE x; --" must have its single-quote escaped
        to \\' so that it cannot close the surrounding SQL string literal early.
        After escaping, every ' in the output must be preceded by a backslash.
        """
        payload = "'); DROP TABLE x; --"
        escaped = _escape_sql_string(payload)
        # The single-quote must be escaped — every ' in the result has a \ before it.
        assert "\\'" in escaped
        # No bare (unescaped) single-quote must remain: check that a ' not preceded
        # by \ is absent.  We do this by stripping all \\' occurrences and verifying
        # no ' remains.
        without_escaped_quotes = escaped.replace("\\'", "")
        assert "'" not in without_escaped_quotes, (
            f"Unescaped single-quote found in: {escaped!r}"
        )

    def test_backslash_and_quote_together(self):
        """A string with both backslash and quote is handled: backslash first."""
        result = _escape_sql_string("O'Brien \\ test")
        assert result == "O\\'Brien \\\\ test"

    def test_empty_string_unchanged(self):
        assert _escape_sql_string("") == ""

    def test_plain_string_unchanged(self):
        assert _escape_sql_string("Primary key") == "Primary key"
