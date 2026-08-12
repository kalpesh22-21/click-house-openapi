"""Tests for the metadata-statement gate on runQuery / explainQuery.

Layer: 1 — Unit (execute_query and get_catalog_schema are mocked; no live stack).

BACKGROUND (found by tracing live agent turns, not by reading code):

  1. The runQuery tool advertised "SELECT, WITH, SHOW, DESCRIBE" and its `sql`
     parameter advertised "SELECT / WITH / SHOW / DESCRIBE".  Neither SHOW nor
     DESCRIBE works for a scoped caller, and that description is re-sent to the
     model on every round-trip.  Measured cost on one turn: 3 of 11 round-trips
     spent on rejected metadata SQL, then a wall-clock timeout.

  2. SHOW / DESCRIBE / DESC / EXPLAIN pass validate_and_sanitize's allowlist and
     then die inside column-provenance extraction (sqlglot parses SHOW/EXPLAIN as
     exp.Command and DESCRIBE/DESC as exp.Describe, none of which the extractor
     accepts).  The resulting PARSE_FAILED_CLOSED told the caller to "simplify the
     SQL" — unactionable, since no rewrite turns SHOW TABLES into a SELECT.

The prefixes are NOT removed from the security allowlist, because they genuinely
work on the UNSCOPED paths (REST admin / GPT Action, and MCP stdio local-trust),
where `current_scope is None` means provenance never runs.  The gate fires exactly
when provenance is required, so those callers are unaffected.

Test IDs:
  desc-runquery-tool-description-honest
  desc-runquery-sql-param-honest
  gate-scoped-show-rejected-honestly
  gate-scoped-describe-rejected-honestly
  gate-scoped-explain-rejected-honestly
  gate-explainquery-shares-the-gate
  gate-unscoped-show-still-executes
  gate-scoped-select-unaffected
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app import service
from app.errors import QueryValidationError
from app.mcp_server import mcp

# ---------------------------------------------------------------------------
# Catalog + scope fixtures (mirrors tests/test_scope_enforcement.py)
# ---------------------------------------------------------------------------

_CATALOG = {
    "analytics.orders": {
        "order_id": "UInt64",
        "status": "String",
    },
}

_SCOPE: frozenset[str] = frozenset(["analytics.orders.order_id", "analytics.orders.status"])

# Every statement kind that clears the security allowlist but cannot be given a
# column provenance.  DESC is the DESCRIBE synonym; both parse as exp.Describe.
# (`SHOW CREATE TABLE …` is absent on purpose: the denylist rejects it earlier, on
# the CREATE keyword, so it never reaches this gate.)
_METADATA_STATEMENTS = [
    "SHOW TABLES",
    "SHOW DATABASES",
    "DESCRIBE analytics.orders",
    "DESC analytics.orders",
    "EXPLAIN SELECT order_id FROM analytics.orders",
]


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    from app.catalog import invalidate_catalog_cache

    invalidate_catalog_cache()
    yield
    invalidate_catalog_cache()


@pytest.fixture()
def mock_execute():
    with patch("app.service.execute_query") as m:
        m.return_value = (["name"], [["orders"]])
        yield m


@pytest.fixture()
def mock_catalog():
    with patch("app.service.get_catalog_schema") as m:
        m.return_value = _CATALOG
        yield m


def _run_with_scope(sql, scope, session_id=None, fn=None):
    """Invoke a service entry point with scope/session context vars bound."""
    from app.principal import current_scope, current_session_id

    fn = fn or service.run_query
    scope_token = current_scope.set(scope)
    session_token = current_session_id.set(session_id)
    try:
        return fn(sql)
    finally:
        current_scope.reset(scope_token)
        current_session_id.reset(session_token)


def _run_query_tool():
    tools = asyncio.run(mcp.list_tools())
    return next(t for t in tools if t.name == "runQuery")


# ---------------------------------------------------------------------------
# Defect 1 — the advertised description must match what actually works
# ---------------------------------------------------------------------------


class TestRunQueryDescriptionHonest:
    """The model re-reads these strings on every round-trip; they must not lie."""

    def test_desc_runquery_tool_description_honest(self):
        description = _run_query_tool().description
        assert "SELECT or WITH" in description
        # It must not advertise SHOW/DESCRIBE as accepted statement kinds...
        assert "(SELECT, WITH, SHOW, DESCRIBE)" not in description
        # ...and it must point at the tools that actually serve metadata.
        assert "listTables" in description
        assert "getTableSchema" in description

    def test_desc_runquery_sql_param_honest(self):
        schema = _run_query_tool().inputSchema
        sql_description = schema["properties"]["sql"]["description"]
        assert "SELECT / WITH / SHOW / DESCRIBE" not in sql_description
        assert "SELECT or WITH" in sql_description
        assert "listTables" in sql_description


# ---------------------------------------------------------------------------
# Defect 2 — scoped callers get an honest, actionable rejection
# ---------------------------------------------------------------------------


class TestScopedMetadataStatementsRejected:
    """DISALLOWED_STATEMENT_TYPE names the allowed set, so the model can self-correct.

    Previously these reached the provenance extractor and returned
    PARSE_FAILED_CLOSED ("simplify the SQL"), which no rewrite could satisfy.
    """

    @pytest.mark.parametrize("sql", _METADATA_STATEMENTS)
    def test_gate_scoped_metadata_rejected_honestly(self, sql, mock_execute, mock_catalog):
        with pytest.raises(QueryValidationError) as exc_info:
            _run_with_scope(sql, _SCOPE, session_id="sess-001")

        assert exc_info.value.code == "DISALLOWED_STATEMENT_TYPE"
        message = exc_info.value.message
        # Actionable: names the allowed statement kinds and the metadata tools.
        assert "SELECT" in message
        assert "listTables" in message
        assert "getTableSchema" in message
        # The unactionable advice must be gone.
        assert "simplify" not in message.lower()
        # Rejected before execution AND before the catalog load.
        mock_execute.assert_not_called()
        mock_catalog.assert_not_called()

    @pytest.mark.parametrize("sql", _METADATA_STATEMENTS)
    def test_gate_explainquery_shares_the_gate(self, sql, mock_execute, mock_catalog):
        """explainQuery must not be an escape hatch around the statement-kind gate."""
        with pytest.raises(QueryValidationError) as exc_info:
            _run_with_scope(sql, _SCOPE, session_id="sess-001", fn=service.explain_query)

        assert exc_info.value.code == "DISALLOWED_STATEMENT_TYPE"
        mock_execute.assert_not_called()

    def test_gate_scoped_select_unaffected(self, mock_execute, mock_catalog):
        """A normal in-scope SELECT still executes — the gate only rejects metadata SQL."""
        result = _run_with_scope(
            "SELECT order_id FROM analytics.orders", _SCOPE, session_id="sess-001"
        )
        assert result is not None
        mock_execute.assert_called_once()

    def test_gate_scoped_with_unaffected(self, mock_execute, mock_catalog):
        """WITH is the other provenance-compatible prefix and must still pass."""
        sql = "WITH 'active' AS s SELECT order_id FROM analytics.orders WHERE status = s"
        result = _run_with_scope(sql, _SCOPE, session_id="sess-001")
        assert result is not None
        mock_execute.assert_called_once()


class TestUnscopedMetadataStatementsStillWork:
    """The unscoped paths (REST admin / GPT Action, MCP stdio) keep SHOW / DESCRIBE.

    `current_scope is None` means provenance never runs there, so these statements
    genuinely reach the executor today.  Removing them from the security allowlist
    outright would have broken those callers — hence a gate, not a removal.
    """

    @pytest.mark.parametrize("sql", _METADATA_STATEMENTS)
    def test_gate_unscoped_metadata_still_executes(self, sql, mock_execute, mock_catalog):
        result = _run_with_scope(sql, None, session_id=None)
        assert result is not None
        mock_execute.assert_called_once()
        # No scope → no provenance → the catalog is never loaded.
        mock_catalog.assert_not_called()


# ---------------------------------------------------------------------------
# The security allowlist itself is unchanged — the gate lives one layer up
# ---------------------------------------------------------------------------


class TestSecurityAllowlistUnchanged:
    def test_validate_and_sanitize_still_accepts_metadata_statements(self):
        from app.security import validate_and_sanitize

        for sql in _METADATA_STATEMENTS:
            assert validate_and_sanitize(sql, 100)

    def test_statement_supports_provenance_predicate(self):
        from app.security import statement_supports_provenance

        assert statement_supports_provenance("SELECT 1")
        assert statement_supports_provenance("  with x as (select 1) select * from x")
        for sql in _METADATA_STATEMENTS:
            assert not statement_supports_provenance(sql)
