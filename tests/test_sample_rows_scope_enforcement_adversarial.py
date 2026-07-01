"""ADVERSARIAL negative tests for app.service.sample_rows column-scope enforcement (D83).

Complements tests/test_sample_rows_scope_enforcement.py (which covers the
labeled D83 scenarios: out-of-scope rejected, full scope executes, empty/None
scope allow-all, uncatalogued fail-closed, scratch-session violation) with:

  - Exact scope boundary: missing exactly ONE column out of many -> rejected.
  - Superset scope (table's columns + unrelated extra columns) -> still
    executes (forbidden-set computation must not falsely trip on extra scope
    entries).
  - D70 case-sensitivity: a scope entry for a required column in the wrong
    case does NOT satisfy that column's requirement -> rejected, same as if
    the column weren't listed at all.
  - Data-leak-surface assertions: on every reject path, execute_query is
    never called (data never touched) AND the raised error's message does not
    contain the forbidden column names verbatim (run_query/sample_rows only
    log a *count*, per the "column names are a secret" comment in
    app/service.py -- this test locks that in as a regression guard for
    sample_rows specifically, since it's easy for a future edit to sample_rows
    to accidentally interpolate column names into the exception message the
    way a naive implementation might).

Test IDs:
  D83-adv-sample-rows-boundary-single-column-missing
  D83-adv-sample-rows-scope-superset-still-executes
  D83-adv-sample-rows-case-sensitive-scope-mismatch-rejected
  D83-adv-sample-rows-reject-message-does-not-leak-column-names
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
from app.errors import ColumnScopeError  # noqa: E402

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

# Missing exactly one column: gross_pay.
_MISSING_ONE_SCOPE: frozenset[str] = frozenset(
    {
        "analytics.orders.order_id",
        "analytics.orders.customer_id",
        "analytics.orders.status",
    }
)

# All of the table's own columns PLUS unrelated columns from a different table
# -- forbidden-set computation is a set-difference; extraneous scope entries
# must never accidentally satisfy (or break) it.
_SUPERSET_SCOPE: frozenset[str] = _FULL_SCOPE | frozenset(
    {"analytics.other_table.unrelated_col", "warehouse.employee.EmployeeCode"}
)

# gross_pay listed with the WRONG case ("Gross_Pay") -- must not satisfy the
# real "gross_pay" requirement (D70: no case-insensitive fallback).
_WRONG_CASE_SCOPE: frozenset[str] = frozenset(
    {
        "analytics.orders.order_id",
        "analytics.orders.customer_id",
        "analytics.orders.status",
        "analytics.orders.Gross_Pay",
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


class TestD83AdvSampleRowsBoundarySingleColumnMissing:
    def test_D83_adv_sample_rows_boundary_single_column_missing_rejected(
        self, mock_execute, mock_catalog
    ):
        """Only ONE column (gross_pay) out of four is out of scope -- must
        still reject wholesale (sampleRows never partially projects)."""
        with pytest.raises(ColumnScopeError) as exc_info:
            _sample_with_scope(
                "analytics", "orders", _MISSING_ONE_SCOPE, session_id="sess-001"
            )
        assert exc_info.value.code == "COLUMN_SCOPE_VIOLATION"
        mock_execute.assert_not_called()


class TestD83AdvSampleRowsScopeSupersetStillExecutes:
    def test_D83_adv_sample_rows_scope_superset_still_executes(
        self, mock_execute, mock_catalog
    ):
        """Scope contains every one of the table's columns PLUS unrelated
        entries from other tables -- the extra entries must not confuse the
        forbidden-set computation into rejecting a fully-in-scope table."""
        result = _sample_with_scope(
            "analytics", "orders", _SUPERSET_SCOPE, session_id="sess-001"
        )
        assert result is not None
        mock_execute.assert_called_once()


class TestD83AdvSampleRowsCaseSensitiveScopeMismatchRejected:
    def test_D83_adv_sample_rows_case_sensitive_scope_mismatch_rejected(
        self, mock_execute, mock_catalog
    ):
        """Scope lists 'Gross_Pay' (wrong case) instead of the real
        'gross_pay' column -- D70 forbids case-insensitive fallback anywhere
        in scope enforcement, so this must be treated the same as gross_pay
        not being listed in scope at all: rejected, not executed."""
        with pytest.raises(ColumnScopeError) as exc_info:
            _sample_with_scope(
                "analytics", "orders", _WRONG_CASE_SCOPE, session_id="sess-001"
            )
        assert exc_info.value.code == "COLUMN_SCOPE_VIOLATION"
        mock_execute.assert_not_called()


class TestD83AdvSampleRowsRejectMessageDoesNotLeakColumnNames:
    def test_D83_adv_sample_rows_reject_message_does_not_leak_column_names(
        self, mock_execute, mock_catalog
    ):
        """The forbidden column's real name ('gross_pay') must never appear in
        the exception message surfaced to the caller -- only a generic
        message (column names are scope-token content, not something to echo
        back to a caller who, by definition, isn't supposed to know they
        exist). This locks in app/service.py's existing 'log only the count'
        discipline as an explicit regression guard for sample_rows."""
        with pytest.raises(ColumnScopeError) as exc_info:
            _sample_with_scope(
                "analytics", "orders", _MISSING_ONE_SCOPE, session_id="sess-001"
            )
        message = str(exc_info.value.message)
        assert "gross_pay" not in message
        assert "gross_pay" not in repr(exc_info.value)
        mock_execute.assert_not_called()
