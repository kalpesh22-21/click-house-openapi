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
# Auth constants — must match conftest.py _TEST_ENV["API_KEY"]
# ---------------------------------------------------------------------------

API_KEY = "test-api-key-abc123"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


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

    def test_create_mode_requires_columns(self):
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])

        def run(client):
            return self._post_ingest(client, {"mode": "create"})

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400
        assert "columns" in resp.json()["detail"]

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


def _build_mock_analyze_client(db_exists, tbl_exists, existing_schema=None):
    """Return a mock client configured for analyze() calls."""
    mock_ch = MagicMock()
    mock_ch.ping.return_value = True
    side = [
        MagicMock(result_rows=[[1]] if db_exists else []),
        MagicMock(result_rows=[[1]] if tbl_exists else []),
    ]
    if tbl_exists and existing_schema is not None:
        side.append(MagicMock(result_rows=existing_schema))
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

    def test_create_mode_issues_create_database_before_create_table(self):
        """CREATE DATABASE IF NOT EXISTS must precede CREATE TABLE."""
        mock_ch = MagicMock()
        mock_ch.query.return_value = MagicMock(result_rows=[])  # table not exists
        mock_ch.command.return_value = None
        mock_ch.insert.return_value = None

        cols = json.dumps([{"name": "id", "type": "Int64"}])

        def run(client):
            return self._post_ingest(client, {"mode": "create", "columns": cols})

        _run_with_mocked_ingest(mock_ch, run)
        call_args = [c.args[0] for c in mock_ch.command.call_args_list]
        create_db_idx = next(
            (i for i, c in enumerate(call_args) if "CREATE DATABASE IF NOT EXISTS" in c), None
        )
        create_tbl_idx = next(
            (i for i, c in enumerate(call_args) if "CREATE TABLE" in c), None
        )
        assert create_db_idx is not None, "CREATE DATABASE must be called"
        assert create_tbl_idx is not None, "CREATE TABLE must be called"
        assert create_db_idx < create_tbl_idx, "CREATE DATABASE must precede CREATE TABLE"

    def test_wipe_mode_requires_columns(self):
        """wipe mode without columns JSON must return 400."""
        mock_ch = MagicMock()

        def run(client):
            return self._post_ingest(client, {"mode": "wipe"})

        resp = _run_with_mocked_ingest(mock_ch, run)
        assert resp.status_code == 400
        assert "columns" in resp.json()["detail"]

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
