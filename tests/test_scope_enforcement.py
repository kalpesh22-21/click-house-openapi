"""Tests for column-scope enforcement in app/service.py::run_query.

D76 Layer-1 unit tests — no live ClickHouse required.  execute_query and
get_catalog_schema are mocked so the entire enforcement path runs in-process.

Test IDs (used as function names):
  D57-in-scope-query-executes
  D57-out-of-scope-column-rejected
  D57-derived-agg-over-forbidden-rejected
  D63-parse-fail-rejects-not-runs
  D57-stdio-no-scope-enforcement
  D57-select-star-forbidden-column-rejected
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure env vars are set before any app module loads (matches conftest pattern)
# ---------------------------------------------------------------------------
_BASE_ENV = {
    "CLICKHOUSE_HOST": "localhost",
    "CLICKHOUSE_PORT": "9000",
    "CLICKHOUSE_USER": "default",
    "CLICKHOUSE_PASSWORD": "test-password",
    "CLICKHOUSE_DATABASE": "default",
    "CLICKHOUSE_SECURE": "false",
    "API_KEY": "test-api-key-abc123",
    "MAX_EXECUTION_TIME": "30",
    "MAX_RESULT_ROWS": "10000",
    "MAX_ROWS_TO_READ": "100000000",
    "DEFAULT_LIMIT": "1000",
    "MAX_RESPONSE_ROWS": "1000",
    "ALLOWED_DATABASES": "*",
    "APP_PORT": "8000",
    "LOG_LEVEL": "INFO",
    "PUBLIC_BASE_URL": "https://test.example.com",
}
for _k, _v in _BASE_ENV.items():
    os.environ.setdefault(_k, _v)

# ---------------------------------------------------------------------------
# App imports (after env is set)
# ---------------------------------------------------------------------------

from app import service  # noqa: E402
from app.errors import ColumnScopeError, ParseFailedError  # noqa: E402

# ---------------------------------------------------------------------------
# Test catalog — mirrors the payroll + employee schema used by provenance tests.
# Kept small to make expected USES sets easy to reason about.
# ---------------------------------------------------------------------------

_CATALOG = {
    "analytics.orders": {
        "order_id": "UInt64",
        "customer_id": "UInt64",
        "gross_pay": "Decimal(18,6)",
        "status": "String",
        "created_at": "DateTime",
    },
    "analytics.customers": {
        "customer_id": "UInt64",
        "name": "String",
        "email": "String",
    },
}

# Full scope: every column in both tables
_FULL_SCOPE: frozenset[str] = frozenset(
    f"{tbl}.{col}"
    for tbl, cols in _CATALOG.items()
    for col in cols
)

# Restricted scope: only analytics.orders.order_id and analytics.customers.name
_RESTRICTED_SCOPE: frozenset[str] = frozenset([
    "analytics.orders.order_id",
    "analytics.orders.status",
    "analytics.customers.customer_id",
    "analytics.customers.name",
])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    """Invalidate the catalog cache between tests."""
    from app.catalog import invalidate_catalog_cache
    invalidate_catalog_cache()
    yield
    invalidate_catalog_cache()


@pytest.fixture()
def mock_execute():
    """Patch execute_query inside app.service."""
    with patch("app.service.execute_query") as m:
        m.return_value = (["order_id"], [["1"]])
        yield m


@pytest.fixture()
def mock_catalog():
    """Patch get_catalog_schema inside app.service to return _CATALOG."""
    with patch("app.service.get_catalog_schema") as m:
        m.return_value = _CATALOG
        yield m


# ---------------------------------------------------------------------------
# Helper: set scope/session_id context vars for the duration of a test
# ---------------------------------------------------------------------------

def _run_with_scope(sql, scope, session_id=None, limit=None):
    """Call service.run_query with the given scope/session_id injected via context vars."""
    from app.principal import current_scope, current_session_id
    scope_token = current_scope.set(scope)
    session_token = current_session_id.set(session_id)
    try:
        return service.run_query(sql, limit=limit)
    finally:
        current_scope.reset(scope_token)
        current_session_id.reset(session_token)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestD57InScopeQueryExecutes:
    """D57-in-scope-query-executes — query uses only in-scope columns, must execute."""

    def test_D57_in_scope_query_executes(self, mock_execute, mock_catalog):
        sql = "SELECT order_id, status FROM analytics.orders WHERE status = 'active'"
        result = _run_with_scope(sql, _FULL_SCOPE, session_id="sess-001")
        assert result["columns"] == ["order_id"]
        mock_execute.assert_called_once()

    def test_D57_in_scope_restricted_scope_executes(self, mock_execute, mock_catalog):
        """Only order_id and status are in scope — query uses exactly those."""
        sql = "SELECT order_id, status FROM analytics.orders"
        result = _run_with_scope(sql, _RESTRICTED_SCOPE, session_id="sess-001")
        assert result is not None
        mock_execute.assert_called_once()


class TestD57OutOfScopeColumnRejected:
    """D57-out-of-scope-column-rejected — query references column not in scope."""

    def test_D57_out_of_scope_column_rejected(self, mock_execute, mock_catalog):
        # gross_pay is NOT in _RESTRICTED_SCOPE
        sql = "SELECT order_id, gross_pay FROM analytics.orders"
        with pytest.raises(ColumnScopeError) as exc_info:
            _run_with_scope(sql, _RESTRICTED_SCOPE, session_id="sess-001")
        assert exc_info.value.code == "COLUMN_SCOPE_VIOLATION"
        # execute_query must NOT have been called
        mock_execute.assert_not_called()

    def test_D57_email_out_of_scope_rejected(self, mock_execute, mock_catalog):
        """email is not in _RESTRICTED_SCOPE."""
        sql = "SELECT customer_id, name, email FROM analytics.customers"
        with pytest.raises(ColumnScopeError) as exc_info:
            _run_with_scope(sql, _RESTRICTED_SCOPE, session_id="sess-001")
        assert exc_info.value.code == "COLUMN_SCOPE_VIOLATION"
        mock_execute.assert_not_called()


class TestD57DerivedAggOverForbiddenRejected:
    """D57-derived-agg-over-forbidden-rejected — AVG(gross_pay) must be blocked even though
    the aggregate result itself is not a raw column value."""

    def test_D57_derived_agg_over_forbidden_rejected(self, mock_execute, mock_catalog):
        # gross_pay not in restricted scope; AVG(gross_pay) must still be blocked
        sql = (
            "SELECT customer_id, AVG(gross_pay) AS avg_pay "
            "FROM analytics.orders "
            "GROUP BY customer_id"
        )
        with pytest.raises(ColumnScopeError) as exc_info:
            _run_with_scope(sql, _RESTRICTED_SCOPE, session_id="sess-001")
        assert exc_info.value.code == "COLUMN_SCOPE_VIOLATION"
        mock_execute.assert_not_called()

    def test_D57_sum_forbidden_col_rejected(self, mock_execute, mock_catalog):
        sql = "SELECT SUM(gross_pay) AS total FROM analytics.orders"
        with pytest.raises(ColumnScopeError) as exc_info:
            _run_with_scope(sql, _RESTRICTED_SCOPE, session_id="sess-001")
        assert exc_info.value.code == "COLUMN_SCOPE_VIOLATION"
        mock_execute.assert_not_called()


class TestD63ParseFailRejectsNotRuns:
    """D63-parse-fail-rejects-not-runs — unparseable SQL raises ParseFailedError,
    execute_query is never called."""

    def test_D63_parse_fail_rejects_not_runs(self, mock_execute, mock_catalog):
        # generateRandom is a table-valued function that causes provenance extraction
        # to fail with ProvenanceExtractionError (fail-closed, D63).
        sql = "SELECT * FROM generateRandom('a UInt8, b Float32', 1, 10, 2) LIMIT 5"
        with pytest.raises(ParseFailedError) as exc_info:
            _run_with_scope(sql, _FULL_SCOPE, session_id="sess-001")
        assert exc_info.value.code == "PARSE_FAILED_CLOSED"
        mock_execute.assert_not_called()

    def test_D63_ddl_parse_fail_rejects(self, mock_execute, mock_catalog):
        # DDL (CREATE TABLE) is rejected by the provenance extractor (fail-closed).
        # Note: validate_and_sanitize would catch this first (DISALLOWED_STATEMENT_TYPE),
        # so this is actually blocked by the earlier guardrail before provenance extraction.
        # We test using INSERT which also fails at the guardrail, but to specifically test
        # the D63 path we need SQL that passes the guardrail but fails provenance.
        # generateRandom passes the guardrail (it's a SELECT) but fails provenance.
        sql = "SELECT * FROM generateRandom('x UInt32', 42, 10, 2)"
        with pytest.raises(ParseFailedError) as exc_info:
            _run_with_scope(sql, _FULL_SCOPE, session_id="sess-001")
        assert exc_info.value.code == "PARSE_FAILED_CLOSED"
        mock_execute.assert_not_called()

    def test_D63_uncatalogued_table_fails_closed(self, mock_execute, mock_catalog):
        """Table not in catalog -> ProvenanceExtractionError -> ParseFailedError."""
        sql = "SELECT x FROM external_db.secret_table"
        with pytest.raises(ParseFailedError) as exc_info:
            _run_with_scope(sql, _FULL_SCOPE, session_id="sess-001")
        assert exc_info.value.code == "PARSE_FAILED_CLOSED"
        mock_execute.assert_not_called()


class TestD57StdioNoScopeEnforcement:
    """D57-stdio-no-scope-enforcement — when scope is None (stdio/local-trust),
    enforcement is skipped and execution proceeds normally."""

    def test_D57_stdio_no_scope_enforcement(self, mock_execute, mock_catalog):
        # scope=None simulates the stdio transport (no JWT → no scope).
        sql = "SELECT order_id, gross_pay FROM analytics.orders"
        # With scope=None, even a query that would be out-of-scope in JWT mode
        # must execute without raising ColumnScopeError.
        from app.principal import current_scope, current_session_id
        scope_token = current_scope.set(None)
        session_token = current_session_id.set(None)
        try:
            result = service.run_query(sql)
        finally:
            current_scope.reset(scope_token)
            current_session_id.reset(session_token)
        assert result is not None
        mock_execute.assert_called_once()
        # get_catalog_schema must NOT have been called (no scope → no enforcement)
        mock_catalog.assert_not_called()

    def test_D57_default_context_is_none_scope(self, mock_execute, mock_catalog):
        """Default ContextVar value is None — enforce nothing in the default context."""
        # No context var tokens set; default is None per ContextVar definition.
        sql = "SELECT order_id FROM analytics.orders"
        result = service.run_query(sql)
        assert result is not None
        mock_execute.assert_called_once()
        mock_catalog.assert_not_called()


class TestD57SelectStarForbiddenColumnRejected:
    """D57-select-star-forbidden-column-rejected — SELECT * expands to all columns;
    if any are forbidden the query must be rejected."""

    def test_D57_select_star_forbidden_column_rejected(self, mock_execute, mock_catalog):
        # SELECT * on analytics.orders expands to all 5 columns.
        # _RESTRICTED_SCOPE does not include gross_pay or customer_id (for orders),
        # and does not include email (for customers).
        # With SELECT *, all columns are in the USES set, but gross_pay is not in scope.
        sql = "SELECT * FROM analytics.orders"
        with pytest.raises(ColumnScopeError) as exc_info:
            _run_with_scope(sql, _RESTRICTED_SCOPE, session_id="sess-001")
        assert exc_info.value.code == "COLUMN_SCOPE_VIOLATION"
        mock_execute.assert_not_called()

    def test_D57_select_star_full_scope_executes(self, mock_execute, mock_catalog):
        """SELECT * with full scope must execute — all columns are in scope."""
        sql = "SELECT * FROM analytics.orders"
        result = _run_with_scope(sql, _FULL_SCOPE, session_id="sess-001")
        assert result is not None
        mock_execute.assert_called_once()


class TestD57EmptyScopeAllowsAllColumns:
    """Empty frozenset scope == ALLOW-ALL: warehouse queries must execute even when
    they reference columns that would be forbidden under a non-empty restricted scope.
    D63 (parse-fail-closed) and D64 (scratch isolation) still apply."""

    def test_D57_empty_scope_allows_all_columns(self, mock_execute, mock_catalog):
        """Empty frozenset + multi-column warehouse query → executes (no ColumnScopeError)."""
        sql = (
            "SELECT order_id, customer_id, gross_pay, status, created_at "
            "FROM analytics.orders"
        )
        result = _run_with_scope(sql, frozenset(), session_id="sess-001")
        assert result is not None
        mock_execute.assert_called_once()

    def test_D57_empty_scope_join_query_executes(self, mock_execute, mock_catalog):
        """Empty scope + join across both tables (all columns) → executes."""
        sql = (
            "SELECT o.order_id, o.gross_pay, c.name, c.email "
            "FROM analytics.orders AS o "
            "JOIN analytics.customers AS c ON o.customer_id = c.customer_id"
        )
        result = _run_with_scope(sql, frozenset(), session_id="sess-001")
        assert result is not None
        mock_execute.assert_called_once()

    def test_D63_parse_fail_with_empty_scope(self, mock_execute, mock_catalog):
        """Unparseable SQL with empty scope is still fail-closed (D63)."""
        sql = "SELECT * FROM generateRandom('a UInt8, b Float32', 1, 10, 2) LIMIT 5"
        from app.errors import ParseFailedError
        with pytest.raises(ParseFailedError) as exc_info:
            _run_with_scope(sql, frozenset(), session_id="sess-001")
        assert exc_info.value.code == "PARSE_FAILED_CLOSED"
        mock_execute.assert_not_called()
