"""Tests for column-scope enforcement in app/service.py::sample_rows (D83).

sampleRows had NO column-scope check before this change (only runQuery did,
D57) — this closes that gap by routing sampleRows' effective `SELECT *
FROM db.table LIMIT n` through the exact same extract_column_provenance +
scope-allowlist path run_query uses (see tests/test_scope_enforcement.py for
the analogous run_query test suite this mirrors).

D76 Layer-1 unit tests — no live ClickHouse required. execute_query and
get_catalog_schema are mocked so the entire enforcement path runs in-process.

Test IDs:
  D83-sample-rows-out-of-scope-rejected
  D83-sample-rows-full-scope-executes
  D83-sample-rows-empty-scope-allow-all
  D83-sample-rows-uncatalogued-table-fail-closed
  D83-sample-rows-stdio-no-enforcement
  D83-sample-rows-scratch-session-violation
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

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

from app import service  # noqa: E402
from app.errors import ColumnScopeError, ParseFailedError  # noqa: E402

_CATALOG = {
    "analytics.orders": {
        "order_id": "UInt64",
        "customer_id": "UInt64",
        "gross_pay": "Decimal(18,6)",
        "status": "String",
    },
}

_FULL_SCOPE: frozenset[str] = frozenset(
    f"analytics.orders.{col}" for col in _CATALOG["analytics.orders"]
)

# Excludes gross_pay.
_RESTRICTED_SCOPE: frozenset[str] = frozenset(
    {
        "analytics.orders.order_id",
        "analytics.orders.customer_id",
        "analytics.orders.status",
    }
)


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    from app.catalog import invalidate_catalog_cache

    invalidate_catalog_cache()
    yield
    invalidate_catalog_cache()


@pytest.fixture()
def mock_execute():
    with patch("app.service.execute_query") as m:
        m.return_value = (["order_id"], [["1"]])
        yield m


@pytest.fixture()
def mock_catalog():
    with patch("app.service.get_catalog_schema") as m:
        m.return_value = _CATALOG
        yield m


def _sample_with_scope(database, table, scope, session_id=None, limit=5):
    from app.principal import current_scope, current_session_id

    scope_token = current_scope.set(scope)
    session_token = current_session_id.set(session_id)
    try:
        return service.sample_rows(database, table, limit=limit)
    finally:
        current_scope.reset(scope_token)
        current_session_id.reset(session_token)


class TestD83SampleRowsOutOfScopeRejected:
    def test_D83_sample_rows_out_of_scope_rejected(self, mock_execute, mock_catalog):
        with pytest.raises(ColumnScopeError) as exc_info:
            _sample_with_scope("analytics", "orders", _RESTRICTED_SCOPE, session_id="sess-001")
        assert exc_info.value.code == "COLUMN_SCOPE_VIOLATION"
        # execute_query must NOT have been called — reject, never partially project.
        mock_execute.assert_not_called()


class TestD83SampleRowsFullScopeExecutes:
    def test_D83_sample_rows_full_scope_executes(self, mock_execute, mock_catalog):
        result = _sample_with_scope("analytics", "orders", _FULL_SCOPE, session_id="sess-001")
        assert result is not None
        mock_execute.assert_called_once()
        sql_arg = mock_execute.call_args[0][0]
        assert "SELECT *" in sql_arg
        assert "LIMIT 5" in sql_arg


class TestD83SampleRowsEmptyScopeAllowAll:
    def test_D83_sample_rows_empty_scope_executes(self, mock_execute, mock_catalog):
        """Empty frozenset scope == ALLOW-ALL (D80b) — must execute even though the
        table has a column (gross_pay) that would be forbidden under a restricted
        scope."""
        result = _sample_with_scope("analytics", "orders", frozenset(), session_id="sess-001")
        assert result is not None
        mock_execute.assert_called_once()


class TestD83SampleRowsStdioNoEnforcement:
    def test_D83_sample_rows_stdio_scope_none_skips_enforcement(self, mock_execute, mock_catalog):
        result = _sample_with_scope("analytics", "orders", None, session_id=None)
        assert result is not None
        mock_execute.assert_called_once()
        # get_catalog_schema must NOT have been called (no scope -> no enforcement)
        mock_catalog.assert_not_called()


class TestD83SampleRowsUncataloguedTableFailClosed:
    def test_D83_sample_rows_uncatalogued_table_rejected(self, mock_execute, mock_catalog):
        """Table not in the (interim) provenance catalog -> SELECT * cannot be
        expanded -> ProvenanceExtractionError -> ParseFailedError (fail-closed,
        matches run_query's SELECT * treatment, D69/OQ-2)."""
        with pytest.raises(ParseFailedError) as exc_info:
            _sample_with_scope(
                "analytics", "uncatalogued_table", _FULL_SCOPE, session_id="sess-001"
            )
        assert exc_info.value.code == "PARSE_FAILED_CLOSED"
        mock_execute.assert_not_called()


class TestD83SampleRowsScratchSessionViolation:
    def test_D83_sample_rows_cross_session_scratch_rejected(self, mock_execute, mock_catalog):
        with pytest.raises(ColumnScopeError) as exc_info:
            _sample_with_scope(
                "scratch", "s_other-session_tmp", _FULL_SCOPE, session_id="sess-001"
            )
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()
