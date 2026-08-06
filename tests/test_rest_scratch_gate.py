"""ADR-0002 / H3 — scratch access fails closed on EVERY transport, incl. REST.

Background
----------
On the REST transport (the ChatGPT GPT Action surface) ``current_scope`` is None
(auth.py binds neither scope nor session). The legacy enforcement rode inside
``if scope is not None:`` in ``_enforce_query_guardrails`` and the ``sample_rows``
inline block, so scratch enforcement was SKIPPED on REST — a REST caller could
``SELECT * FROM scratch.s_<victim>_bp_...`` and read another session's scratch
table (H3).

The fix (ADR-0002) re-triggers provenance extraction whenever the query
REFERENCES the scratch DB (gated by ``require_session_scratch_gate``, default
True), independent of scope. A scratch reference with a None/mismatched session
then fails closed via ``_validate_scratch_name`` → SCRATCH_SESSION_VIOLATION.
Normal warehouse queries reference no scratch DB, so provenance never runs and
the GPT Action keeps working with no PARSE_FAILED_CLOSED regression.

Test IDs:
  H3-precheck-* .................. _references_scratch_db over-approximation
  H3-rest-scratch-run ............ scope=None+session=None+scratch → violation (run_query)
  H3-rest-scratch-sample ......... same via sample_rows
  H3-rest-scratch-explain ........ same via explain_query
  H3-gpt-unaffected-* ............ scope=None+session=None+warehouse → executes, no parse
  H3-flag-off-* .................. gate off reverts to legacy scope-only trigger
  H3-error-map-* ................. ColumnScopeError→403, ParseFailedError→400
"""

from __future__ import annotations

import asyncio
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
from app.config import Settings  # noqa: E402
from app.errors import ColumnScopeError, ParseFailedError  # noqa: E402
from app.service import _references_scratch_db  # noqa: E402

# ---------------------------------------------------------------------------
# Test catalog — one warehouse table. Scratch tables are NOT in the catalog.
# ---------------------------------------------------------------------------
_CATALOG = {
    "analytics.employees": {
        "employee_id": "UInt64",
        "department": "String",
        "status": "String",
    },
}

# A well-formed foreign scratch table name: s_<session>_bp_<32hex>.
_HEX32 = "0123456789abcdef0123456789abcdef"
_FOREIGN_SCRATCH = f"s_deadbeef_bp_{_HEX32}"


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    from app.catalog import invalidate_catalog_cache

    invalidate_catalog_cache()
    yield
    invalidate_catalog_cache()


@pytest.fixture()
def mock_execute():
    with patch("app.service.execute_query") as m:
        m.return_value = (["employee_id"], [["42"]])
        yield m


@pytest.fixture()
def mock_catalog():
    with patch("app.service.get_catalog_schema") as m:
        m.return_value = _CATALOG
        yield m


# ---------------------------------------------------------------------------
# Context helpers — set scope/session as the REST transport does (both None).
# ---------------------------------------------------------------------------

def _run(sql, scope, session_id, settings=None):
    from app.principal import current_scope, current_session_id

    scope_token = current_scope.set(scope)
    session_token = current_session_id.set(session_id)
    try:
        return service.run_query(sql, settings=settings)
    finally:
        current_scope.reset(scope_token)
        current_session_id.reset(session_token)


def _explain(sql, scope, session_id, settings=None):
    from app.principal import current_scope, current_session_id

    scope_token = current_scope.set(scope)
    session_token = current_session_id.set(session_id)
    try:
        return service.explain_query(sql, settings=settings)
    finally:
        current_scope.reset(scope_token)
        current_session_id.reset(session_token)


def _sample(database, table, scope, session_id, settings=None):
    from app.principal import current_scope, current_session_id

    scope_token = current_scope.set(scope)
    session_token = current_session_id.set(session_id)
    try:
        return service.sample_rows(database, table, limit=5, settings=settings)
    finally:
        current_scope.reset(scope_token)
        current_session_id.reset(session_token)


def _gate_off_settings() -> Settings:
    """Settings identical to the env default but with the ADR-0002 gate OFF."""
    return Settings().model_copy(update={"require_session_scratch_gate": False})


# ---------------------------------------------------------------------------
# Section 2 — _references_scratch_db pre-check (fail-closed-safe over-approx).
# ---------------------------------------------------------------------------

class TestReferencesScratchDbPreCheck:
    def test_matches_bare_scratch_ref(self):
        assert _references_scratch_db("SELECT * FROM scratch.t", "scratch") is True

    def test_matches_backtick_quoted_ref(self):
        assert _references_scratch_db("SELECT x FROM `scratch`.`t`", "scratch") is True

    def test_matches_case_insensitively(self):
        assert _references_scratch_db("SELECT x FROM SCRATCH.T", "scratch") is True

    def test_matches_inside_join(self):
        sql = "SELECT a.x FROM scratch.x AS a JOIN analytics.employees AS e ON a.id = e.employee_id"
        assert _references_scratch_db(sql, "scratch") is True

    def test_does_not_match_warehouse_only_query(self):
        assert _references_scratch_db(
            "SELECT employee_id FROM analytics.employees", "scratch"
        ) is False

    def test_column_named_scratch_is_tolerated_false_positive(self):
        """OVER-APPROXIMATION (documented, tolerated): a column/alias literally
        named ``scratch`` matches the token scan even though no scratch TABLE is
        referenced. This only forces the provenance parse to run (a safe,
        fail-closed cost) — it never grants access. The pre-check MUST never
        return False for a real scratch reference, so a spurious True here is the
        acceptable side of the trade."""
        assert _references_scratch_db(
            "SELECT scratch FROM analytics.employees", "scratch"
        ) is True

    def test_respects_configured_db_name(self):
        # A non-default scratch DB name is honoured; the default token is not.
        assert _references_scratch_db("SELECT * FROM sandbox.t", "sandbox") is True
        assert _references_scratch_db("SELECT * FROM scratch.t", "sandbox") is False

    # --- ADR-0002 review: no-space quoted forms (the verified false-negative) ---
    # Deleting the quote used to FUSE `FROM"scratch"` → `FROMscratch`, killing the
    # left \b boundary so `\bscratch\b` did not match → False → H3 bypass on REST.
    # Replacing the quote with a SPACE preserves the boundary. These MUST be True.

    def test_matches_double_quote_no_space(self):
        assert _references_scratch_db('SELECT x FROM"scratch"."t"', "scratch") is True

    def test_matches_backtick_no_space(self):
        assert _references_scratch_db("SELECT x FROM`scratch`.`t`", "scratch") is True

    def test_matches_join_double_quote_no_space(self):
        sql = 'SELECT a.x FROM analytics.employees a JOIN"scratch"."t" s ON a.id = s.id'
        assert _references_scratch_db(sql, "scratch") is True

    def test_matches_leading_double_quote_no_space(self):
        # Even with the quote flush against the keyword on BOTH sides.
        assert _references_scratch_db('FROM"scratch"."t"', "scratch") is True


# ---------------------------------------------------------------------------
# ADVERSARIAL — REST shape (scope=None, session=None) reading foreign scratch.
# This is the H3 close: it used to return rows; it must now fail closed.
# ---------------------------------------------------------------------------

class TestRestShapeScratchFailsClosed:
    def test_run_query_rest_scratch_violation(self, mock_execute, mock_catalog):
        sql = f"SELECT employee_id FROM scratch.{_FOREIGN_SCRATCH}"
        with pytest.raises(ColumnScopeError) as exc_info:
            _run(sql, scope=None, session_id=None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_explain_query_rest_scratch_violation(self, mock_execute, mock_catalog):
        sql = f"SELECT employee_id FROM scratch.{_FOREIGN_SCRATCH}"
        with pytest.raises(ColumnScopeError) as exc_info:
            _explain(sql, scope=None, session_id=None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_sample_rows_rest_scratch_violation(self, mock_execute, mock_catalog):
        with pytest.raises(ColumnScopeError) as exc_info:
            _sample("scratch", _FOREIGN_SCRATCH, scope=None, session_id=None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_run_query_rest_scratch_backtick_quoted_violation(
        self, mock_execute, mock_catalog
    ):
        sql = f"SELECT employee_id FROM `scratch`.`{_FOREIGN_SCRATCH}`"
        with pytest.raises(ColumnScopeError) as exc_info:
            _run(sql, scope=None, session_id=None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    # --- ADR-0002 review: the NO-SPACE quoted bypass, end-to-end on the REST shape.
    # Before the replace-with-space fix these EXECUTED (execute_query called) on
    # both run_query and explain_query. They must now fail closed. ---

    def test_run_query_rest_scratch_double_quote_no_space_violation(
        self, mock_execute, mock_catalog
    ):
        sql = f'SELECT employee_id FROM"scratch"."{_FOREIGN_SCRATCH}"'
        with pytest.raises(ColumnScopeError) as exc_info:
            _run(sql, scope=None, session_id=None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_run_query_rest_scratch_backtick_no_space_violation(
        self, mock_execute, mock_catalog
    ):
        sql = f"SELECT employee_id FROM`scratch`.`{_FOREIGN_SCRATCH}`"
        with pytest.raises(ColumnScopeError) as exc_info:
            _run(sql, scope=None, session_id=None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_run_query_rest_scratch_join_no_space_violation(
        self, mock_execute, mock_catalog
    ):
        sql = (
            'SELECT e.employee_id FROM analytics.employees e '
            f'JOIN"scratch"."{_FOREIGN_SCRATCH}" s ON e.employee_id = s.employee_id'
        )
        with pytest.raises(ColumnScopeError) as exc_info:
            _run(sql, scope=None, session_id=None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_explain_query_rest_scratch_double_quote_no_space_violation(
        self, mock_execute, mock_catalog
    ):
        sql = f'SELECT employee_id FROM"scratch"."{_FOREIGN_SCRATCH}"'
        with pytest.raises(ColumnScopeError) as exc_info:
            _explain(sql, scope=None, session_id=None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_explain_query_rest_scratch_backtick_no_space_violation(
        self, mock_execute, mock_catalog
    ):
        sql = f"SELECT employee_id FROM`scratch`.`{_FOREIGN_SCRATCH}`"
        with pytest.raises(ColumnScopeError) as exc_info:
            _explain(sql, scope=None, session_id=None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()


# ---------------------------------------------------------------------------
# GPT-UNAFFECTED — a normal warehouse query on the REST shape still executes and
# never triggers a provenance parse (no PARSE_FAILED_CLOSED regression).
# ---------------------------------------------------------------------------

class TestGptWarehouseQueryUnaffected:
    def test_run_query_warehouse_executes_without_provenance(
        self, mock_execute, mock_catalog
    ):
        sql = "SELECT employee_id FROM analytics.employees"
        with patch("app.service.extract_column_provenance") as mock_prov:
            result = _run(sql, scope=None, session_id=None)
        assert result is not None
        mock_execute.assert_called_once()
        # The GPT's normal query must NOT touch the provenance/catalog machinery.
        mock_prov.assert_not_called()
        mock_catalog.assert_not_called()

    def test_explain_query_warehouse_executes_without_provenance(
        self, mock_execute, mock_catalog
    ):
        sql = "SELECT employee_id FROM analytics.employees"
        with patch("app.service.extract_column_provenance") as mock_prov:
            result = _explain(sql, scope=None, session_id=None)
        assert result is not None
        mock_execute.assert_called_once()
        mock_prov.assert_not_called()
        mock_catalog.assert_not_called()


# ---------------------------------------------------------------------------
# FLAG-OFF — with require_session_scratch_gate=False the scratch-reference
# disjunct is disabled, so a scope=None scratch query reverts to legacy
# behavior (enforcement skipped, executes). Documents the off-switch.
# ---------------------------------------------------------------------------

class TestGateOffRevertsToLegacy:
    def test_run_query_gate_off_scratch_executes(self, mock_execute, mock_catalog):
        sql = f"SELECT employee_id FROM scratch.{_FOREIGN_SCRATCH}"
        result = _run(sql, scope=None, session_id=None, settings=_gate_off_settings())
        assert result is not None
        mock_execute.assert_called_once()
        # No provenance parse ran (legacy scope-only trigger, scope is None).
        mock_catalog.assert_not_called()

    def test_sample_rows_gate_off_scratch_executes(self, mock_execute, mock_catalog):
        result = _sample(
            "scratch", _FOREIGN_SCRATCH, scope=None, session_id=None,
            settings=_gate_off_settings(),
        )
        assert result is not None
        mock_execute.assert_called_once()
        mock_catalog.assert_not_called()


# ---------------------------------------------------------------------------
# main.py error-map — the two errors that now fire on REST must map to clean 4xx.
# ---------------------------------------------------------------------------

class TestDomainErrorMap:
    def test_map_contains_new_entries(self):
        from app.main import _DOMAIN_ERROR_STATUS

        assert _DOMAIN_ERROR_STATUS[ColumnScopeError] == 403
        assert _DOMAIN_ERROR_STATUS[ParseFailedError] == 400

    def test_handler_resolves_column_scope_to_403(self):
        from app.main import domain_error_handler

        exc = ColumnScopeError(message="denied", code="SCRATCH_SESSION_VIOLATION")
        resp = asyncio.run(domain_error_handler(_fake_request(), exc))
        assert resp.status_code == 403

    def test_handler_resolves_parse_failed_to_400(self):
        from app.main import domain_error_handler

        exc = ParseFailedError(message="parse", code="PARSE_FAILED_CLOSED")
        resp = asyncio.run(domain_error_handler(_fake_request(), exc))
        assert resp.status_code == 400


def _fake_request():
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/query",
            "headers": [],
        }
    )
