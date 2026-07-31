"""Tests for the cartesian-join guardrail in app/service.py::run_query.

Layer-1 unit tests — no live ClickHouse required.  execute_query and
ensure_employee_access_fresh are mocked so the enforcement path runs in-process.

Key property under test: the block is applied UNCONDITIONALLY — independent of
whether a column scope is set — so it also protects the no-scope / stdio path.
These tests therefore run with scope=None (the stdio/local-trust context) and
assert the query is still rejected before execution.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Ensure env vars are set before any app module loads (matches conftest pattern).
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
from app.errors import CartesianJoinError  # noqa: E402


@pytest.fixture()
def mock_execute():
    """Patch execute_query inside app.service (asserted not-called on rejection)."""
    with patch("app.service.execute_query") as m:
        m.return_value = (["id"], [["1"]])
        yield m


@pytest.fixture()
def mock_employee_access():
    """Patch the employee-access refresh so the allow-path needs no ClickHouse."""
    with patch("app.service.ensure_employee_access_fresh") as m:
        yield m


def _run_no_scope(sql, mock_execute):
    """Call run_query on the no-scope (stdio/local-trust) path via context vars."""
    from app.principal import current_scope, current_session_id
    scope_token = current_scope.set(None)
    session_token = current_session_id.set(None)
    try:
        return service.run_query(sql)
    finally:
        current_scope.reset(scope_token)
        current_session_id.reset(session_token)


def test_run_query_rejects_cartesian_join_even_without_scope(mock_execute):
    sql = "SELECT * FROM employees CROSS JOIN departments"
    with pytest.raises(CartesianJoinError) as exc_info:
        _run_no_scope(sql, mock_execute)
    exc = exc_info.value
    assert exc.code == "CARTESIAN_JOIN_FORBIDDEN"
    # Message names both offending base tables so the model can self-correct.
    assert "employees" in exc.message
    assert "departments" in exc.message
    # Rejected before execution.
    mock_execute.assert_not_called()


def test_run_query_rejects_comma_join(mock_execute):
    with pytest.raises(CartesianJoinError) as exc_info:
        _run_no_scope("SELECT * FROM employees, departments", mock_execute)
    assert exc_info.value.code == "CARTESIAN_JOIN_FORBIDDEN"
    mock_execute.assert_not_called()


def test_run_query_allows_proper_join(mock_execute, mock_employee_access):
    sql = "SELECT * FROM employees e JOIN departments d ON e.dept_id = d.id"
    result = _run_no_scope(sql, mock_execute)
    assert result is not None
    mock_execute.assert_called_once()


def test_run_query_allows_cross_join_with_subquery(mock_execute, mock_employee_access):
    sql = "SELECT * FROM employees CROSS JOIN (SELECT now() AS asof) p"
    result = _run_no_scope(sql, mock_execute)
    assert result is not None
    mock_execute.assert_called_once()


# ---------------------------------------------------------------------------
# ADVERSARIAL (QA): the guardrail must not be bypassable at the service seam.
# ---------------------------------------------------------------------------


def test_run_query_rejects_bare_join_no_on(mock_execute):
    """A bare `JOIN b` (no ON/USING) is a cartesian and must be rejected before exec."""
    with pytest.raises(CartesianJoinError) as exc_info:
        _run_no_scope("SELECT * FROM employees JOIN departments", mock_execute)
    assert exc_info.value.code == "CARTESIAN_JOIN_FORBIDDEN"
    mock_execute.assert_not_called()


def test_run_query_rejects_constant_true_on_condition(mock_execute):
    """A constant-true `ON 1 = 1` does not relate the tables — it is a full cartesian
    product and is rejected BEFORE execution at the service boundary."""
    sql = "SELECT * FROM employees CROSS JOIN departments ON 1 = 1"
    with pytest.raises(CartesianJoinError) as exc_info:
        _run_no_scope(sql, mock_execute)
    assert exc_info.value.code == "CARTESIAN_JOIN_FORBIDDEN"
    mock_execute.assert_not_called()


def test_run_query_rejects_final_modifier_cartesian(mock_execute):
    """A FINAL-modified cartesian is rejected before execution (the guard unwraps
    exp.Final so the base tables stay visible)."""
    sql = "SELECT * FROM employees FINAL CROSS JOIN departments FINAL"
    with pytest.raises(CartesianJoinError) as exc_info:
        _run_no_scope(sql, mock_execute)
    assert exc_info.value.code == "CARTESIAN_JOIN_FORBIDDEN"
    mock_execute.assert_not_called()
