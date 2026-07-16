"""D64 scratch-table isolation for the METADATA read tools (list_tables / get_table_schema).

run_query / sample_rows were already session-gated; these tests cover the metadata
leak fixed alongside them: list_tables('scratch') must return only the caller's own
scratch tables (empty when no session is bound), and get_table_schema('scratch', ...)
must raise SCRATCH_SESSION_VIOLATION for a foreign / session-less scratch table while
still returning the schema of the caller's own scratch table.

Layer-1 unit tests — no live ClickHouse required.

Test IDs:
  D64-list-scratch-own-session-only
  D64-list-scratch-no-session-empty
  D64-list-nonscratch-unaffected
  D64-schema-scratch-own-session-passes
  D64-schema-scratch-foreign-session-rejected
  D64-schema-scratch-no-session-rejected
  D64-schema-nonscratch-unaffected
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Env vars before any app module loads (matches the existing scratch tests).
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
from app.config import get_settings  # noqa: E402
from app.errors import ColumnScopeError  # noqa: E402
from app.principal import current_session_id  # noqa: E402

_SCRATCH_DB = get_settings().scratch_database  # "scratch"

# Session A owns two scratch tables; session B owns one.  Names follow the mint
# pattern s_<session_id>_bp_<uuid> (session ids are underscore-free).
_SESSION_A = "sessaaa111"
_SESSION_B = "sessbbb222"

_A_TABLE_1 = f"s_{_SESSION_A}_bp_deadbeef"
_A_TABLE_2 = f"s_{_SESSION_A}_bp_cafef00d"
_B_TABLE_1 = f"s_{_SESSION_B}_bp_0badf00d"

# The raw system.tables rows list_tables would see for the scratch database:
# every session's scratch tables live in one physical database.
_SCRATCH_ROWS = [
    (_SCRATCH_DB, _A_TABLE_1, "MergeTree"),
    (_SCRATCH_DB, _A_TABLE_2, "MergeTree"),
    (_SCRATCH_DB, _B_TABLE_1, "MergeTree"),
]

_WAREHOUSE_ROWS = [
    ("analytics", "employees", "MergeTree"),
    ("analytics", "departments", "MergeTree"),
]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_execute():
    """Patch execute_query inside app.service; per-test .return_value is set."""
    with patch("app.service.execute_query") as m:
        yield m


def _list_with_session(database, session_id):
    token = current_session_id.set(session_id)
    try:
        return service.list_tables(database)
    finally:
        current_session_id.reset(token)


def _schema_with_session(database, table, session_id):
    token = current_session_id.set(session_id)
    try:
        return service.get_table_schema(database, table)
    finally:
        current_session_id.reset(token)


# ---------------------------------------------------------------------------
# list_tables
# ---------------------------------------------------------------------------

class TestListTablesScratchIsolation:
    def test_D64_list_scratch_own_session_only(self, mock_execute):
        """Session A sees ONLY A's scratch tables, never B's."""
        mock_execute.return_value = (["database", "name", "engine"], _SCRATCH_ROWS)
        result = _list_with_session(_SCRATCH_DB, _SESSION_A)
        names = {t["name"] for t in result}
        assert names == {_A_TABLE_1, _A_TABLE_2}
        assert _B_TABLE_1 not in names

    def test_D64_list_scratch_session_b_only_sees_b(self, mock_execute):
        mock_execute.return_value = (["database", "name", "engine"], _SCRATCH_ROWS)
        result = _list_with_session(_SCRATCH_DB, _SESSION_B)
        assert {t["name"] for t in result} == {_B_TABLE_1}

    def test_D64_list_scratch_no_session_empty(self, mock_execute):
        """FAIL-CLOSED: no bound session -> empty scratch listing (never leak)."""
        mock_execute.return_value = (["database", "name", "engine"], _SCRATCH_ROWS)
        result = _list_with_session(_SCRATCH_DB, None)
        assert result == []

    def test_D64_list_nonscratch_unaffected(self, mock_execute):
        """A non-scratch database returns its full, unfiltered table list."""
        mock_execute.return_value = (["database", "name", "engine"], _WAREHOUSE_ROWS)
        # Even with no session bound, warehouse listings are unchanged.
        result = _list_with_session("analytics", None)
        assert {t["name"] for t in result} == {"employees", "departments"}

    def test_D64_list_scratch_shape_preserved(self, mock_execute):
        """The row dict shape {database,name,engine} is preserved after filtering."""
        mock_execute.return_value = (["database", "name", "engine"], _SCRATCH_ROWS)
        result = _list_with_session(_SCRATCH_DB, _SESSION_A)
        assert result[0] == {
            "database": _SCRATCH_DB,
            "name": _A_TABLE_1,
            "engine": "MergeTree",
        }


# ---------------------------------------------------------------------------
# get_table_schema
# ---------------------------------------------------------------------------

class TestGetTableSchemaScratchIsolation:
    @pytest.fixture()
    def mock_schema_pipeline(self):
        """Mocks needed for a schema call that reaches introspection + overlay."""
        cols = (["name", "type", "comment"], [["employee_id", "UInt64", ""]])
        with patch("app.service.execute_query", return_value=cols) as m_exec, patch(
            "app.service.get_semantic_catalog", return_value={}
        ), patch("app.service.get_catalog_sha", return_value="a" * 40):
            yield m_exec

    def test_D64_schema_scratch_own_session_passes(self, mock_schema_pipeline):
        """Session A CAN read the schema of its own scratch table (D93 flows)."""
        result = _schema_with_session(_SCRATCH_DB, _A_TABLE_1, _SESSION_A)
        assert result["table"] == _A_TABLE_1
        assert result["catalogued"] is False
        assert {c["name"] for c in result["columns"]} == {"employee_id"}
        mock_schema_pipeline.assert_called_once()

    def test_D64_schema_scratch_foreign_session_rejected(self, mock_execute):
        """Session B requesting A's scratch schema -> SCRATCH_SESSION_VIOLATION.

        execute_query must NOT be called: the gate fires before introspection.
        """
        with pytest.raises(ColumnScopeError) as exc_info:
            _schema_with_session(_SCRATCH_DB, _A_TABLE_1, _SESSION_B)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_D64_schema_scratch_no_session_rejected(self, mock_execute):
        """FAIL-CLOSED: no bound session -> SCRATCH_SESSION_VIOLATION, no introspection."""
        with pytest.raises(ColumnScopeError) as exc_info:
            _schema_with_session(_SCRATCH_DB, _A_TABLE_1, None)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_D64_schema_scratch_malformed_name_rejected(self, mock_execute):
        """A non s_<sid>_<suffix> name in the scratch db is rejected fail-closed."""
        with pytest.raises(ColumnScopeError) as exc_info:
            _schema_with_session(_SCRATCH_DB, "compensation_export", _SESSION_A)
        assert exc_info.value.code == "SCRATCH_SESSION_VIOLATION"
        mock_execute.assert_not_called()

    def test_D64_schema_nonscratch_unaffected(self):
        """A non-scratch table's schema is unchanged (no session gate applied)."""
        cols = (["name", "type", "comment"], [["employee_id", "UInt64", ""]])
        with patch("app.service.execute_query", return_value=cols), patch(
            "app.service.get_semantic_catalog", return_value={}
        ), patch("app.service.get_catalog_sha", return_value="b" * 40):
            # No session bound, yet a warehouse table's schema is still returned.
            result = _schema_with_session("analytics", "employees", None)
        assert result["table"] == "employees"
        assert {c["name"] for c in result["columns"]} == {"employee_id"}
