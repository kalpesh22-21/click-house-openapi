"""Tests for app.ch_errors.rewrite_clickhouse_error.

Fixture strings are verbatim str(DatabaseError) values captured from
clickhouse-connect against ClickHouse 24.8.14, so the parser is exercised on
the real shape (driver prefix, "In query" echo, version + URL suffix).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError
from fastapi import HTTPException

from app.ch_errors import rewrite_clickhouse_error as rw

_SUFFIX = " (version 24.8.14.39 (official build)) (for url http://localhost:8123)"

NOT_AN_AGGREGATE = (
    "Received ClickHouse exception, code: 215, server response: Code: 215. "
    "DB::Exception: Column dbpcm_warehouse.employee.hire_date is not under aggregate "
    "function and not in GROUP BY keys. In query SELECT formatDateTime(hire_date, '%Y-%m') "
    "AS hire_month, count(*) AS hired FROM dbpcm_warehouse.employee WHERE (employee_status "
    "!= 'Not Hired') GROUP BY toStartOfMonth(hire_date) ORDER BY toStartOfMonth(hire_date) "
    "ASC LIMIT 1000. (NOT_AN_AGGREGATE)" + _SUFFIX
)
UNKNOWN_IDENTIFIER = (
    "Received ClickHouse exception, code: 47, server response: Code: 47. DB::Exception: "
    "Unknown expression identifier 'hire_dat' in scope SELECT hire_dat FROM "
    "dbpcm_warehouse.employee. Maybe you meant: ['hire_date']. (UNKNOWN_IDENTIFIER)" + _SUFFIX
)
UNKNOWN_FUNCTION = (
    "Received ClickHouse exception, code: 46, server response: Code: 46. DB::Exception: "
    "Function with name 'DATE_FORMATX' does not exist. In scope SELECT DATE_FORMATX(hire_date, "
    "'%Y') FROM dbpcm_warehouse.employee. Maybe you meant: ['DATE_FORMAT']. (UNKNOWN_FUNCTION)"
    + _SUFFIX
)
SYNTAX_TRUNCATED = (
    "Received ClickHouse exception, code: 62, server response: Code: 62. DB::Exception: "
    "Syntax error: failed at position 89 ('Native') (line 2, col 9): Native. Expected one of: "
    "token sequence, Dot, token, OR, AND, IS NOT DISTINCT FROM, IS NULL, IS NOT NULL, BETWEEN, "
    "NOT BETWEEN, LIKE, ILIKE, NOT LIKE, NOT ILIKE, REGEXP, IN, NOT IN, GLOBAL IN, GLOBAL NOT IN, "
    "MOD, DIV, alias, AS, GROUP BY, WITH, HAVING, WINDOW, QUALIFY, ORDER BY, LIMIT, OFFSET, FETCH, "
    "SETTINGS, UNION, EXCEPT, INTERSECT, INTO OUTFILE, FORMAT, end of query. (SYNTAX_ERROR)"
    + _SUFFIX
)
SYNTAX_UNMATCHED = (
    "Received ClickHouse exception, code: 62, server response: Code: 62. DB::Exception: "
    "Syntax error: failed at position 52 ('(') (line 1, col 52): (hire_date > '2024-01-01'\n "
    "FORMAT Native. Unmatched parentheses: (. (SYNTAX_ERROR)" + _SUFFIX
)
UNKNOWN_TABLE = (
    "Received ClickHouse exception, code: 60, server response: Code: 60. DB::Exception: "
    "Unknown table expression identifier 'dbpcm_warehouse.nope' in scope SELECT * FROM "
    "dbpcm_warehouse.nope. (UNKNOWN_TABLE)" + _SUFFIX
)
UNLISTED_CODE = (
    "Received ClickHouse exception, code: 999, server response: Code: 999. DB::Exception: "
    "Something odd happened. (SOME_NEW_ERROR)" + _SUFFIX
)


class TestNoiseRemoval:
    @pytest.mark.parametrize(
        "raw",
        [NOT_AN_AGGREGATE, UNKNOWN_IDENTIFIER, UNKNOWN_FUNCTION, SYNTAX_TRUNCATED,
         SYNTAX_UNMATCHED, UNKNOWN_TABLE, UNLISTED_CODE],
    )
    def test_strips_version_url_and_driver_prefix(self, raw):
        out = rw(raw)
        assert "version 24.8" not in out
        assert "for url" not in out
        assert "localhost:8123" not in out
        assert "Received ClickHouse exception" not in out
        assert "server response" not in out

    def test_drops_echoed_query_after_in_query(self):
        out = rw(NOT_AN_AGGREGATE)
        assert "In query" not in out
        assert "LIMIT 1000" not in out
        assert "is not under aggregate function and not in GROUP BY keys" in out

    def test_drops_in_scope_echo_but_keeps_suggestion(self):
        out = rw(UNKNOWN_IDENTIFIER)
        assert "in scope" not in out
        assert "FROM dbpcm_warehouse.employee" not in out
        assert "Maybe you meant: ['hire_date']" in out

    def test_in_scope_echo_is_case_insensitive(self):
        out = rw(UNKNOWN_FUNCTION)
        assert "In scope" not in out
        assert "Maybe you meant: ['DATE_FORMAT']" in out

    def test_shorter_than_raw(self):
        assert len(rw(NOT_AN_AGGREGATE)) < len(NOT_AN_AGGREGATE)


class TestLabelAndHints:
    def test_not_an_aggregate_names_column_and_alias_fix(self):
        out = rw(NOT_AN_AGGREGATE)
        assert out.startswith("NOT_AN_AGGREGATE (code 215):")
        assert "Hint:" in out
        assert "'hire_date' appears in SELECT" in out
        assert "GROUP BY the SELECT alias" in out

    def test_unknown_identifier_points_at_get_table_schema(self):
        out = rw(UNKNOWN_IDENTIFIER)
        assert out.startswith("UNKNOWN_IDENTIFIER (code 47):")
        assert "getTableSchema" in out

    def test_unknown_table_points_at_list_tables(self):
        out = rw(UNKNOWN_TABLE)
        assert "listTables" in out
        assert "'dbpcm_warehouse.nope'" in out

    def test_syntax_error_at_driver_format_suffix_is_explained(self):
        out = rw(SYNTAX_TRUNCATED)
        assert "'Native'" not in out
        assert "line 2, col 9" not in out
        assert "ended before it was complete" in out
        # Expected-token list is truncated, not dropped.
        assert "Expected one of: token sequence, Dot, token, OR, AND, IS NOT DISTINCT FROM (and more)" in out
        assert "INTO OUTFILE" not in out

    def test_syntax_error_strips_appended_format_native(self):
        out = rw(SYNTAX_UNMATCHED)
        assert "FORMAT Native" not in out
        assert "Unmatched parentheses: (" in out
        assert "position 52" in out

    def test_unlisted_code_passes_through_cleaned_without_hint(self):
        out = rw(UNLISTED_CODE)
        assert out == "SOME_NEW_ERROR (code 999): Something odd happened."
        assert "Hint:" not in out

    def test_unparseable_input_is_returned_trimmed(self):
        assert rw("totally unexpected text (for url http://h:8123)") == "totally unexpected text"
        assert rw("") == "unknown ClickHouse error"

    def test_body_is_capped(self):
        raw = "Code: 62. DB::Exception: " + "x" * 2000 + " (SYNTAX_ERROR)"
        out = rw(raw)
        assert len(out) < 800


class TestWiring:
    """The rewrite reaches the caller through execute_query and the MCP tool."""

    def test_execute_query_forwards_rewritten_message(self):
        from app import clickhouse_client as cc
        from app.config import get_settings

        get_settings.cache_clear()
        client = MagicMock()
        client.query.side_effect = DatabaseError(NOT_AN_AGGREGATE)
        with patch.object(cc, "get_client", return_value=client):
            with pytest.raises(HTTPException) as exc:
                cc.execute_query("SELECT 1", get_settings())
        err = exc.value.detail["error"]
        assert exc.value.detail["code"] == "CLICKHOUSE_QUERY_ERROR"
        assert err.startswith("ClickHouse query error: NOT_AN_AGGREGATE (code 215):")
        assert "Hint:" in err
        assert "In query" not in err
        assert "localhost:8123" not in err

    def test_mcp_run_query_surfaces_hint_and_not_explain_advice(self):
        from mcp.server.fastmcp.exceptions import ToolError

        from app import service
        from app.mcp_server import run_query as mcp_run_query

        with patch.object(service, "execute_query") as mock_exec:
            mock_exec.side_effect = HTTPException(
                status_code=400,
                detail={"error": "ClickHouse query error: " + rw(NOT_AN_AGGREGATE),
                        "code": "CLICKHOUSE_QUERY_ERROR"},
            )
            with pytest.raises(ToolError) as exc:
                mcp_run_query(sql="SELECT 1")
        msg = str(exc.value)
        assert "[CLICKHOUSE_QUERY_ERROR]" in msg
        assert "Hint:" in msg
        assert "explainQuery" not in msg
        assert "Apply the hint, then retry." in msg

    def test_mcp_unlisted_code_gets_generic_retry_suffix(self):
        from mcp.server.fastmcp.exceptions import ToolError

        from app import service
        from app.mcp_server import run_query as mcp_run_query

        with patch.object(service, "execute_query") as mock_exec:
            mock_exec.side_effect = HTTPException(
                status_code=400,
                detail={"error": "ClickHouse query error: " + rw(UNLISTED_CODE),
                        "code": "CLICKHOUSE_QUERY_ERROR"},
            )
            with pytest.raises(ToolError) as exc:
                mcp_run_query(sql="SELECT 1")
        msg = str(exc.value)
        assert "Hint:" not in msg
        assert "Fix the SQL, then retry." in msg
