"""Unit and endpoint tests for the admin CSV ingestion feature.

All tests mock the ClickHouse client — no live server is required.

Test coverage:
- Pure helpers: infer_column_type, coerce, validate_identifier,
  validate_ch_type, compare_schemas, build_create_table_sql.
- Endpoint tests: POST /admin/analyze and POST /admin/ingest via
  FastAPI TestClient with multipart uploads.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers under test
# ---------------------------------------------------------------------------

from app.admin_ingest import (
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
# Endpoint tests — /admin/analyze
# ===========================================================================


def _make_admin_client() -> TestClient:
    """Create a TestClient backed by a fully mocked ClickHouse client.

    The lifespan pings ClickHouse — we patch get_client at the module level
    so the lifespan ping succeeds.
    """
    from app.config import get_settings
    get_settings.cache_clear()

    mock_ch = MagicMock()
    mock_ch.ping.return_value = True

    with patch("app.clickhouse_client._cached_client", return_value=mock_ch):
        from app.main import app
        return TestClient(app, raise_server_exceptions=False)


SIMPLE_CSV = b"id,name,score\n1,Alice,9.5\n2,Bob,8.0\n"

# ---------------------------------------------------------------------------
# Auth — /admin uses the static admin API key (NOT a per-tenant JWT).
# Must match conftest._TEST_ENV["API_KEY"].
# ---------------------------------------------------------------------------

ADMIN_API_KEY = "test-api-key-abc123"
AUTH = {"Authorization": f"Bearer {ADMIN_API_KEY}"}


class TestAnalyzeEndpoint:
    _FORM = {
        "host": "localhost",
        "port": "9000",
        "user": "default",
        "password": "secret",
        "secure": "false",
        "database": "mydb",
        "table": "mytable",
    }

    def _post_analyze(self, client, form=None, csv_bytes=SIMPLE_CSV, headers=None):
        data = dict(self._FORM)
        if form:
            data.update(form)
        return client.post(
            "/admin/analyze",
            data=data,
            files={"file": ("data.csv", csv_bytes, "text/csv")},
            headers=headers if headers is not None else AUTH,
        )

    def test_analyze_returns_200_on_success(self):
        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        # database exists
        mock_ch.query.side_effect = [
            MagicMock(result_rows=[[1]]),     # database_exists
            MagicMock(result_rows=[]),        # table_exists -> False
        ]
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=MagicMock(ping=lambda: True)):
                from app.config import get_settings
                get_settings.cache_clear()
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = self._post_analyze(client)
        assert resp.status_code == 200

    def test_analyze_response_shape(self):
        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        mock_ch.query.side_effect = [
            MagicMock(result_rows=[[1]]),  # database_exists
            MagicMock(result_rows=[]),     # table_exists -> False
        ]
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=MagicMock(ping=lambda: True)):
                from app.config import get_settings
                get_settings.cache_clear()
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = self._post_analyze(client)
        body = resp.json()
        required_keys = {
            "connection_ok", "database_exists", "table_exists",
            "csv_columns", "csv_row_sample_count", "inferred_columns",
            "existing_schema", "schema_matches", "schema_diff",
        }
        assert required_keys <= set(body.keys())

    def test_analyze_csv_columns_correct(self):
        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        mock_ch.query.side_effect = [
            MagicMock(result_rows=[[1]]),
            MagicMock(result_rows=[]),
        ]
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=MagicMock(ping=lambda: True)):
                from app.config import get_settings
                get_settings.cache_clear()
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = self._post_analyze(client)
        body = resp.json()
        assert body["csv_columns"] == ["id", "name", "score"]

    def test_analyze_schema_null_when_table_not_exists(self):
        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        mock_ch.query.side_effect = [
            MagicMock(result_rows=[[1]]),
            MagicMock(result_rows=[]),
        ]
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=MagicMock(ping=lambda: True)):
                from app.config import get_settings
                get_settings.cache_clear()
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = self._post_analyze(client)
        body = resp.json()
        assert body["schema_matches"] is None
        assert body["schema_diff"] is None
        assert body["existing_schema"] is None

    def test_analyze_schema_diff_when_table_exists(self):
        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        mock_ch.query.side_effect = [
            MagicMock(result_rows=[[1]]),   # database_exists
            MagicMock(result_rows=[[1]]),   # table_exists
            # existing schema query: columns name, type
            MagicMock(result_rows=[
                ["id", "Int64"],
                ["name", "String"],
                ["score", "Float64"],
            ]),
            # existing comments query: columns name, comment
            MagicMock(result_rows=[
                ["id", ""],
                ["name", ""],
                ["score", ""],
            ]),
        ]
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=MagicMock(ping=lambda: True)):
                from app.config import get_settings
                get_settings.cache_clear()
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = self._post_analyze(client)
        body = resp.json()
        assert body["table_exists"] is True
        assert body["schema_diff"] is not None

    def test_analyze_returns_400_on_connection_failure(self):
        with patch("app.admin_ingest.build_admin_client", side_effect=Exception("refused")):
            with patch("app.clickhouse_client._cached_client", return_value=MagicMock(ping=lambda: True)):
                from app.config import get_settings
                get_settings.cache_clear()
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = self._post_analyze(client)
        assert resp.status_code == 400
        assert "refused" in resp.json()["detail"]

    def test_analyze_400_on_invalid_identifier(self):
        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=MagicMock(ping=lambda: True)):
                from app.config import get_settings
                get_settings.cache_clear()
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = self._post_analyze(
                    client,
                    form={"database": "bad-db"},
                )
        assert resp.status_code == 400

    def test_analyze_401_without_auth(self):
        with patch("app.clickhouse_client._cached_client", return_value=MagicMock(ping=lambda: True)):
            from app.config import get_settings
            get_settings.cache_clear()
            from app.main import app
            client = TestClient(app, raise_server_exceptions=False)
            resp = self._post_analyze(client, headers={})
        assert resp.status_code == 401


# ===========================================================================
# Endpoint tests — /admin/ingest
# ===========================================================================


def _run_with_mocked_ingest(mock_ch: MagicMock, fn):
    """Run *fn(client)* with build_admin_client and the singleton CH client both mocked.

    The patch must remain active for the duration of the HTTP call, so we keep
    the context managers open while calling *fn*.
    """
    from app.config import get_settings
    get_settings.cache_clear()

    singleton_mock = MagicMock()
    singleton_mock.ping.return_value = True

    with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
        with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
            from app.main import app
            client = TestClient(app, raise_server_exceptions=False)
            return fn(client)


class TestIngestEndpoint:
    _BASE_FORM = {
        "host": "localhost",
        "port": "9000",
        "user": "default",
        "password": "secret",
        "secure": "false",
        "database": "mydb",
        "table": "mytable",
    }

    def _post_ingest(self, client, extra_form: dict, csv_bytes=SIMPLE_CSV, headers=None):
        data = {**self._BASE_FORM, **extra_form}
        return client.post(
            "/admin/ingest",
            data=data,
            files={"file": ("data.csv", csv_bytes, "text/csv")},
            headers=headers if headers is not None else AUTH,
        )

    def test_create_mode_returns_200(self):
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([{"name": "id", "type": "Int64"}, {"name": "name", "type": "String"}, {"name": "score", "type": "Float64"}])

        def run(client):
            return self._post_ingest(client, {"mode": "create", "columns": cols, "order_by": '["id"]'})

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200

    def test_create_mode_response_shape(self):
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([{"name": "id", "type": "Int64"}, {"name": "name", "type": "String"}, {"name": "score", "type": "Float64"}])

        def run(client):
            return self._post_ingest(client, {"mode": "create", "columns": cols})

        resp = _run_with_mocked_ingest(mock_ch, run)
        body = resp.json()
        assert {"mode", "table", "rows_in_csv", "rows_inserted", "rows_skipped", "watermark"} <= set(body.keys())

    def test_create_mode_calls_create_table_ddl(self):
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([{"name": "id", "type": "Int64"}, {"name": "name", "type": "String"}])

        def run(client):
            return self._post_ingest(client, {"mode": "create", "columns": cols, "order_by": '["id"]'})

        _run_with_mocked_ingest(mock_ch, run)
        ddl_calls = [str(c.args[0]) for c in mock_ch.command.call_args_list]
        create_calls = [c for c in ddl_calls if "CREATE TABLE" in c]
        assert len(create_calls) == 1
        assert "MergeTree" in create_calls[0]

    def test_create_mode_400_when_table_exists(self):
        mock_ch = MagicMock()
        # table_exists -> True
        mock_ch.query.return_value = MagicMock(result_rows=[[1]])

        cols = json.dumps([{"name": "id", "type": "Int64"}])

        def run(client):
            return self._post_ingest(client, {"mode": "create", "columns": cols})

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400
        assert "already exists" in resp.json()["detail"]

    def test_wipe_mode_drops_and_recreates_table(self):
        mock_ch = MagicMock()
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([{"name": "id", "type": "Int64"}, {"name": "name", "type": "String"}])

        def run(client):
            return self._post_ingest(client, {"mode": "wipe", "columns": cols})

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        ddl_calls = [str(c.args[0]) for c in mock_ch.command.call_args_list]
        assert any("DROP TABLE IF EXISTS" in c for c in ddl_calls)
        assert any("CREATE TABLE" in c for c in ddl_calls)

    def test_wipe_mode_response_watermark_null(self):
        mock_ch = MagicMock()
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([{"name": "id", "type": "Int64"}])

        def run(client):
            return self._post_ingest(client, {"mode": "wipe", "columns": cols})

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.json()["watermark"] is None

    def test_append_mode_filters_by_watermark(self):
        mock_ch = MagicMock()

        def query_side_effect(sql, parameters=None):
            if "system.tables" in sql:
                return MagicMock(result_rows=[[1]])
            if "system.columns" in sql:
                return MagicMock(result_rows=[
                    ["id", "Int64"],
                    ["ts", "DateTime"],
                ])
            if sql.startswith("SELECT max("):
                return MagicMock(result_rows=[[datetime(2024, 1, 1, 0, 0, 0)]])
            return MagicMock(result_rows=[])

        mock_ch.query.side_effect = query_side_effect
        mock_ch.insert.return_value = None

        # CSV: 2 rows, one before watermark, one after.
        csv_bytes = (
            b"id,ts\n"
            b"1,2023-12-31 00:00:00\n"  # before watermark -> skip
            b"2,2024-01-02 00:00:00\n"  # after watermark -> insert
        )

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "append", "timestamp_column": "ts"},
                csv_bytes=csv_bytes,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows_inserted"] == 1
        assert body["rows_skipped"] == 1
        assert body["watermark"] is not None

    def test_append_mode_inserts_all_when_table_empty(self):
        mock_ch = MagicMock()

        def query_side_effect(sql, parameters=None):
            if "system.tables" in sql:
                return MagicMock(result_rows=[[1]])
            if "system.columns" in sql:
                return MagicMock(result_rows=[
                    ["id", "Int64"],
                    ["ts", "DateTime"],
                ])
            if sql.startswith("SELECT max("):
                return MagicMock(result_rows=[[None]])
            return MagicMock(result_rows=[])

        mock_ch.query.side_effect = query_side_effect
        mock_ch.insert.return_value = None

        csv_bytes = b"id,ts\n1,2024-01-01 00:00:00\n2,2024-01-02 00:00:00\n"

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "append", "timestamp_column": "ts"},
                csv_bytes=csv_bytes,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows_inserted"] == 2
        assert body["rows_skipped"] == 0

    def test_append_mode_fails_when_table_not_exists(self):
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "append", "timestamp_column": "ts"},
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400
        assert "does not exist" in resp.json()["detail"]

    def test_append_mode_requires_timestamp_column(self):
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])

        def run(client):
            return self._post_ingest(client, {"mode": "append"})

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400
        assert "timestamp_column" in resp.json()["detail"]

    def test_create_mode_maps_normalized_columns_to_raw_header(self):
        # Regression: the UI normalizes column names (e.g. "User ID" → "user_id")
        # and sends those in columns_json, but the CSV still has the raw header.
        # The ingest must align declared (sanitized) names to the raw header by
        # normalized form instead of failing with "missing columns".
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])  # table absent
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([
            {"name": "user_id", "type": "Int64"},
            {"name": "full_name", "type": "String"},
        ])
        csv_bytes = b"User ID,Full Name\n1,Alice\n2,Bob\n"

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "create", "columns": cols, "order_by": '["user_id"]'},
                csv_bytes=csv_bytes,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        assert resp.json()["rows_inserted"] == 2

        # The values must land in the right columns despite the header mismatch.
        insert_kwargs = mock_ch.insert.call_args.kwargs
        assert insert_kwargs["column_names"] == ["user_id", "full_name"]
        assert insert_kwargs["data"] == [[1, "Alice"], [2, "Bob"]]

    def test_append_mode_resolves_unsanitized_timestamp_column(self):
        # Regression: the timestamp column may arrive as the raw header name
        # (e.g. "TS"), which differs from the sanitized table column ("ts").
        # Append must resolve it rather than reporting it as not found.
        mock_ch = MagicMock()

        def query_side_effect(sql, parameters=None):
            if "system.tables" in sql:
                return MagicMock(result_rows=[[1]])
            if "system.columns" in sql:
                return MagicMock(result_rows=[["id", "Int64"], ["ts", "DateTime"]])
            if sql.startswith("SELECT max("):
                # Watermark query must reference the sanitized column name.
                assert "`ts`" in sql
                return MagicMock(result_rows=[[datetime(2024, 1, 1, 0, 0, 0)]])
            return MagicMock(result_rows=[])

        mock_ch.query.side_effect = query_side_effect
        mock_ch.insert.return_value = None

        # Raw header uses uppercase names that survive identifier validation but
        # differ from their sanitized form.
        csv_bytes = b"ID,TS\n1,2024-01-02 00:00:00\n"

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "append", "timestamp_column": "TS"},
                csv_bytes=csv_bytes,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        assert resp.json()["rows_inserted"] == 1

    def test_invalid_mode_returns_400(self):
        mock_ch = MagicMock()
        cols = json.dumps([{"name": "id", "type": "Int64"}])

        def run(client):
            return self._post_ingest(client, {"mode": "invalid_mode", "columns": cols})

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400

    def test_insert_called_with_correct_column_names(self):
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([
            {"name": "id", "type": "Int64"},
            {"name": "name", "type": "String"},
            {"name": "score", "type": "Float64"},
        ])

        def run(client):
            return self._post_ingest(client, {"mode": "create", "columns": cols})

        _run_with_mocked_ingest(mock_ch, run)
        assert mock_ch.insert.called
        call_kwargs = mock_ch.insert.call_args
        assert call_kwargs.kwargs["column_names"] == ["id", "name", "score"]

    def test_create_mode_without_columns_auto_infers_from_csv(self):
        """When no columns JSON is provided, schema is auto-inferred from the CSV header."""
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        # No "columns" form field — schema should be auto-built from SIMPLE_CSV header.
        def run(client):
            return self._post_ingest(client, {"mode": "create"})

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        # DDL should have been issued with auto-inferred column names.
        ddl_calls = [str(c.args[0]) for c in mock_ch.command.call_args_list]
        create_calls = [c for c in ddl_calls if "CREATE TABLE" in c]
        assert len(create_calls) == 1
        assert "MergeTree" in create_calls[0]

    def test_ingest_400_on_bad_identifier(self):
        mock_ch = MagicMock()
        cols = json.dumps([{"name": "id", "type": "Int64"}])

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "create", "columns": cols, "database": "1invalid"},
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400

    def test_batch_insert_called_in_chunks(self):
        """When batch_size is 1 and 2 rows exist, insert is called twice."""
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([{"name": "id", "type": "Int64"}])
        csv_bytes = b"id\n1\n2\n"

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "create", "columns": cols, "batch_size": "1"},
                csv_bytes=csv_bytes,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        assert mock_ch.insert.call_count == 2

    def test_order_by_empty_array_uses_tuple(self):
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([{"name": "id", "type": "Int64"}])

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "create", "columns": cols, "order_by": "[]"},
            )

        _run_with_mocked_ingest(mock_ch, run)
        ddl_calls = [str(c.args[0]) for c in mock_ch.command.call_args_list]
        create_calls = [c for c in ddl_calls if "CREATE TABLE" in c]
        assert any("tuple()" in c for c in create_calls)

    def test_response_rows_in_csv_correct(self):
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([{"name": "id", "type": "Int64"}, {"name": "name", "type": "String"}, {"name": "score", "type": "Float64"}])

        def run(client):
            return self._post_ingest(client, {"mode": "create", "columns": cols}, csv_bytes=SIMPLE_CSV)

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.json()["rows_in_csv"] == 2

    def test_ingest_401_without_auth(self):
        cols = json.dumps([{"name": "id", "type": "Int64"}])

        def run(client):
            return self._post_ingest(client, {"mode": "create", "columns": cols}, headers={})

        mock_ch = MagicMock()
        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 401


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
# analyze service — additional scenarios
# ---------------------------------------------------------------------------


def _build_mock_analyze_client(db_exists, tbl_exists, existing_schema=None, existing_comments=None):
    """Return a mock client configured for analyze() calls.

    Provides mock responses for:
    1. database_exists
    2. table_exists
    3. existing schema (name, type) — only when tbl_exists and existing_schema given
    4. existing comments (name, comment) — only when tbl_exists and existing_schema given
    """
    mock_ch = MagicMock()
    mock_ch.ping.return_value = True
    side = [
        MagicMock(result_rows=[[1]] if db_exists else []),
        MagicMock(result_rows=[[1]] if tbl_exists else []),
    ]
    if tbl_exists and existing_schema is not None:
        side.append(MagicMock(result_rows=existing_schema))
        # Build a comments response: use provided or empty comments for each column.
        if existing_comments is not None:
            comments_rows = existing_comments
        else:
            comments_rows = [[row[0], ""] for row in existing_schema]
        side.append(MagicMock(result_rows=comments_rows))
    mock_ch.query.side_effect = side
    return mock_ch


def _analyze_with_mock(mock_ch, csv_bytes=SIMPLE_CSV, database="mydb", table="mytable"):
    with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
        with patch("app.clickhouse_client._cached_client", return_value=MagicMock(ping=lambda: True)):
            from app.config import get_settings
            get_settings.cache_clear()
            from app.main import app
            client = TestClient(app, raise_server_exceptions=False)
            return client.post(
                "/admin/analyze",
                data={
                    "host": "localhost",
                    "port": "9000",
                    "user": "default",
                    "password": "secret",
                    "secure": "false",
                    "database": database,
                    "table": table,
                },
                files={"file": ("data.csv", csv_bytes, "text/csv")},
                headers=AUTH,
            )


class TestAnalyzeEndpointAdditional:
    def test_schema_matches_true_when_schema_identical(self):
        """When table exists and schema matches CSV exactly, schema_matches is True."""
        mock_ch = _build_mock_analyze_client(
            db_exists=True,
            tbl_exists=True,
            existing_schema=[
                ["id", "Int64"],
                ["name", "String"],
                ["score", "Float64"],
            ],
        )
        resp = _analyze_with_mock(mock_ch)
        assert resp.status_code == 200
        body = resp.json()
        assert body["table_exists"] is True
        assert body["schema_matches"] is True
        assert body["schema_diff"]["type_mismatches"] == []
        assert body["schema_diff"]["missing_in_csv"] == []
        assert body["schema_diff"]["extra_in_csv"] == []

    def test_schema_matches_false_on_type_mismatch(self):
        """When table has different column types, schema_matches is False."""
        mock_ch = _build_mock_analyze_client(
            db_exists=True,
            tbl_exists=True,
            existing_schema=[
                ["id", "String"],   # CSV infers Int64
                ["name", "String"],
                ["score", "String"],  # CSV infers Float64
            ],
        )
        resp = _analyze_with_mock(mock_ch)
        assert resp.status_code == 200
        body = resp.json()
        assert body["schema_matches"] is False
        mismatches = body["schema_diff"]["type_mismatches"]
        mismatch_names = {m["name"] for m in mismatches}
        assert "id" in mismatch_names
        assert "score" in mismatch_names

    def test_schema_matches_false_on_extra_csv_column(self):
        """Table has fewer columns than CSV — schema_matches is False."""
        mock_ch = _build_mock_analyze_client(
            db_exists=True,
            tbl_exists=True,
            existing_schema=[
                ["id", "Int64"],
                ["name", "String"],
                # missing 'score'
            ],
        )
        resp = _analyze_with_mock(mock_ch)
        assert resp.status_code == 200
        body = resp.json()
        assert body["schema_matches"] is False
        assert "score" in body["schema_diff"]["extra_in_csv"]

    def test_schema_matches_false_on_missing_csv_column(self):
        """Table has more columns than CSV — schema_matches is False."""
        mock_ch = _build_mock_analyze_client(
            db_exists=True,
            tbl_exists=True,
            existing_schema=[
                ["id", "Int64"],
                ["name", "String"],
                ["score", "Float64"],
                ["extra_col", "String"],
            ],
        )
        resp = _analyze_with_mock(mock_ch)
        assert resp.status_code == 200
        body = resp.json()
        assert body["schema_matches"] is False
        assert "extra_col" in body["schema_diff"]["missing_in_csv"]

    def test_analyze_returns_400_when_ping_false(self):
        """Ping returning False must produce a 400 error."""
        mock_ch = MagicMock()
        mock_ch.ping.return_value = False

        resp = _analyze_with_mock(mock_ch)
        assert resp.status_code == 400
        assert "ping" in resp.json()["detail"].lower() or "unreachable" in resp.json()["detail"].lower()

    def test_analyze_table_not_exists_no_schema_queries(self):
        """When the table does not exist, no existing schema is fetched."""
        mock_ch = _build_mock_analyze_client(db_exists=True, tbl_exists=False)
        resp = _analyze_with_mock(mock_ch)
        assert resp.status_code == 200
        body = resp.json()
        assert body["table_exists"] is False
        assert body["existing_schema"] is None
        assert body["schema_matches"] is None

    def test_analyze_inferred_columns_reported(self):
        """Inferred column types appear in the response."""
        mock_ch = _build_mock_analyze_client(db_exists=True, tbl_exists=False)
        resp = _analyze_with_mock(mock_ch)
        body = resp.json()
        inferred = {c["name"]: c["suggested_type"] for c in body["inferred_columns"]}
        assert inferred["id"] == "Int64"
        assert inferred["name"] == "String"
        assert inferred["score"] == "Float64"


# ---------------------------------------------------------------------------
# ingest endpoint — additional gap tests
# ---------------------------------------------------------------------------


class TestIngestEndpointAdditional:
    _BASE_FORM = {
        "host": "localhost",
        "port": "9000",
        "user": "default",
        "password": "secret",
        "secure": "false",
        "database": "mydb",
        "table": "mytable",
    }

    def _post_ingest(self, client, extra_form, csv_bytes=SIMPLE_CSV, headers=None):
        data = {**self._BASE_FORM, **extra_form}
        return client.post(
            "/admin/ingest",
            data=data,
            files={"file": ("data.csv", csv_bytes, "text/csv")},
            headers=headers if headers is not None else AUTH,
        )

    def test_create_mode_does_not_create_database(self):
        """Ingest must never issue CREATE DATABASE — it uses the existing database."""
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])  # table not exists
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([{"name": "id", "type": "Int64"}])

        def run(client):
            return self._post_ingest(client, {"mode": "create", "columns": cols})

        _run_with_mocked_ingest(mock_ch, run)
        call_args = [c.args[0] for c in mock_ch.command.call_args_list]
        assert not any("CREATE DATABASE" in c for c in call_args), (
            "CREATE DATABASE must NOT be issued"
        )
        assert any("CREATE TABLE" in c for c in call_args), "CREATE TABLE must be called"

    def test_wipe_mode_without_columns_auto_infers_from_csv(self):
        """wipe mode without columns JSON auto-infers schema from the CSV header."""
        mock_ch = MagicMock()
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        # No "columns" form field.
        def run(client):
            return self._post_ingest(client, {"mode": "wipe"})

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        ddl_calls = [str(c.args[0]) for c in mock_ch.command.call_args_list]
        assert any("DROP TABLE IF EXISTS" in c for c in ddl_calls)
        assert any("CREATE TABLE" in c for c in ddl_calls)

    def test_wipe_mode_rows_skipped_is_zero(self):
        """wipe mode always reports rows_skipped=0."""
        mock_ch = MagicMock()
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([{"name": "id", "type": "Int64"}, {"name": "name", "type": "String"}])

        def run(client):
            return self._post_ingest(client, {"mode": "wipe", "columns": cols})

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        assert resp.json()["rows_skipped"] == 0

    def test_wipe_mode_drop_before_create(self):
        """DROP TABLE IF EXISTS must precede CREATE TABLE in wipe mode."""
        mock_ch = MagicMock()
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([{"name": "id", "type": "Int64"}])

        def run(client):
            return self._post_ingest(client, {"mode": "wipe", "columns": cols})

        _run_with_mocked_ingest(mock_ch, run)
        call_args = [c.args[0] for c in mock_ch.command.call_args_list]
        drop_idx = next((i for i, c in enumerate(call_args) if "DROP TABLE IF EXISTS" in c), None)
        create_idx = next((i for i, c in enumerate(call_args) if "CREATE TABLE" in c), None)
        assert drop_idx is not None, "DROP TABLE IF EXISTS must be called"
        assert create_idx is not None, "CREATE TABLE must be called"
        assert drop_idx < create_idx, "DROP must precede CREATE"

    def test_append_row_at_watermark_is_skipped(self):
        """Row with timestamp exactly equal to watermark must be skipped (strictly >)."""
        mock_ch = MagicMock()

        def query_side_effect(sql, parameters=None):
            if "system.tables" in sql:
                return MagicMock(result_rows=[[1]])
            if "system.columns" in sql:
                return MagicMock(result_rows=[["id", "Int64"], ["ts", "DateTime"]])
            if sql.startswith("SELECT max("):
                return MagicMock(result_rows=[[datetime(2024, 1, 15, 0, 0, 0)]])
            return MagicMock(result_rows=[])

        mock_ch.query.side_effect = query_side_effect
        mock_ch.insert.return_value = None

        csv_bytes = (
            b"id,ts\n"
            b"1,2024-01-15 00:00:00\n"  # AT watermark -> skip
            b"2,2024-01-16 00:00:00\n"  # after watermark -> insert
        )

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "append", "timestamp_column": "ts"},
                csv_bytes=csv_bytes,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows_inserted"] == 1
        assert body["rows_skipped"] == 1

    def test_append_with_null_max_from_nonempty_result_keeps_all_rows(self):
        """When max() returns a NULL value (all NULLs in column), all rows are kept."""
        mock_ch = MagicMock()

        def query_side_effect(sql, parameters=None):
            if "system.tables" in sql:
                return MagicMock(result_rows=[[1]])
            if "system.columns" in sql:
                return MagicMock(result_rows=[["id", "Int64"], ["ts", "DateTime"]])
            if sql.startswith("SELECT max("):
                # ClickHouse returns a row with NULL when all values are NULL
                return MagicMock(result_rows=[[None]])
            return MagicMock(result_rows=[])

        mock_ch.query.side_effect = query_side_effect
        mock_ch.insert.return_value = None

        csv_bytes = b"id,ts\n1,2024-01-01 00:00:00\n2,2024-01-02 00:00:00\n"

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "append", "timestamp_column": "ts"},
                csv_bytes=csv_bytes,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows_inserted"] == 2
        assert body["rows_skipped"] == 0
        # watermark should be reported as None (the NULL max)
        assert body["watermark"] is None

    def test_append_timestamp_column_not_in_csv_returns_400(self):
        """If timestamp_column is not present in the CSV header, 400 is returned."""
        mock_ch = MagicMock()

        def query_side_effect(sql, parameters=None):
            if "system.tables" in sql:
                return MagicMock(result_rows=[[1]])
            if "system.columns" in sql:
                # Table has id and ts columns; CSV only has id and name.
                return MagicMock(result_rows=[["id", "Int64"], ["ts", "DateTime"]])
            if sql.startswith("SELECT max("):
                return MagicMock(result_rows=[[datetime(2024, 1, 1, 0, 0, 0)]])
            return MagicMock(result_rows=[])

        mock_ch.query.side_effect = query_side_effect

        # CSV does NOT have the 'ts' column (which is both the table column and
        # the timestamp_column).  The missing-columns check fires first and
        # reports 'ts' as the missing column.
        csv_bytes = b"id,name\n1,Alice\n2,Bob\n"

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "append", "timestamp_column": "ts"},
                csv_bytes=csv_bytes,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400
        # The error reports the missing schema column(s).
        assert "ts" in resp.json()["detail"]

    def test_append_table_no_columns_returns_400(self):
        """If the existing table has no columns, append returns 400."""
        mock_ch = MagicMock()

        def query_side_effect(sql, parameters=None):
            if "system.tables" in sql:
                return MagicMock(result_rows=[[1]])  # table exists
            if "system.columns" in sql:
                return MagicMock(result_rows=[])  # no columns
            return MagicMock(result_rows=[])

        mock_ch.query.side_effect = query_side_effect

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "append", "timestamp_column": "ts"},
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400
        assert "no columns" in resp.json()["detail"].lower()

    def test_create_mode_invalid_column_type_in_json_returns_400(self):
        """If columns JSON contains a disallowed type, 400 is returned."""
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])

        # TEXT is not on the whitelist
        cols = json.dumps([{"name": "id", "type": "TEXT"}])

        def run(client):
            return self._post_ingest(client, {"mode": "create", "columns": cols})

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400

    def test_create_mode_invalid_column_name_in_json_returns_400(self):
        """If columns JSON contains an invalid column identifier, 400 is returned."""
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])

        cols = json.dumps([{"name": "col; DROP TABLE x", "type": "Int64"}])

        def run(client):
            return self._post_ingest(client, {"mode": "create", "columns": cols})

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400

    def test_create_mode_malformed_columns_json_returns_400(self):
        """If columns field is invalid JSON, 400 is returned."""
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])

        def run(client):
            return self._post_ingest(client, {"mode": "create", "columns": "not-json"})

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400

    def test_create_mode_malformed_order_by_json_returns_400(self):
        """If order_by field is invalid JSON, 400 is returned."""
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])

        cols = json.dumps([{"name": "id", "type": "Int64"}])

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "create", "columns": cols, "order_by": "not-json"},
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400

    def test_create_mode_invalid_order_by_identifier_returns_400(self):
        """If order_by contains an unsafe identifier, 400 is returned."""
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])

        cols = json.dumps([{"name": "id", "type": "Int64"}])
        order_by = json.dumps(["id; DROP TABLE x"])

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "create", "columns": cols, "order_by": order_by},
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400

    def test_append_inserts_all_rows_with_all_newer(self):
        """When all CSV rows are strictly newer than the watermark, all are inserted."""
        mock_ch = MagicMock()

        def query_side_effect(sql, parameters=None):
            if "system.tables" in sql:
                return MagicMock(result_rows=[[1]])
            if "system.columns" in sql:
                return MagicMock(result_rows=[["id", "Int64"], ["ts", "DateTime"]])
            if sql.startswith("SELECT max("):
                return MagicMock(result_rows=[[datetime(2024, 1, 1, 0, 0, 0)]])
            return MagicMock(result_rows=[])

        mock_ch.query.side_effect = query_side_effect
        mock_ch.insert.return_value = None

        csv_bytes = (
            b"id,ts\n"
            b"1,2024-02-01 00:00:00\n"
            b"2,2024-03-01 00:00:00\n"
            b"3,2024-04-01 00:00:00\n"
        )

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "append", "timestamp_column": "ts"},
                csv_bytes=csv_bytes,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows_inserted"] == 3
        assert body["rows_skipped"] == 0

    def test_append_skips_all_rows_when_all_older_or_equal(self):
        """When all CSV rows are at or before the watermark, none are inserted."""
        mock_ch = MagicMock()

        def query_side_effect(sql, parameters=None):
            if "system.tables" in sql:
                return MagicMock(result_rows=[[1]])
            if "system.columns" in sql:
                return MagicMock(result_rows=[["id", "Int64"], ["ts", "DateTime"]])
            if sql.startswith("SELECT max("):
                return MagicMock(result_rows=[[datetime(2024, 6, 1, 0, 0, 0)]])
            return MagicMock(result_rows=[])

        mock_ch.query.side_effect = query_side_effect
        mock_ch.insert.return_value = None

        csv_bytes = (
            b"id,ts\n"
            b"1,2024-01-01 00:00:00\n"  # older
            b"2,2024-06-01 00:00:00\n"  # at watermark
        )

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "append", "timestamp_column": "ts"},
                csv_bytes=csv_bytes,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows_inserted"] == 0
        assert body["rows_skipped"] == 2

    def test_append_watermark_value_reported_in_response(self):
        """The watermark value from max() is returned as a string in the response."""
        mock_ch = MagicMock()
        wm = datetime(2024, 3, 15, 12, 30, 0)

        def query_side_effect(sql, parameters=None):
            if "system.tables" in sql:
                return MagicMock(result_rows=[[1]])
            if "system.columns" in sql:
                return MagicMock(result_rows=[["id", "Int64"], ["ts", "DateTime"]])
            if sql.startswith("SELECT max("):
                return MagicMock(result_rows=[[wm]])
            return MagicMock(result_rows=[])

        mock_ch.query.side_effect = query_side_effect
        mock_ch.insert.return_value = None

        csv_bytes = b"id,ts\n1,2024-04-01 00:00:00\n"

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "append", "timestamp_column": "ts"},
                csv_bytes=csv_bytes,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        body = resp.json()
        assert body["watermark"] == str(wm)

    def test_ingest_invalid_table_identifier_returns_400(self):
        """Table name with injection characters must return 400."""
        mock_ch = MagicMock()
        cols = json.dumps([{"name": "id", "type": "Int64"}])

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "create", "columns": cols, "table": "bad; DROP TABLE x"},
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400

    def test_ingest_timestamp_column_invalid_identifier_returns_400(self):
        """timestamp_column with invalid identifier must return 400."""
        mock_ch = MagicMock()

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "append", "timestamp_column": "ts; DROP TABLE x"},
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400


# ===========================================================================
# New tests for fixes #1–#9
# ===========================================================================


# ---------------------------------------------------------------------------
# Fix #1: column mapping by name (CSV order != declared order)
# ---------------------------------------------------------------------------


class TestColumnMappingByName:
    """CSV column order different from declared/table column order must still
    produce correct per-column values (fix #1).
    """

    _BASE_FORM = {
        "host": "localhost",
        "port": "9000",
        "user": "default",
        "password": "secret",
        "secure": "false",
        "database": "mydb",
        "table": "mytable",
    }

    def _post_ingest(self, client, extra_form, csv_bytes, headers=None):
        data = {**self._BASE_FORM, **extra_form}
        return client.post(
            "/admin/ingest",
            data=data,
            files={"file": ("data.csv", csv_bytes, "text/csv")},
            headers=headers if headers is not None else AUTH,
        )

    def test_create_mode_columns_by_name_csv_order_reversed(self):
        """Create mode: CSV has (score, name, id) but declared order is (id, name, score).
        The inserted row must have values in declared order: [1, 'Alice', 9.5].
        """
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        # Declared order: id (Int64), name (String), score (Float64)
        cols = json.dumps([
            {"name": "id", "type": "Int64"},
            {"name": "name", "type": "String"},
            {"name": "score", "type": "Float64"},
        ])
        # CSV column order is reversed: score, name, id
        csv_bytes = b"score,name,id\n9.5,Alice,1\n"

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "create", "columns": cols, "order_by": '["id"]'},
                csv_bytes=csv_bytes,
            )

        _run_with_mocked_ingest(mock_ch, run)
        assert mock_ch.insert.called
        inserted_data = mock_ch.insert.call_args.kwargs["data"]
        # Declared order: id=1, name='Alice', score=9.5
        assert inserted_data[0][0] == 1       # id
        assert inserted_data[0][1] == "Alice"  # name
        assert inserted_data[0][2] == pytest.approx(9.5)  # score

    def test_wipe_mode_columns_by_name(self):
        """Wipe mode: same name-based mapping guarantee."""
        mock_ch = MagicMock()
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([
            {"name": "ts", "type": "DateTime"},
            {"name": "value", "type": "Float64"},
        ])
        # CSV has value before ts
        csv_bytes = b"value,ts\n3.14,2024-01-01 00:00:00\n"

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "wipe", "columns": cols},
                csv_bytes=csv_bytes,
            )

        _run_with_mocked_ingest(mock_ch, run)
        assert mock_ch.insert.called
        inserted_data = mock_ch.insert.call_args.kwargs["data"]
        from datetime import datetime as dt
        # Declared order: ts first, value second
        assert isinstance(inserted_data[0][0], dt)
        assert inserted_data[0][1] == pytest.approx(3.14)

    def test_append_mode_columns_by_name_csv_order_reversed(self):
        """Append mode: columns from DB schema; CSV order differs.
        Values must land in DB column order.
        """
        mock_ch = MagicMock()

        def query_side_effect(sql, parameters=None):
            if "system.tables" in sql:
                return MagicMock(result_rows=[[1]])
            if "system.columns" in sql:
                # DB schema: id (Int64), ts (DateTime)
                return MagicMock(result_rows=[
                    ["id", "Int64"],
                    ["ts", "DateTime"],
                ])
            if sql.startswith("SELECT max("):
                return MagicMock(result_rows=[[None]])  # empty table
            return MagicMock(result_rows=[])

        mock_ch.query.side_effect = query_side_effect
        mock_ch.insert.return_value = None

        # CSV has ts before id (reversed from DB order)
        csv_bytes = b"ts,id\n2024-03-01 00:00:00,42\n"

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "append", "timestamp_column": "ts"},
                csv_bytes=csv_bytes,
            )

        _run_with_mocked_ingest(mock_ch, run)
        assert mock_ch.insert.called
        inserted_data = mock_ch.insert.call_args.kwargs["data"]
        # DB column order: id first, ts second
        assert inserted_data[0][0] == 42
        from datetime import datetime as dt
        assert isinstance(inserted_data[0][1], dt)

    def test_create_mode_missing_csv_column_returns_400(self):
        """If a declared column is absent from the CSV header, 400 is returned
        listing the missing column name.
        """
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None

        cols = json.dumps([
            {"name": "id", "type": "Int64"},
            {"name": "missing_col", "type": "String"},
        ])
        # CSV only has 'id', not 'missing_col'
        csv_bytes = b"id\n1\n2\n"

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "create", "columns": cols},
                csv_bytes=csv_bytes,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400
        assert "missing_col" in resp.json()["detail"]

    def test_append_mode_missing_csv_column_returns_400(self):
        """If the existing table has a column absent from the CSV header, 400."""
        mock_ch = MagicMock()

        def query_side_effect(sql, parameters=None):
            if "system.tables" in sql:
                return MagicMock(result_rows=[[1]])
            if "system.columns" in sql:
                return MagicMock(result_rows=[
                    ["id", "Int64"],
                    ["required_col", "String"],
                ])
            if sql.startswith("SELECT max("):
                return MagicMock(result_rows=[[None]])
            return MagicMock(result_rows=[])

        mock_ch.query.side_effect = query_side_effect

        # CSV only has 'id', missing 'required_col'
        csv_bytes = b"id\n1\n2\n"

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "append", "timestamp_column": "id"},
                csv_bytes=csv_bytes,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400
        assert "required_col" in resp.json()["detail"]

    def test_create_mode_missing_multiple_csv_columns_400_lists_all(self):
        """If multiple declared columns are absent, error message lists all of them."""
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None

        cols = json.dumps([
            {"name": "id", "type": "Int64"},
            {"name": "alpha", "type": "String"},
            {"name": "beta", "type": "Float64"},
        ])
        # CSV only has 'id'
        csv_bytes = b"id\n1\n"

        def run(client):
            return self._post_ingest(
                client,
                {"mode": "create", "columns": cols},
                csv_bytes=csv_bytes,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "alpha" in detail
        assert "beta" in detail


# ---------------------------------------------------------------------------
# Fix #2: credential redaction in ClickHouse errors
# ---------------------------------------------------------------------------


class TestCredentialRedaction:
    """ClickHouseError messages must not leak host:port details (fix #2)."""

    def test_ingest_ch_error_redacts_host_port(self):
        """A ClickHouseError raised during ingest must have host:port scrubbed."""
        from clickhouse_connect.driver.exceptions import ClickHouseError as CHError

        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.side_effect = CHError("Error at myserver.internal:9000 — bad DDL")
        mock_ch.insert.return_value = None

        cols = json.dumps([{"name": "id", "type": "Int64"}])
        csv_bytes = b"id\n1\n"

        def run(client):
            return client.post(
                "/admin/ingest",
                data={
                    "host": "myserver.internal",
                    "port": "9000",
                    "user": "default",
                    "password": "s3cr3t",
                    "secure": "false",
                    "database": "mydb",
                    "table": "mytable",
                    "mode": "create",
                    "columns": cols,
                },
                files={"file": ("data.csv", csv_bytes, "text/csv")},
                headers=AUTH,
            )

        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = run(client)

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        # Host and port must not appear in the error message.
        assert "myserver.internal" not in detail
        assert "9000" not in detail
        assert "<host>:<port>" in detail or "ClickHouse error" in detail

    def test_ingest_ch_error_redacts_password(self):
        """A ClickHouseError that leaks a password must have it scrubbed."""
        from clickhouse_connect.driver.exceptions import ClickHouseError as CHError

        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.side_effect = CHError("Auth failed for password=supersecretpwd")
        mock_ch.insert.return_value = None

        cols = json.dumps([{"name": "id", "type": "Int64"}])
        csv_bytes = b"id\n1\n"

        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    "/admin/ingest",
                    data={
                        "host": "localhost",
                        "port": "9000",
                        "user": "default",
                        "password": "supersecretpwd",
                        "secure": "false",
                        "database": "mydb",
                        "table": "mytable",
                        "mode": "create",
                        "columns": cols,
                    },
                    files={"file": ("data.csv", csv_bytes, "text/csv")},
                    headers=AUTH,
                )

        assert resp.status_code == 400
        assert "supersecretpwd" not in resp.json()["detail"]

    def test_analyze_ch_error_redacts_host_port(self):
        """A ClickHouseError raised during analyze must have host:port scrubbed."""
        from clickhouse_connect.driver.exceptions import ClickHouseError as CHError

        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        mock_ch.query.side_effect = CHError("Connection refused at dbhost.corp:8123")

        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    "/admin/analyze",
                    data={
                        "host": "dbhost.corp",
                        "port": "8123",
                        "user": "default",
                        "password": "pw",
                        "secure": "false",
                        "database": "mydb",
                        "table": "mytable",
                    },
                    files={"file": ("data.csv", b"id\n1\n", "text/csv")},
                    headers=AUTH,
                )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "dbhost.corp" not in detail
        assert "8123" not in detail


# ---------------------------------------------------------------------------
# Fix #3: upload size limit (413)
# ---------------------------------------------------------------------------


class TestUploadSizeLimit:
    """Uploads exceeding MAX_CSV_BYTES must be rejected with 413 (fix #3)."""

    _FORM = {
        "host": "localhost",
        "port": "9000",
        "user": "default",
        "password": "secret",
        "secure": "false",
        "database": "mydb",
        "table": "mytable",
    }

    def _make_big_csv(self, size_bytes: int) -> bytes:
        """Return a minimal CSV header followed by enough data rows to exceed size_bytes."""
        header = b"id\n"
        row = b"1\n"
        data = header + row * ((size_bytes // len(row)) + 1)
        return data

    def test_analyze_413_when_file_too_large(self):
        from app.routers.admin import MAX_CSV_BYTES
        big = self._make_big_csv(MAX_CSV_BYTES + 1)

        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
            from app.main import app
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/admin/analyze",
                data=self._FORM,
                files={"file": ("big.csv", big, "text/csv")},
                headers=AUTH,
            )

        assert resp.status_code == 413
        assert "200 MB" in resp.json()["detail"]

    def test_ingest_413_when_file_too_large(self):
        from app.routers.admin import MAX_CSV_BYTES
        big = self._make_big_csv(MAX_CSV_BYTES + 1)
        cols = json.dumps([{"name": "id", "type": "Int64"}])

        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
            from app.main import app
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/admin/ingest",
                data={**self._FORM, "mode": "create", "columns": cols},
                files={"file": ("big.csv", big, "text/csv")},
                headers=AUTH,
            )

        assert resp.status_code == 413
        assert "200 MB" in resp.json()["detail"]

    def test_analyze_200_when_file_at_limit(self):
        """A file exactly at MAX_CSV_BYTES is accepted (boundary condition)."""
        from app.routers.admin import MAX_CSV_BYTES

        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        mock_ch.query.side_effect = [
            MagicMock(result_rows=[[1]]),
            MagicMock(result_rows=[]),
        ]

        # Build a CSV that is exactly MAX_CSV_BYTES in length.
        header = b"id\n"
        row = b"1\n"
        filler = b"1\n" * ((MAX_CSV_BYTES - len(header)) // len(row))
        exact = (header + filler)[:MAX_CSV_BYTES]

        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    "/admin/analyze",
                    data=self._FORM,
                    files={"file": ("ok.csv", exact, "text/csv")},
                    headers=AUTH,
                )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Fix #5: batch_size < 1 returns 400
# ---------------------------------------------------------------------------


class TestBatchSizeValidation:
    """batch_size=0 or negative must return 400 (fix #5)."""

    _BASE_FORM = {
        "host": "localhost",
        "port": "9000",
        "user": "default",
        "password": "secret",
        "secure": "false",
        "database": "mydb",
        "table": "mytable",
    }

    def test_batch_size_zero_returns_400(self):
        mock_ch = MagicMock()
        cols = json.dumps([{"name": "id", "type": "Int64"}])

        def run(client):
            return client.post(
                "/admin/ingest",
                data={**self._BASE_FORM, "mode": "create", "columns": cols, "batch_size": "0"},
                files={"file": ("data.csv", b"id\n1\n", "text/csv")},
                headers=AUTH,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400
        assert "batch_size" in resp.json()["detail"]

    def test_batch_size_negative_returns_400(self):
        mock_ch = MagicMock()
        cols = json.dumps([{"name": "id", "type": "Int64"}])

        def run(client):
            return client.post(
                "/admin/ingest",
                data={**self._BASE_FORM, "mode": "create", "columns": cols, "batch_size": "-10"},
                files={"file": ("data.csv", b"id\n1\n", "text/csv")},
                headers=AUTH,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400

    def test_batch_size_one_is_valid(self):
        """batch_size=1 is the minimum valid value."""
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([{"name": "id", "type": "Int64"}])

        def run(client):
            return client.post(
                "/admin/ingest",
                data={**self._BASE_FORM, "mode": "create", "columns": cols, "batch_size": "1"},
                files={"file": ("data.csv", b"id\n1\n2\n", "text/csv")},
                headers=AUTH,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        # Two rows → two batches of size 1
        assert mock_ch.insert.call_count == 2


# ---------------------------------------------------------------------------
# Fix #6: non-UTF-8 upload returns 400
# ---------------------------------------------------------------------------


class TestNonUtf8Upload:
    """Non-UTF-8 CSV content must return 400 (fix #6)."""

    _FORM = {
        "host": "localhost",
        "port": "9000",
        "user": "default",
        "password": "secret",
        "secure": "false",
        "database": "mydb",
        "table": "mytable",
    }

    def test_analyze_non_utf8_returns_400(self):
        mock_ch = MagicMock()
        mock_ch.ping.return_value = True

        # Latin-1 encoded bytes that are not valid UTF-8.
        bad_bytes = b"id,name\n1,caf\xe9\n"

        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    "/admin/analyze",
                    data=self._FORM,
                    files={"file": ("data.csv", bad_bytes, "text/csv")},
                    headers=AUTH,
                )

        assert resp.status_code == 400
        assert "UTF-8" in resp.json()["detail"]

    def test_ingest_non_utf8_returns_400(self):
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None

        bad_bytes = b"id\n\xff\xfe\n"
        cols = json.dumps([{"name": "id", "type": "String"}])

        def run(client):
            return client.post(
                "/admin/ingest",
                data={**self._FORM, "mode": "create", "columns": cols},
                files={"file": ("data.csv", bad_bytes, "text/csv")},
                headers=AUTH,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400
        assert "UTF-8" in resp.json()["detail"]

    def test_utf8_bom_is_accepted(self):
        """UTF-8 with BOM (utf-8-sig) must still be accepted."""
        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        mock_ch.query.side_effect = [
            MagicMock(result_rows=[[1]]),
            MagicMock(result_rows=[]),
        ]
        bom_csv = b"\xef\xbb\xbfid,name\n1,Alice\n"

        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.post(
                    "/admin/analyze",
                    data=self._FORM,
                    files={"file": ("data.csv", bom_csv, "text/csv")},
                    headers=AUTH,
                )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Fix #7: String-typed watermark comparison warning
# ---------------------------------------------------------------------------


class TestStringWatermarkWarning:
    """String-typed timestamp columns trigger a warning log (fix #7)."""

    _BASE_FORM = {
        "host": "localhost",
        "port": "9000",
        "user": "default",
        "password": "secret",
        "secure": "false",
        "database": "mydb",
        "table": "mytable",
    }

    def test_string_timestamp_column_emits_warning(self, caplog):
        import logging as _logging
        mock_ch = MagicMock()

        def query_side_effect(sql, parameters=None):
            if "system.tables" in sql:
                return MagicMock(result_rows=[[1]])
            if "system.columns" in sql:
                # ts column is String type
                return MagicMock(result_rows=[["id", "Int64"], ["ts", "String"]])
            if sql.startswith("SELECT max("):
                return MagicMock(result_rows=[["2024-01-01 00:00:00"]])
            return MagicMock(result_rows=[])

        mock_ch.query.side_effect = query_side_effect
        mock_ch.insert.return_value = None

        csv_bytes = b"id,ts\n1,2024-02-01 00:00:00\n"

        with caplog.at_level(_logging.WARNING, logger="app.admin_ingest"):
            def run(client):
                return client.post(
                    "/admin/ingest",
                    data={**self._BASE_FORM, "mode": "append", "timestamp_column": "ts"},
                    files={"file": ("data.csv", csv_bytes, "text/csv")},
                    headers=AUTH,
                )

            _run_with_mocked_ingest(mock_ch, run)

        assert any("lexicographic" in r.message or "String" in r.message
                   for r in caplog.records)


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

from app.admin_ingest import (
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
# ingest: auto-sanitized names in CREATE DDL and insert column_names
# ---------------------------------------------------------------------------


def _csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    """Build CSV bytes from a header list and row lists."""
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(row))
    return "\n".join(lines).encode()


class TestIngestCreateUsesSanitizedNames:
    """When no columns_json is provided, CREATE DDL and insert use sanitized names."""

    _BASE_FORM = {
        "host": "localhost",
        "port": "9000",
        "user": "default",
        "password": "secret",
        "secure": "false",
        "database": "mydb",
        "table": "mytable",
    }

    def test_messy_headers_produce_sanitized_ddl_and_insert_names(self):
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])  # table not exists
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        csv_data = _csv_bytes(
            ["Order Date", "Total (USD)", "2024 Sales"],
            [["2024-01-01", "100.00", "500"]],
        )

        def run(client):
            return client.post(
                "/admin/ingest",
                data={**self._BASE_FORM, "mode": "create"},
                files={"file": ("data.csv", csv_data, "text/csv")},
                headers=AUTH,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200

        # Check CREATE TABLE DDL uses sanitized names.
        ddl_calls = [str(c.args[0]) for c in mock_ch.command.call_args_list]
        create_calls = [c for c in ddl_calls if "CREATE TABLE" in c]
        assert len(create_calls) == 1
        ddl = create_calls[0]
        assert "order_date" in ddl
        assert "total_usd" in ddl
        assert "_2024_sales" in ddl
        # Original messy names must NOT appear.
        assert "Order Date" not in ddl
        assert "Total (USD)" not in ddl

        # Check insert uses sanitized column names.
        assert mock_ch.insert.called
        insert_col_names = mock_ch.insert.call_args.kwargs["column_names"]
        assert insert_col_names == ["order_date", "total_usd", "_2024_sales"]


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


class TestIngestCreateWithDescriptionEmitsComment:
    """End-to-end: columns_json with description → COMMENT in DDL."""

    _BASE_FORM = {
        "host": "localhost",
        "port": "9000",
        "user": "default",
        "password": "secret",
        "secure": "false",
        "database": "mydb",
        "table": "mytable",
    }

    def test_description_appears_in_ddl(self):
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([
            {"name": "id", "type": "Int64", "description": "Primary key"},
            {"name": "name", "type": "String", "description": None},
            {"name": "score", "type": "Float64"},
        ])

        def run(client):
            return client.post(
                "/admin/ingest",
                data={**self._BASE_FORM, "mode": "create", "columns": cols},
                files={"file": ("data.csv", SIMPLE_CSV, "text/csv")},
                headers=AUTH,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200

        ddl_calls = [str(c.args[0]) for c in mock_ch.command.call_args_list]
        create_calls = [c for c in ddl_calls if "CREATE TABLE" in c]
        assert len(create_calls) == 1
        ddl = create_calls[0]
        assert "COMMENT 'Primary key'" in ddl
        # Columns without description must not have COMMENT keyword next to them.
        assert ddl.count("COMMENT") == 1

    def test_column_without_description_has_no_comment_in_ddl(self):
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([
            {"name": "id", "type": "Int64"},
            {"name": "name", "type": "String"},
            {"name": "score", "type": "Float64"},
        ])

        def run(client):
            return client.post(
                "/admin/ingest",
                data={**self._BASE_FORM, "mode": "create", "columns": cols},
                files={"file": ("data.csv", SIMPLE_CSV, "text/csv")},
                headers=AUTH,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200

        ddl_calls = [str(c.args[0]) for c in mock_ch.command.call_args_list]
        create_calls = [c for c in ddl_calls if "CREATE TABLE" in c]
        assert len(create_calls) == 1
        assert "COMMENT" not in create_calls[0]


# ---------------------------------------------------------------------------
# _escape_sql_string and description escaping
# ---------------------------------------------------------------------------


class TestDescriptionSingleQuoteEscaped:
    """Single quotes and backslashes in descriptions must be escaped in DDL."""

    def test_escape_single_quote(self):
        from app.admin_ingest import _escape_sql_string
        assert _escape_sql_string("O'Brien") == "O\\'Brien"

    def test_escape_backslash(self):
        from app.admin_ingest import _escape_sql_string
        assert _escape_sql_string("C:\\path") == "C:\\\\path"

    def test_escape_both(self):
        from app.admin_ingest import _escape_sql_string
        assert _escape_sql_string("O'Brien \\ test") == "O\\'Brien \\\\ test"

    def test_escape_empty_string(self):
        from app.admin_ingest import _escape_sql_string
        assert _escape_sql_string("") == ""

    def test_no_special_chars_unchanged(self):
        from app.admin_ingest import _escape_sql_string
        assert _escape_sql_string("Primary key") == "Primary key"

    def test_description_with_quote_in_ddl(self):
        """Description with single quote must be properly escaped in CREATE DDL."""
        sql = build_create_table_sql(
            database="mydb",
            table="t",
            columns=[{"name": "col", "type": "String", "description": "O'Brien \\ test"}],
            order_by=[],
        )
        # Backslash first (escaping order: \ → \\, then ' → \').
        assert "COMMENT 'O\\'Brien \\\\ test'" in sql
        # The raw unescaped values must not appear as injection.
        assert "O'Brien \\ test" not in sql

    def test_end_to_end_escaped_description_in_ingest(self):
        """End-to-end: description with quote/backslash is escaped in DDL via ingest."""
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([
            {"name": "id", "type": "Int64", "description": "O'Brien \\ test"},
            {"name": "name", "type": "String"},
            {"name": "score", "type": "Float64"},
        ])
        base_form = {
            "host": "localhost", "port": "9000", "user": "default",
            "password": "secret", "secure": "false",
            "database": "mydb", "table": "mytable",
        }

        def run(client):
            return client.post(
                "/admin/ingest",
                data={**base_form, "mode": "create", "columns": cols},
                files={"file": ("data.csv", SIMPLE_CSV, "text/csv")},
                headers=AUTH,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200

        ddl_calls = [str(c.args[0]) for c in mock_ch.command.call_args_list]
        create_calls = [c for c in ddl_calls if "CREATE TABLE" in c]
        assert len(create_calls) == 1
        ddl = create_calls[0]
        assert "\\'" in ddl   # escaped single quote present
        assert "\\\\" in ddl  # escaped backslash present


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
# analyze: original_name, sanitized name, and description in columns report
# ---------------------------------------------------------------------------


class TestAnalyzeReturnsOriginalAndSanitizedNamesAndDescriptions:
    """analyze() columns report includes original_name, name (sanitized), and description."""

    _FORM = {
        "host": "localhost",
        "port": "9000",
        "user": "default",
        "password": "secret",
        "secure": "false",
        "database": "mydb",
        "table": "mytable",
    }

    def _post_analyze(self, client, csv_bytes):
        return client.post(
            "/admin/analyze",
            data=self._FORM,
            files={"file": ("data.csv", csv_bytes, "text/csv")},
            headers=AUTH,
        )

    def test_messy_headers_produce_original_and_sanitized_names(self):
        """analyze() columns[i] must have original_name and sanitized name."""
        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        mock_ch.query.side_effect = [
            MagicMock(result_rows=[[1]]),   # database_exists
            MagicMock(result_rows=[]),      # table not exists
        ]

        csv_data = _csv_bytes(["Order Date", "Total (USD)"], [["2024-01-01", "100.0"]])

        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = self._post_analyze(client, csv_data)

        assert resp.status_code == 200
        body = resp.json()
        assert "columns" in body
        cols = body["columns"]
        assert cols[0]["original_name"] == "Order Date"
        assert cols[0]["name"] == "order_date"
        assert cols[1]["original_name"] == "Total (USD)"
        assert cols[1]["name"] == "total_usd"

    def test_columns_report_includes_in_csv_true_for_csv_columns(self):
        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        mock_ch.query.side_effect = [
            MagicMock(result_rows=[[1]]),
            MagicMock(result_rows=[]),
        ]
        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = self._post_analyze(client, SIMPLE_CSV)

        body = resp.json()
        for col in body["columns"]:
            assert col["in_csv"] is True

    def test_columns_report_includes_description_from_existing_table(self):
        """When table exists and has column comments, they appear in the columns report."""
        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        mock_ch.query.side_effect = [
            MagicMock(result_rows=[[1]]),   # database_exists
            MagicMock(result_rows=[[1]]),   # table_exists
            # existing schema: name, type
            MagicMock(result_rows=[
                ["id", "Int64"],
                ["name", "String"],
                ["score", "Float64"],
            ]),
            # existing comments: name, comment
            MagicMock(result_rows=[
                ["id", "Primary key"],
                ["name", ""],
                ["score", "Score value"],
            ]),
        ]

        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = self._post_analyze(client, SIMPLE_CSV)

        assert resp.status_code == 200
        body = resp.json()
        cols_by_name = {c["name"]: c for c in body["columns"]}
        assert cols_by_name["id"]["description"] == "Primary key"
        assert cols_by_name["name"]["description"] == ""
        assert cols_by_name["score"]["description"] == "Score value"

    def test_table_only_column_has_in_csv_false(self):
        """Columns present in the table but absent from the CSV have in_csv=False."""
        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        mock_ch.query.side_effect = [
            MagicMock(result_rows=[[1]]),   # database_exists
            MagicMock(result_rows=[[1]]),   # table_exists
            # Table has id, name, score, PLUS extra_col not in CSV.
            MagicMock(result_rows=[
                ["id", "Int64"],
                ["name", "String"],
                ["score", "Float64"],
                ["extra_col", "String"],
            ]),
            # Comments
            MagicMock(result_rows=[
                ["id", ""],
                ["name", ""],
                ["score", ""],
                ["extra_col", "Extra column not in CSV"],
            ]),
        ]

        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = self._post_analyze(client, SIMPLE_CSV)

        assert resp.status_code == 200
        body = resp.json()
        extra = next(c for c in body["columns"] if c["name"] == "extra_col")
        assert extra["in_csv"] is False
        assert extra["description"] == "Extra column not in CSV"
        assert extra["existing_type"] == "String"
        assert extra["inferred_type"] is None

    def test_columns_report_has_inferred_type_for_csv_columns(self):
        """inferred_type is populated for each CSV column from type inference."""
        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        mock_ch.query.side_effect = [
            MagicMock(result_rows=[[1]]),
            MagicMock(result_rows=[]),
        ]
        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = self._post_analyze(client, SIMPLE_CSV)

        body = resp.json()
        cols = {c["name"]: c for c in body["columns"]}
        assert cols["id"]["inferred_type"] == "Int64"
        assert cols["name"]["inferred_type"] == "String"
        assert cols["score"]["inferred_type"] == "Float64"


# ===========================================================================
# New tests for code-review fixes (FIX 1–3)
# ===========================================================================


# ---------------------------------------------------------------------------
# FIX 1 — _parse_columns_json robustness (non-dict, missing keys, duplicates)
# ---------------------------------------------------------------------------


class TestParseColumnsJsonRobustness:
    """_parse_columns_json must raise ValueError (not KeyError/TypeError) for
    malformed entries, and must reject duplicate column names.
    """

    def test_rejects_non_dict_item(self):
        """A non-object entry (e.g. a string) must raise ValueError, not TypeError."""
        with pytest.raises(ValueError, match="JSON object"):
            _parse_columns_json('["not_a_dict"]')

    def test_rejects_non_dict_integer_item(self):
        with pytest.raises(ValueError, match="JSON object"):
            _parse_columns_json('[42]')

    def test_rejects_item_missing_name(self):
        """An entry with 'type' but no 'name' must raise ValueError."""
        with pytest.raises(ValueError, match="'name' and 'type'"):
            _parse_columns_json('[{"type": "Int64"}]')

    def test_rejects_item_missing_type(self):
        """An entry with 'name' but no 'type' must raise ValueError."""
        with pytest.raises(ValueError, match="'name' and 'type'"):
            _parse_columns_json('[{"name": "col"}]')

    def test_rejects_item_missing_both_keys(self):
        """An entry with neither 'name' nor 'type' must raise ValueError."""
        with pytest.raises(ValueError, match="'name' and 'type'"):
            _parse_columns_json('[{}]')

    def test_rejects_duplicate_column_names(self):
        """Two entries with the same 'name' must raise ValueError."""
        with pytest.raises(ValueError, match="Duplicate column name"):
            _parse_columns_json(
                '[{"name": "id", "type": "Int64"}, {"name": "id", "type": "String"}]'
            )

    def test_rejects_duplicate_column_names_message_contains_name(self):
        """The duplicate error message must include the repeated name."""
        with pytest.raises(ValueError, match="'my_col'"):
            _parse_columns_json(
                '[{"name": "my_col", "type": "Int64"}, '
                '{"name": "my_col", "type": "Float64"}]'
            )

    def test_accepts_distinct_names(self):
        """Two entries with distinct valid names must succeed."""
        result = _parse_columns_json(
            '[{"name": "col_a", "type": "Int64"}, {"name": "col_b", "type": "String"}]'
        )
        assert len(result) == 2
        assert result[0]["name"] == "col_a"
        assert result[1]["name"] == "col_b"

    def test_non_dict_item_via_endpoint_returns_400(self):
        """Non-dict item in columns JSON must cause the ingest endpoint to return 400."""
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])

        cols = json.dumps(["not_a_dict"])

        def run(client):
            return client.post(
                "/admin/ingest",
                data={
                    "host": "localhost", "port": "9000", "user": "default",
                    "password": "secret", "secure": "false",
                    "database": "mydb", "table": "mytable",
                    "mode": "create", "columns": cols,
                },
                files={"file": ("data.csv", b"id\n1\n", "text/csv")},
                headers=AUTH,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400

    def test_duplicate_column_names_via_endpoint_returns_400(self):
        """Duplicate column names in columns JSON must cause the endpoint to return 400."""
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])

        cols = json.dumps([
            {"name": "id", "type": "Int64"},
            {"name": "id", "type": "String"},
        ])

        def run(client):
            return client.post(
                "/admin/ingest",
                data={
                    "host": "localhost", "port": "9000", "user": "default",
                    "password": "secret", "secure": "false",
                    "database": "mydb", "table": "mytable",
                    "mode": "create", "columns": cols,
                },
                files={"file": ("data.csv", b"id\n1\n", "text/csv")},
                headers=AUTH,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400
        assert "Duplicate" in resp.json()["detail"]


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


# ---------------------------------------------------------------------------
# FIX 3 — analyze: sanitized-name schema matching (no false mismatch)
# ---------------------------------------------------------------------------


class TestAnalyzeSanitizedSchemaMatch:
    """analyze() must use sanitized column names for compare_schemas and
    _build_column_report so that a table created via auto-sanitization
    correctly reports schema_matches=True when CSV has the corresponding
    messy headers.
    """

    _FORM = {
        "host": "localhost",
        "port": "9000",
        "user": "default",
        "password": "secret",
        "secure": "false",
        "database": "mydb",
        "table": "mytable",
    }

    def _post_analyze(self, client, csv_bytes):
        return client.post(
            "/admin/analyze",
            data=self._FORM,
            files={"file": ("data.csv", csv_bytes, "text/csv")},
            headers=AUTH,
        )

    def _make_client(self, existing_schema_rows, existing_comments_rows=None):
        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        side = [
            MagicMock(result_rows=[[1]]),   # database_exists
            MagicMock(result_rows=[[1]]),   # table_exists
            MagicMock(result_rows=existing_schema_rows),
        ]
        if existing_comments_rows is None:
            existing_comments_rows = [[row[0], ""] for row in existing_schema_rows]
        side.append(MagicMock(result_rows=existing_comments_rows))
        mock_ch.query.side_effect = side
        return mock_ch

    def test_messy_csv_headers_match_sanitized_table_schema(self):
        """A table created with sanitized names (order_date, total_usd) must
        report schema_matches=True when the CSV uses the original messy headers
        (Order Date, Total (USD)).
        """
        # Table has sanitized column names (as created by auto-ingest).
        mock_ch = self._make_client(
            existing_schema_rows=[
                ["order_date", "Date"],
                ["total_usd", "Float64"],
            ]
        )

        # CSV still has the original messy headers.
        csv_data = _csv_bytes(
            ["Order Date", "Total (USD)"],
            [["2024-01-01", "100.0"]],
        )

        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = self._post_analyze(client, csv_data)

        assert resp.status_code == 200
        body = resp.json()
        assert body["schema_matches"] is True
        assert body["schema_diff"]["missing_in_csv"] == []
        assert body["schema_diff"]["extra_in_csv"] == []
        assert body["schema_diff"]["type_mismatches"] == []

    def test_inferred_type_populated_by_sanitized_name(self):
        """inferred_columns and columns[i].inferred_type are keyed by sanitized name."""
        mock_ch = MagicMock()
        mock_ch.ping.return_value = True
        mock_ch.query.side_effect = [
            MagicMock(result_rows=[[1]]),  # db exists
            MagicMock(result_rows=[]),     # table not exists
        ]

        csv_data = _csv_bytes(
            ["Order Date", "Total (USD)"],
            [["2024-01-01", "99.5"]],
        )

        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = self._post_analyze(client, csv_data)

        assert resp.status_code == 200
        body = resp.json()

        # inferred_columns keyed by sanitized name.
        inferred_by_name = {c["name"]: c["suggested_type"] for c in body["inferred_columns"]}
        assert "order_date" in inferred_by_name
        assert inferred_by_name["order_date"] == "Date"
        assert "total_usd" in inferred_by_name
        assert inferred_by_name["total_usd"] == "Float64"

        # columns[i].inferred_type is also populated (via sanitized lookup).
        cols_by_name = {c["name"]: c for c in body["columns"]}
        assert cols_by_name["order_date"]["inferred_type"] == "Date"
        assert cols_by_name["total_usd"]["inferred_type"] == "Float64"

    def test_no_false_missing_or_extra_in_schema_diff(self):
        """When table and CSV have the same columns (raw vs sanitized),
        schema_diff must not report false missing_in_csv or extra_in_csv.
        """
        mock_ch = self._make_client(
            existing_schema_rows=[
                ["order_date", "Date"],
                ["total_usd", "Float64"],
                ["_2024_sales", "Int64"],
            ]
        )

        csv_data = _csv_bytes(
            ["Order Date", "Total (USD)", "2024 Sales"],
            [["2024-01-01", "100.0", "500"]],
        )

        from app.config import get_settings
        get_settings.cache_clear()
        singleton_mock = MagicMock()
        singleton_mock.ping.return_value = True
        with patch("app.admin_ingest.build_admin_client", return_value=mock_ch):
            with patch("app.clickhouse_client._cached_client", return_value=singleton_mock):
                from app.main import app
                client = TestClient(app, raise_server_exceptions=False)
                resp = self._post_analyze(client, csv_data)

        body = resp.json()
        assert body["schema_matches"] is True
        assert body["schema_diff"]["missing_in_csv"] == []
        assert body["schema_diff"]["extra_in_csv"] == []


# ---------------------------------------------------------------------------
# FIX 3 — append: messy-header CSV appends into sanitized-name table correctly
# ---------------------------------------------------------------------------


class TestAppendMessyHeaderIntoSanitizedTable:
    """Appending a CSV with messy headers into a table that was created via
    auto-sanitization must correctly map values to sanitized column names.
    """

    _BASE_FORM = {
        "host": "localhost",
        "port": "9000",
        "user": "default",
        "password": "secret",
        "secure": "false",
        "database": "mydb",
        "table": "mytable",
    }

    def test_messy_header_append_uses_sanitized_column_names_in_insert(self):
        """Insert call must use sanitized column names, not raw messy header names."""
        mock_ch = MagicMock()

        def query_side_effect(sql, parameters=None):
            if "system.tables" in sql:
                return MagicMock(result_rows=[[1]])  # table exists
            if "system.columns" in sql:
                # Table was created with sanitized column names.
                return MagicMock(result_rows=[
                    ["order_date", "Date"],
                    ["total_usd", "Float64"],
                ])
            if sql.startswith("SELECT max("):
                return MagicMock(result_rows=[[None]])  # empty table → insert all
            return MagicMock(result_rows=[])

        mock_ch.query.side_effect = query_side_effect
        mock_ch.insert.return_value = None

        # CSV has the original messy headers.
        csv_data = _csv_bytes(
            ["Order Date", "Total (USD)"],
            [["2024-01-01", "99.5"]],
        )

        def run(client):
            return client.post(
                "/admin/ingest",
                data={**self._BASE_FORM, "mode": "append", "timestamp_column": "order_date"},
                files={"file": ("data.csv", csv_data, "text/csv")},
                headers=AUTH,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows_inserted"] == 1
        assert body["rows_skipped"] == 0

        # The insert must use the table's sanitized column names.
        assert mock_ch.insert.called
        insert_col_names = mock_ch.insert.call_args.kwargs["column_names"]
        assert insert_col_names == ["order_date", "total_usd"]

    def test_messy_header_append_watermark_filter_works(self):
        """Watermark filter must still work when CSV has messy headers."""
        mock_ch = MagicMock()

        from datetime import date as _date

        def query_side_effect(sql, parameters=None):
            if "system.tables" in sql:
                return MagicMock(result_rows=[[1]])
            if "system.columns" in sql:
                return MagicMock(result_rows=[
                    ["order_date", "Date"],
                    ["amount", "Float64"],
                ])
            if sql.startswith("SELECT max("):
                return MagicMock(result_rows=[[_date(2024, 1, 15)]])
            return MagicMock(result_rows=[])

        mock_ch.query.side_effect = query_side_effect
        mock_ch.insert.return_value = None

        # CSV has messy headers; two rows, one before watermark, one after.
        csv_data = _csv_bytes(
            ["Order Date", "Amount"],
            [
                ["2024-01-10", "50.0"],   # before watermark → skip
                ["2024-01-20", "75.0"],   # after watermark → insert
            ],
        )

        def run(client):
            return client.post(
                "/admin/ingest",
                data={**self._BASE_FORM, "mode": "append", "timestamp_column": "order_date"},
                files={"file": ("data.csv", csv_data, "text/csv")},
                headers=AUTH,
            )

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows_inserted"] == 1
        assert body["rows_skipped"] == 1

        # Column names in insert must be sanitized.
        assert mock_ch.insert.called
        insert_col_names = mock_ch.insert.call_args.kwargs["column_names"]
        assert insert_col_names == ["order_date", "amount"]


# ===========================================================================
# Admin API-key auth contract — /admin uses the static key, NOT a tenant JWT
# ===========================================================================

class TestAdminApiKeyAuth:
    """The /admin write routes authenticate with the static admin API key.

    A per-tenant JWT (valid for the query/schema routes) must NOT authenticate
    here — admin is a separate operational credential.
    """

    def _client(self):
        from app.main import app

        return TestClient(app, raise_server_exceptions=False)

    def test_wrong_admin_key_returns_401(self):
        resp = self._client().post(
            "/admin/analyze", headers={"Authorization": "Bearer wrong-key"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "INVALID_AUTH"

    def test_tenant_jwt_not_accepted_on_admin(self):
        from tests.jwt_helpers import make_jwt

        resp = self._client().post(
            "/admin/analyze", headers={"Authorization": f"Bearer {make_jwt()}"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "INVALID_AUTH"

    def test_valid_admin_key_passes_auth_layer(self):
        # Valid key clears auth; the missing form body then yields 422 — proving
        # the request got past the auth dependency (not 401/403).
        resp = self._client().post("/admin/analyze", headers=AUTH)
        assert resp.status_code not in (401, 403)

    def test_missing_auth_returns_401(self):
        resp = self._client().post("/admin/analyze")
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == "MISSING_AUTH"
