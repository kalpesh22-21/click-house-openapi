"""Tests for scratch-table session isolation in app/service.py::run_query.

D76 Layer-1 unit tests — no live ClickHouse required.

Test IDs:
  D64-scratch-own-session-passes
  D64-scratch-cross-session-rejected
  D64-scratch-parse-fail-closed
  D64-scratch-no-session-id-no-check
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Env vars before any app module loads
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

from app import service  # noqa: E402
from app.errors import ColumnScopeError  # noqa: E402

# ---------------------------------------------------------------------------
# Test catalog — minimal schema with one warehouse table.
# Scratch tables are NOT in the catalog by design (they are session-scoped).
# ---------------------------------------------------------------------------

_CATALOG = {
    "analytics.employees": {
        "employee_id": "UInt64",
        "department": "String",
        "status": "String",
    },
}

# Scope grants access to all warehouse columns.
_FULL_SCOPE: frozenset[str] = frozenset([
    "analytics.employees.employee_id",
    "analytics.employees.department",
    "analytics.employees.status",
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
        m.return_value = (["employee_id"], [["42"]])
        yield m


@pytest.fixture()
def mock_catalog():
    """Patch get_catalog_schema inside app.service to return _CATALOG."""
    with patch("app.service.get_catalog_schema") as m:
        m.return_value = _CATALOG
        yield m


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _run_with_scope(sql, scope, session_id=None):
    """Call service.run_query with the given scope/session_id via context vars."""
    from app.principal import current_scope, current_session_id
    scope_token = current_scope.set(scope)
    session_token = current_session_id.set(session_id)
    try:
        return service.run_query(sql)
    finally:
        current_scope.reset(scope_token)
        current_session_id.reset(session_token)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestD64ScratchOwnSessionPasses:
    """D64-scratch-own-session-passes — querying own session's scratch table is allowed."""

    def test_D64_scratch_own_session_passes(self, mock_execute, mock_catalog):
        # Scratch table name follows the s_<session_id>_<suffix> pattern.
        sql = (
            "SELECT s.employee_id, s.hire_date_override, e.department "
            "FROM scratch.s_sess_abc123_onboarding AS s "
            "JOIN analytics.employees AS e ON s.employee_id = e.employee_id "
            "WHERE e.status = 'A'"
        )
        result = _run_with_scope(sql, _FULL_SCOPE, session_id="sess_abc123")
        assert result is not None
        mock_execute.assert_called_once()

    def test_D64_scratch_own_session_only_scratch(self, mock_execute, mock_catalog):
        """Query against only own scratch table — no warehouse join required.

        Note: scratch column names must NOT overlap with any catalog column names
        because the extractor's step-8 fail-closed path triggers when an unresolved
        column has an exact catalog name match.  Use non-catalog names here.
        """
        sql = (
            "SELECT hire_date_override, salary_adj "
            "FROM scratch.s_sess_xyz_compensation"
        )
        result = _run_with_scope(sql, _FULL_SCOPE, session_id="sess_xyz")
        assert result is not None
        mock_execute.assert_called_once()


class TestD64ScratchCrossSessionRejected:
    """D64-scratch-cross-session-rejected — querying another session's scratch table is blocked."""

    def test_D64_scratch_cross_session_rejected(self, mock_execute, mock_catalog):
        # Table name has session prefix s_sess_xyz789_ but caller is sess_abc123.
        sql = (
            "SELECT s.employee_id, s.salary_adjustment, e.department "
            "FROM scratch.s_sess_xyz789_compensation AS s "
            "JOIN analytics.employees AS e ON s.employee_id = e.employee_id "
            "WHERE e.status = 'A'"
        )
        with pytest.raises(ColumnScopeError) as exc_info:
            _run_with_scope(sql, _FULL_SCOPE, session_id="sess_abc123")
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_D64_scratch_different_session_rejected(self, mock_execute, mock_catalog):
        sql = (
            "SELECT employee_id "
            "FROM scratch.s_sess_other_onboarding"
        )
        with pytest.raises(ColumnScopeError) as exc_info:
            _run_with_scope(sql, _FULL_SCOPE, session_id="sess_mine")
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()


class TestD64ScratchParseFailClosed:
    """D64-scratch-parse-fail-closed — malformed scratch table name (non-prefixed) is fail-closed."""

    def test_D64_scratch_malformed_name_fail_closed(self, mock_execute, mock_catalog):
        # scratch.compensation_export does not match s_<session_id>_<suffix> pattern.
        sql = (
            "SELECT employee_id "
            "FROM scratch.compensation_export"
        )
        with pytest.raises(ColumnScopeError) as exc_info:
            _run_with_scope(sql, _FULL_SCOPE, session_id="sess_abc123")
        # Malformed scratch name -> ScratchSessionError -> SCRATCH_SESSION_VIOLATION
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_D64_scratch_table_no_suffix_fail_closed(self, mock_execute, mock_catalog):
        """s_sess_abc123_ with no suffix is also invalid."""
        # The provenance extractor checks len(tbl_name) > len(expected_prefix) so
        # exactly `s_sess_abc123_` with nothing after is rejected.
        sql = "SELECT x FROM scratch.s_sess_abc123_"
        with pytest.raises(ColumnScopeError) as exc_info:
            _run_with_scope(sql, _FULL_SCOPE, session_id="sess_abc123")
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()


class TestD64ScratchNoSessionIdNoCheck:
    """D64-scratch-no-session-id-no-check — when scope is None (stdio), scratch names
    are not validated.  This matches the stdio/local-trust path."""

    def test_D64_scratch_no_session_id_no_check(self, mock_execute, mock_catalog):
        # scope=None → enforcement is entirely skipped, no session validation.
        sql = (
            "SELECT employee_id "
            "FROM scratch.s_sess_xyz_whatever"
        )
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
        # No catalog query was needed
        mock_catalog.assert_not_called()

    def test_D64_scope_set_but_no_session_id_passes(self, mock_execute, mock_catalog):
        """When scope is set but session_id is None, scratch validation is skipped
        (session_id=None is the 'skip validation' sentinel in the extractor).

        Note: scratch column names must NOT overlap with catalog column names to
        avoid triggering the step-8 fail-closed path on unresolved column names.
        """
        sql = (
            "SELECT hire_date_override "
            "FROM scratch.s_sess_abc_data"
        )
        # session_id=None with scope set → extractor skips session validation
        result = _run_with_scope(sql, _FULL_SCOPE, session_id=None)
        assert result is not None
        mock_execute.assert_called_once()


class TestD64ScratchIsolationSurvivesAllowAll:
    """Scratch-isolation (D64) must survive the allow-all (empty-scope) policy.
    Even when column_scope is an empty frozenset (ALLOW-ALL), cross-session
    scratch access must still be rejected."""

    def test_D64_scratch_cross_session_rejected_even_with_empty_scope(
        self, mock_execute, mock_catalog
    ):
        """Empty frozenset scope + session_id set + cross-session scratch → SCRATCH_SESSION_VIOLATION.

        Proves that the allow-all shortcut (empty scope) does NOT bypass scratch
        isolation — the extractor still runs and checks the table prefix.
        """
        sql = (
            "SELECT s.hire_date_override, e.department "
            "FROM scratch.s_sess_other_xyz_onboarding AS s "
            "JOIN analytics.employees AS e ON s.employee_id = e.employee_id "
            "WHERE e.status = 'A'"
        )
        with pytest.raises(ColumnScopeError) as exc_info:
            _run_with_scope(sql, frozenset(), session_id="sess_mine")
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()
