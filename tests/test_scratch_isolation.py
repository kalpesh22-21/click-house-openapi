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
            "FROM scratch.s_sessabc123_onboarding AS s "
            "JOIN analytics.employees AS e ON s.employee_id = e.employee_id "
            "WHERE e.status = 'A'"
        )
        result = _run_with_scope(sql, _FULL_SCOPE, session_id="sessabc123")
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
            "FROM scratch.s_sessxyz_compensation"
        )
        result = _run_with_scope(sql, _FULL_SCOPE, session_id="sessxyz")
        assert result is not None
        mock_execute.assert_called_once()


class TestD64ScratchCrossSessionRejected:
    """D64-scratch-cross-session-rejected — querying another session's scratch table is blocked."""

    def test_D64_scratch_cross_session_rejected(self, mock_execute, mock_catalog):
        # Table name has session prefix s_sessxyz789_ but caller is sessabc123.
        sql = (
            "SELECT s.employee_id, s.salary_adjustment, e.department "
            "FROM scratch.s_sessxyz789_compensation AS s "
            "JOIN analytics.employees AS e ON s.employee_id = e.employee_id "
            "WHERE e.status = 'A'"
        )
        with pytest.raises(ColumnScopeError) as exc_info:
            _run_with_scope(sql, _FULL_SCOPE, session_id="sessabc123")
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_D64_scratch_different_session_rejected(self, mock_execute, mock_catalog):
        sql = (
            "SELECT employee_id "
            "FROM scratch.s_sessother_onboarding"
        )
        with pytest.raises(ColumnScopeError) as exc_info:
            _run_with_scope(sql, _FULL_SCOPE, session_id="sessmine")
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
            _run_with_scope(sql, _FULL_SCOPE, session_id="sessabc123")
        # Malformed scratch name -> ScratchSessionError -> SCRATCH_SESSION_VIOLATION
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_D64_scratch_table_no_suffix_fail_closed(self, mock_execute, mock_catalog):
        """s_sessabc123_ with no suffix is also invalid."""
        # The provenance extractor requires a non-empty suffix after the extracted
        # session, so exactly `s_sessabc123_` with nothing after is rejected.
        sql = "SELECT x FROM scratch.s_sessabc123_"
        with pytest.raises(ColumnScopeError) as exc_info:
            _run_with_scope(sql, _FULL_SCOPE, session_id="sessabc123")
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()


class TestScratchNoSessionFailsClosedEvenScopeless:
    """ADR-0002 / H3 — with the gate ON (default), a scratch reference on the
    scope-less/session-less path (stdio or REST shape) is now FAIL-CLOSED.

    This SUPERSEDES the old D64 "no-session-id-no-check" behavior. The gate is
    transport-agnostic: stdio has no production scratch use and cannot create
    scratch tables, so a session-less scratch READ must be denied there too
    (north-star: only the creating session ever sees its scratch table). The
    legacy skip-and-execute behavior is now reachable ONLY via the off-switch
    (require_session_scratch_gate=False), pinned by the companion test below."""

    def test_scratch_scopeless_no_session_fails_closed(self, mock_execute, mock_catalog):
        # scope=None + session=None + a scratch reference → SCRATCH_SESSION_VIOLATION.
        # The scratch-reference pre-check triggers provenance even with no scope, so
        # the None session can never prove ownership and the read is rejected.
        sql = (
            "SELECT employee_id "
            "FROM scratch.s_sessxyz_whatever"
        )
        from app.principal import current_scope, current_session_id
        scope_token = current_scope.set(None)
        session_token = current_session_id.set(None)
        try:
            with pytest.raises(ColumnScopeError) as exc_info:
                service.run_query(sql)
        finally:
            current_scope.reset(scope_token)
            current_session_id.reset(session_token)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()
        # The catalog IS consulted now — provenance runs to validate the scratch ref.
        mock_catalog.assert_called()

    def test_scratch_scopeless_gate_off_reverts_to_skip_and_execute(
        self, mock_execute, mock_catalog
    ):
        """Off-switch: with require_session_scratch_gate=False the scratch-reference
        disjunct is disabled, so scope=None + session=None reverts to the legacy
        skip-and-execute behavior (no provenance parse, query runs). This is the
        ONE test that pins the pre-ADR-0002 permissive stdio path."""
        from app.config import Settings
        gate_off = Settings().model_copy(update={"require_session_scratch_gate": False})
        sql = (
            "SELECT employee_id "
            "FROM scratch.s_sessxyz_whatever"
        )
        from app.principal import current_scope, current_session_id
        scope_token = current_scope.set(None)
        session_token = current_session_id.set(None)
        try:
            result = service.run_query(sql, settings=gate_off)
        finally:
            current_scope.reset(scope_token)
            current_session_id.reset(session_token)
        assert result is not None
        mock_execute.assert_called_once()
        # No catalog query was needed — enforcement skipped (legacy scope-only trigger).
        mock_catalog.assert_not_called()

    def test_warehouse_scopeless_still_skips_provenance_and_executes(
        self, mock_execute, mock_catalog
    ):
        """GPT-unaffected invariant: a NON-scratch (warehouse) query on the same
        scope-less/session-less path still skips provenance entirely and executes —
        only scratch references trigger the gate."""
        sql = "SELECT employee_id FROM analytics.employees"
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
        mock_catalog.assert_not_called()

    def test_D64_scope_set_but_no_session_id_fails_closed_on_scratch(
        self, mock_execute, mock_catalog
    ):
        """When scope is set but session_id is None, a scratch reference now FAILS
        CLOSED (auth-hardening Slice 1 / D64): a scratch table whose owning session
        is unknown can never be proven to belong to the caller.

        This is the reconciliation of the omit-the-header bypass — previously this
        path silently passed (session_id=None was a 'skip validation' sentinel),
        which let a scoped caller read another session's scratch by dropping the
        X-Session-Id header. It must now reject before execution.
        """
        sql = (
            "SELECT hire_date_override "
            "FROM scratch.s_sessabc_data"
        )
        with pytest.raises(ColumnScopeError) as exc_info:
            _run_with_scope(sql, _FULL_SCOPE, session_id=None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()


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
            "FROM scratch.s_sessotherxyz_onboarding AS s "
            "JOIN analytics.employees AS e ON s.employee_id = e.employee_id "
            "WHERE e.status = 'A'"
        )
        with pytest.raises(ColumnScopeError) as exc_info:
            _run_with_scope(sql, frozenset(), session_id="sessmine")
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()
