"""Guardrail parity tests for app/service.py::explain_query.

explain_query previously wrapped user SQL in ``EXPLAIN`` after only
validate_and_sanitize — with NO scratch-session or column-scope enforcement.
Because ClickHouse must resolve referenced tables/columns to build a plan, a
scoped caller could ``EXPLAIN SELECT ... FROM scratch.s_<victim>_...`` to
confirm the existence/column structure of another session's scratch table, or
probe out-of-scope warehouse columns — bypassing the exact gates run_query
enforces.

These tests assert explain_query now applies the SAME shared guardrail
(_enforce_query_guardrails) as run_query, and that the two paths raise the same
codes for the same violation.

Test IDs:
  explain-scratch-cross-session-rejected     (ADVERSARIAL — the exact leak)
  explain-scratch-own-session-passes         (POSITIVE)
  explain-column-scope-rejected              (COLUMN-SCOPE)
  explain-run-consistency                    (CONSISTENCY — same code both paths)
  explain-no-scope-stdio-parity              (NO-SCOPE / stdio parity)
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
from app.errors import (  # noqa: E402
    CartesianJoinError,
    ColumnScopeError,
    ParseFailedError,
)

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

# Full scope — grants access to every warehouse column.
_FULL_SCOPE: frozenset[str] = frozenset([
    "analytics.employees.employee_id",
    "analytics.employees.department",
    "analytics.employees.status",
])

# Restricted scope — deliberately EXCLUDES analytics.employees.status.
_RESTRICTED_SCOPE: frozenset[str] = frozenset([
    "analytics.employees.employee_id",
    "analytics.employees.department",
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
        m.return_value = (["explain"], [["Expression"]])
        yield m


@pytest.fixture()
def mock_catalog():
    """Patch get_catalog_schema inside app.service to return _CATALOG."""
    with patch("app.service.get_catalog_schema") as m:
        m.return_value = _CATALOG
        yield m


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _explain_with_scope(sql, scope, session_id=None):
    """Call service.explain_query with the given scope/session_id via context vars."""
    from app.principal import current_scope, current_session_id
    scope_token = current_scope.set(scope)
    session_token = current_session_id.set(session_id)
    try:
        return service.explain_query(sql)
    finally:
        current_scope.reset(scope_token)
        current_session_id.reset(session_token)


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
# ADVERSARIAL — the exact leak: EXPLAIN of another session's scratch table.
# ---------------------------------------------------------------------------

class TestExplainScratchCrossSessionRejected:
    """explain-scratch-cross-session-rejected — EXPLAIN must not be a side channel
    that confirms another session's scratch table exists / has given columns."""

    def test_explain_cross_session_scratch_rejected(self, mock_execute, mock_catalog):
        # Table prefix s_sessother_ but caller session is sessmine.
        sql = (
            "SELECT s.employee_id, s.salary_adjustment, e.department "
            "FROM scratch.s_sessother_compensation AS s "
            "JOIN analytics.employees AS e ON s.employee_id = e.employee_id "
            "WHERE e.status = 'A'"
        )
        with pytest.raises(ColumnScopeError) as exc_info:
            _explain_with_scope(sql, _FULL_SCOPE, session_id="sessmine")
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        # The leak is closed BEFORE the plan is ever built.
        mock_execute.assert_not_called()

    def test_explain_cross_session_scratch_rejected_even_with_empty_scope(
        self, mock_execute, mock_catalog
    ):
        """Allow-all (empty frozenset) scope must NOT bypass scratch isolation in explain."""
        sql = "SELECT employee_id FROM scratch.s_sessother_onboarding"
        with pytest.raises(ColumnScopeError) as exc_info:
            _explain_with_scope(sql, frozenset(), session_id="sessmine")
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()


# ---------------------------------------------------------------------------
# POSITIVE — EXPLAIN of the caller's OWN scratch table is allowed through.
# ---------------------------------------------------------------------------

class TestExplainOwnScratchPasses:
    """explain-scratch-own-session-passes — a caller may EXPLAIN their own scratch table."""

    def test_explain_own_scratch_passes(self, mock_execute, mock_catalog):
        sql = (
            "SELECT hire_date_override, salary_adj "
            "FROM scratch.s_sessmine_compensation"
        )
        result = _explain_with_scope(sql, _FULL_SCOPE, session_id="sessmine")
        assert result is not None
        # Reached _execute → EXPLAIN was actually built and run.
        mock_execute.assert_called_once()
        explain_sql = mock_execute.call_args.args[0]
        assert explain_sql.startswith("EXPLAIN ")


# ---------------------------------------------------------------------------
# COLUMN-SCOPE — EXPLAIN referencing an out-of-scope column is rejected.
# ---------------------------------------------------------------------------

class TestExplainColumnScopeRejected:
    """explain-column-scope-rejected — a non-empty scope excludes a column and
    EXPLAIN referencing it raises COLUMN_SCOPE_VIOLATION."""

    def test_explain_out_of_scope_column_rejected(self, mock_execute, mock_catalog):
        # status is NOT in _RESTRICTED_SCOPE.
        sql = "SELECT employee_id, status FROM analytics.employees"
        with pytest.raises(ColumnScopeError) as exc_info:
            _explain_with_scope(sql, _RESTRICTED_SCOPE, session_id="sessmine")
        assert exc_info.value.code == "COLUMN_SCOPE_VIOLATION"
        mock_execute.assert_not_called()


# ---------------------------------------------------------------------------
# CONSISTENCY — explain_query and run_query raise the SAME code for the SAME
# violation (proves the two paths share one enforcement function).
# ---------------------------------------------------------------------------

class TestExplainRunConsistency:
    """explain-run-consistency — identical violation → identical code on both paths."""

    def test_same_scratch_violation_code_on_both_paths(self, mock_execute, mock_catalog):
        sql = "SELECT employee_id FROM scratch.s_sessother_onboarding"

        with pytest.raises(ColumnScopeError) as explain_exc:
            _explain_with_scope(sql, _FULL_SCOPE, session_id="sessmine")
        with pytest.raises(ColumnScopeError) as run_exc:
            _run_with_scope(sql, _FULL_SCOPE, session_id="sessmine")

        assert explain_exc.value.code == run_exc.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_same_column_scope_violation_code_on_both_paths(self, mock_execute, mock_catalog):
        sql = "SELECT employee_id, status FROM analytics.employees"

        with pytest.raises(ColumnScopeError) as explain_exc:
            _explain_with_scope(sql, _RESTRICTED_SCOPE, session_id="sessmine")
        with pytest.raises(ColumnScopeError) as run_exc:
            _run_with_scope(sql, _RESTRICTED_SCOPE, session_id="sessmine")

        assert explain_exc.value.code == run_exc.value.code == "COLUMN_SCOPE_VIOLATION"
        mock_execute.assert_not_called()


# ---------------------------------------------------------------------------
# NO-SCOPE (stdio) — WAREHOUSE query still skips provenance (GPT-unaffected
# invariant); SCRATCH reference now fails closed even scope-less (ADR-0002/H3).
# ---------------------------------------------------------------------------

class TestExplainNoScopeWarehouseSkipsProvenance:
    """explain-no-scope-warehouse-skips-provenance — with scope None a NON-scratch
    (warehouse) explain skips the provenance block and executes, exactly like
    run_query's GPT-unaffected path. Only scratch references trigger the gate."""

    def test_explain_no_scope_warehouse_skips_scope_block(self, mock_execute, mock_catalog):
        sql = "SELECT employee_id FROM analytics.employees"
        from app.principal import current_scope, current_session_id
        scope_token = current_scope.set(None)
        session_token = current_session_id.set(None)
        try:
            result = service.explain_query(sql)
        finally:
            current_scope.reset(scope_token)
            current_session_id.reset(session_token)
        assert result is not None
        mock_execute.assert_called_once()
        # No catalog resolution was needed — the provenance block was skipped entirely.
        mock_catalog.assert_not_called()


class TestExplainNoScopeScratchFailsClosed:
    """explain-no-scope-scratch-fails-closed — ADR-0002/H3: with the gate ON
    (default), a scratch reference on the scope-less/session-less explain path is
    now FAIL-CLOSED (transport-agnostic), superseding the old "no-scope skips the
    scope block for scratch too" behavior. stdio cannot create scratch tables, so a
    session-less scratch READ must be denied on explain as well."""

    def test_explain_no_scope_scratch_fails_closed(self, mock_execute, mock_catalog):
        sql = "SELECT employee_id FROM scratch.s_sessxyz_whatever"
        from app.principal import current_scope, current_session_id
        scope_token = current_scope.set(None)
        session_token = current_session_id.set(None)
        try:
            with pytest.raises(ColumnScopeError) as exc_info:
                service.explain_query(sql)
        finally:
            current_scope.reset(scope_token)
            current_session_id.reset(session_token)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_explain_no_scope_scratch_gate_off_reverts_to_execute(
        self, mock_execute, mock_catalog
    ):
        """Off-switch parity with run_query: with require_session_scratch_gate=False
        the scope-less scratch explain reverts to the legacy skip-and-execute path."""
        from app.config import Settings
        gate_off = Settings().model_copy(update={"require_session_scratch_gate": False})
        sql = "SELECT employee_id FROM scratch.s_sessxyz_whatever"
        from app.principal import current_scope, current_session_id
        scope_token = current_scope.set(None)
        session_token = current_session_id.set(None)
        try:
            result = service.explain_query(sql, settings=gate_off)
        finally:
            current_scope.reset(scope_token)
            current_session_id.reset(session_token)
        assert result is not None
        mock_execute.assert_called_once()
        mock_catalog.assert_not_called()


# ---------------------------------------------------------------------------
# CARTESIAN — the unconditional gate fires through the explain path too,
# including on the no-scope (stdio) path.
# ---------------------------------------------------------------------------

class TestExplainCartesianJoinRejected:
    """test_explain_cartesian_join_rejected — EXPLAIN of a cross join of two base
    tables with no join condition is rejected before a plan is built. The gate is
    unconditional, so it must fire even with scope=None."""

    def test_explain_cartesian_join_rejected(self, mock_execute, mock_catalog):
        sql = "SELECT * FROM analytics.employees CROSS JOIN analytics.departments"
        # scope set — fires via the scoped path.
        with pytest.raises(CartesianJoinError) as exc_info:
            _explain_with_scope(sql, _FULL_SCOPE, session_id="sessmine")
        assert exc_info.value.code == "CARTESIAN_JOIN_FORBIDDEN"
        mock_execute.assert_not_called()

    def test_explain_cartesian_join_rejected_no_scope(self, mock_execute, mock_catalog):
        """The cartesian gate is UNCONDITIONAL — it fires on the stdio path (scope None)."""
        sql = "SELECT * FROM analytics.employees CROSS JOIN analytics.departments"
        from app.principal import current_scope, current_session_id
        scope_token = current_scope.set(None)
        session_token = current_session_id.set(None)
        try:
            with pytest.raises(CartesianJoinError) as exc_info:
                service.explain_query(sql)
        finally:
            current_scope.reset(scope_token)
            current_session_id.reset(session_token)
        assert exc_info.value.code == "CARTESIAN_JOIN_FORBIDDEN"
        mock_execute.assert_not_called()


# ---------------------------------------------------------------------------
# PARSE-FAIL — a scoped caller cannot use EXPLAIN to bypass the fail-closed
# provenance gate: unparseable SQL is rejected without a plan.
# ---------------------------------------------------------------------------

class TestExplainUnparseableScopedFailsClosed:
    """test_explain_unparseable_scoped_fails_closed — a scoped EXPLAIN of SQL whose
    column provenance cannot be extracted is fail-closed (PARSE_FAILED_CLOSED),
    exactly as run_query. EXPLAIN is NOT an escape hatch."""

    def test_explain_unparseable_scoped_fails_closed(self, mock_execute, mock_catalog):
        # generateRandom passes validate_and_sanitize (it is a SELECT) but the
        # provenance extractor cannot resolve it -> ProvenanceExtractionError.
        sql = "SELECT * FROM generateRandom('x UInt32', 42, 10, 2)"
        with pytest.raises(ParseFailedError) as exc_info:
            _explain_with_scope(sql, _FULL_SCOPE, session_id="sessmine")
        assert exc_info.value.code == "PARSE_FAILED_CLOSED"
        mock_execute.assert_not_called()


# ---------------------------------------------------------------------------
# NULL-SESSION scratch — omit-the-header bypass defense, through the explain path.
# scope set but session_id None: a scratch reference can never be proven to
# belong to the caller, so it must fail closed.
# ---------------------------------------------------------------------------

class TestExplainScratchNullSessionFailsClosed:
    """test_explain_scratch_ref_with_null_session_fails_closed — scope set but
    session_id None + a scratch table reference -> SCRATCH_SESSION_VIOLATION.
    Covers both the bare and the backtick-quoted identifier forms."""

    _HEX32 = "0123456789abcdef0123456789abcdef"

    def test_explain_scratch_null_session_fails_closed(self, mock_execute, mock_catalog):
        sql = f"SELECT employee_id FROM scratch.s_deadbeef_bp_{self._HEX32}"
        with pytest.raises(ColumnScopeError) as exc_info:
            _explain_with_scope(sql, _FULL_SCOPE, session_id=None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_explain_scratch_null_session_backtick_quoted_fails_closed(
        self, mock_execute, mock_catalog
    ):
        sql = f"SELECT employee_id FROM `scratch`.`s_deadbeef_bp_{self._HEX32}`"
        with pytest.raises(ColumnScopeError) as exc_info:
            _explain_with_scope(sql, _FULL_SCOPE, session_id=None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()


# ---------------------------------------------------------------------------
# ADR-0002 review — REST shape (scope=None, session=None) + the NO-SPACE quoted
# bypass. Deleting the quote used to fuse `FROM"scratch"` → `FROMscratch`, so the
# scratch-reference pre-check returned False, provenance never ran on the
# scope-less path, and EXPLAIN of a foreign scratch table EXECUTED. The
# replace-with-space fix closes it: these must fail closed via explain too.
# ---------------------------------------------------------------------------

class TestExplainRestScratchNoSpaceBypassFailsClosed:
    _HEX32 = "0123456789abcdef0123456789abcdef"

    def test_explain_rest_scratch_double_quote_no_space(self, mock_execute, mock_catalog):
        sql = f'SELECT employee_id FROM"scratch"."s_deadbeef_bp_{self._HEX32}"'
        with pytest.raises(ColumnScopeError) as exc_info:
            _explain_with_scope(sql, None, session_id=None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_explain_rest_scratch_backtick_no_space(self, mock_execute, mock_catalog):
        sql = f"SELECT employee_id FROM`scratch`.`s_deadbeef_bp_{self._HEX32}`"
        with pytest.raises(ColumnScopeError) as exc_info:
            _explain_with_scope(sql, None, session_id=None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_explain_rest_scratch_join_no_space(self, mock_execute, mock_catalog):
        sql = (
            'SELECT e.employee_id FROM analytics.employees e '
            f'JOIN"scratch"."s_deadbeef_bp_{self._HEX32}" s ON e.employee_id = s.employee_id'
        )
        with pytest.raises(ColumnScopeError) as exc_info:
            _explain_with_scope(sql, None, session_id=None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()
