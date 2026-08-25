"""Route-level integration tests using FastAPI TestClient.

ClickHouse is fully mocked — no live server required.

Key patching note:
  query.py  imports `from app.clickhouse_client import execute_query` at module load.
  schema.py imports `from app.clickhouse_client import execute_query` at module load,
            AND calls `from app.clickhouse_client import get_client` inline in
            list_tables / get_table_schema.

  We must patch the names AS BOUND IN THE ROUTER MODULE:
    - app.routers.query.execute_query
    - app.routers.schema.execute_query
    - app.clickhouse_client.get_client (inline imports resolve at call time)

Tests assert:
  - /query rejects bad SQL with 400 BEFORE calling execute_query.
  - /query passes guardrail-approved SQL down to the mocked client.
  - /query applies LIMIT and returns the correct compact response shape.
  - /query/explain wraps SQL in EXPLAIN and calls execute_query.
  - Schema routes (/databases, /tables, /tables/schema) require auth.
  - /health requires no auth and returns correct shape.
  - Truncation behaviour: truncated=True when rows exceed max_response_rows.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from tests.jwt_helpers import make_jwt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# A valid tenant JWT (user_name='alice'); the autouse _patch_jwks fixture in
# conftest resolves its signing key locally so no IdP is contacted.
AUTH = {"Authorization": f"Bearer {make_jwt()}"}


# ---------------------------------------------------------------------------
# Module-level env setup — applied to every test in this file
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def env_setup():
    os.environ["MAX_RESPONSE_ROWS"] = "1000"
    os.environ["DEFAULT_LIMIT"] = "1000"
    os.environ["ALLOWED_DATABASES"] = "*"
    get_settings.cache_clear()
    yield
    os.environ["ALLOWED_DATABASES"] = "*"
    os.environ["MAX_RESPONSE_ROWS"] = "1000"
    get_settings.cache_clear()


@pytest.fixture()
def mock_execute():
    """Patch execute_query in the service layer — single patch covers all routes.

    Both query and schema operations now go through app.service.execute_query,
    so there is only one patch target.  Individual tests configure the return
    value as needed; the default here satisfies most schema-route tests.
    """
    with patch("app.service.execute_query") as m:
        m.return_value = (["id", "name"], [["1", "Alice"], ["2", "Bob"]])
        yield m


@pytest.fixture()
def mock_query_execute(mock_execute):
    """Alias kept for backward compatibility with existing query-route tests."""
    return mock_execute


@pytest.fixture()
def mock_schema_execute(mock_execute):
    """Alias kept for backward compatibility with existing schema-route tests."""
    mock_execute.return_value = (["name"], [["default"], ["analytics"]])
    return mock_execute


@pytest.fixture()
def mock_ch_client():
    """Patch get_client at the clickhouse_client module level.

    Needed so the ping() call in the lifespan/health route succeeds without a
    real ClickHouse server.
    """
    mock = MagicMock()
    mock.ping.return_value = True
    with patch("app.clickhouse_client.get_client", return_value=mock):
        yield mock


@pytest.fixture()
def client(mock_execute, mock_ch_client):
    """TestClient with all ClickHouse interactions stubbed."""
    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# /query — POST — bad SQL rejected before ClickHouse is called
# ===========================================================================

class TestQueryRouteRejection:

    @pytest.mark.parametrize("bad_sql, expected_code", [
        # INSERT is not in _ALLOWED_PREFIXES → DISALLOWED_STATEMENT_TYPE
        ("INSERT INTO t VALUES (1)", "DISALLOWED_STATEMENT_TYPE"),
        ("DROP TABLE t", "DISALLOWED_STATEMENT_TYPE"),
        ("UPDATE t SET x=1", "DISALLOWED_STATEMENT_TYPE"),
        ("DELETE FROM t", "DISALLOWED_STATEMENT_TYPE"),
        ("ALTER TABLE t ADD COLUMN c Int32", "DISALLOWED_STATEMENT_TYPE"),
        ("CREATE TABLE t (id Int32)", "DISALLOWED_STATEMENT_TYPE"),
        ("SELECT 1; DROP TABLE x", "MULTIPLE_STATEMENTS"),
        ("", "EMPTY_QUERY"),
        ("SELECT id FROM t FORMAT JSON", "DISALLOWED_KEYWORD"),
    ])
    def test_bad_sql_returns_400(self, client, mock_query_execute, bad_sql, expected_code):
        resp = client.post("/query", headers=AUTH, json={"sql": bad_sql})
        assert resp.status_code == 400, (
            f"Expected 400 for {bad_sql!r}, got {resp.status_code}: {resp.text}"
        )
        assert resp.json()["detail"]["code"] == expected_code

    def test_bad_sql_never_calls_execute_query(self, client, mock_query_execute):
        """The mocked execute_query must NOT be called when guardrail rejects SQL."""
        client.post("/query", headers=AUTH, json={"sql": "DROP TABLE secret"})
        mock_query_execute.assert_not_called()

    def test_multiple_bad_sql_never_calls_execute_query(self, client, mock_query_execute):
        for bad_sql in ["INSERT INTO t VALUES(1)", "DELETE FROM t", "ALTER TABLE t ADD COLUMN x Int32"]:
            mock_query_execute.reset_mock()
            client.post("/query", headers=AUTH, json={"sql": bad_sql})
            mock_query_execute.assert_not_called()


# ===========================================================================
# /query — POST — valid SQL passes through to ClickHouse
# ===========================================================================

class TestQueryRouteAcceptance:

    def test_valid_select_returns_200(self, client, mock_query_execute):
        mock_query_execute.return_value = (["id"], [["1"]])
        resp = client.post("/query", headers=AUTH, json={"sql": "SELECT 1"})
        assert resp.status_code == 200, resp.text

    def test_valid_select_calls_execute_query(self, client, mock_query_execute):
        """execute_query must be called with a SQL string that still contains SELECT."""
        mock_query_execute.return_value = (["id"], [["1"]])
        client.post("/query", headers=AUTH, json={"sql": "SELECT 1"})
        mock_query_execute.assert_called_once()
        sql_arg = mock_query_execute.call_args[0][0]
        assert "SELECT" in sql_arg.upper()

    def test_limit_injected_in_sql_passed_to_execute(self, client, mock_query_execute):
        """When SQL has no LIMIT, validate_and_sanitize injects DEFAULT_LIMIT before calling CH."""
        mock_query_execute.return_value = (["id"], [["1"]])
        client.post("/query", headers=AUTH, json={"sql": "SELECT id FROM t"})
        sql_arg = mock_query_execute.call_args[0][0]
        assert "LIMIT" in sql_arg.upper()

    def test_caller_limit_overrides_default(self, client, mock_query_execute):
        """When body.limit is set, it overrides the injected default."""
        mock_query_execute.return_value = (["id"], [["1"]])
        client.post("/query", headers=AUTH, json={"sql": "SELECT id FROM t", "limit": 50})
        sql_arg = mock_query_execute.call_args[0][0]
        assert "LIMIT 50" in sql_arg

    def test_caller_limit_capped_at_max_response_rows(self, client, mock_query_execute):
        """body.limit larger than max_response_rows is capped."""
        os.environ["MAX_RESPONSE_ROWS"] = "100"
        get_settings.cache_clear()
        mock_query_execute.return_value = (["id"], [["1"]])
        client.post("/query", headers=AUTH, json={"sql": "SELECT id FROM t", "limit": 9999})
        sql_arg = mock_query_execute.call_args[0][0]
        # The effective limit must not exceed MAX_RESPONSE_ROWS=100
        assert "LIMIT 100" in sql_arg

    def test_response_shape_columns_rows_row_count_truncated(self, client, mock_query_execute):
        """Response must have {columns, rows, row_count, truncated} keys."""
        mock_query_execute.return_value = (["id", "name"], [["1", "Alice"]])
        resp = client.post("/query", headers=AUTH, json={"sql": "SELECT id, name FROM t LIMIT 1"})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"columns", "rows", "row_count", "truncated"}

    def test_response_columns_match_returned_columns(self, client, mock_query_execute):
        mock_query_execute.return_value = (["user_id", "email"], [["42", "alice@example.com"]])
        resp = client.post("/query", headers=AUTH, json={"sql": "SELECT user_id, email FROM users LIMIT 1"})
        body = resp.json()
        assert body["columns"] == ["user_id", "email"]

    def test_response_rows_match_returned_rows(self, client, mock_query_execute):
        mock_query_execute.return_value = (["id"], [["10"], ["20"]])
        resp = client.post("/query", headers=AUTH, json={"sql": "SELECT id FROM t LIMIT 2"})
        body = resp.json()
        assert body["rows"] == [["10"], ["20"]]

    def test_response_row_count_equals_len_rows(self, client, mock_query_execute):
        mock_query_execute.return_value = (["id"], [["1"], ["2"], ["3"]])
        resp = client.post("/query", headers=AUTH, json={"sql": "SELECT id FROM t LIMIT 3"})
        body = resp.json()
        assert body["row_count"] == 3
        assert body["row_count"] == len(body["rows"])

    def test_truncated_false_when_rows_within_cap(self, client, mock_query_execute):
        """truncated=False when row count is below MAX_RESPONSE_ROWS."""
        mock_query_execute.return_value = (["id"], [["1"], ["2"]])
        resp = client.post("/query", headers=AUTH, json={"sql": "SELECT id FROM t LIMIT 2"})
        body = resp.json()
        assert body["truncated"] is False


# ===========================================================================
# /query — Truncation behaviour
# ===========================================================================

class TestQueryTruncation:

    def test_truncated_true_when_rows_exceed_max_response_rows(self, client, mock_query_execute):
        """When execute_query returns more rows than MAX_RESPONSE_ROWS, truncated=True."""
        os.environ["MAX_RESPONSE_ROWS"] = "3"
        get_settings.cache_clear()

        # Return 5 rows; max_response_rows=3 → truncated=True, rows capped at 3
        rows = [[str(i)] for i in range(5)]
        mock_query_execute.return_value = (["id"], rows)

        resp = client.post("/query", headers=AUTH, json={"sql": "SELECT id FROM t LIMIT 5"})
        body = resp.json()
        assert body["truncated"] is True
        assert body["row_count"] == 3
        assert len(body["rows"]) == 3

    def test_truncated_rows_are_first_n_rows(self, client, mock_query_execute):
        """When truncated, the first MAX_RESPONSE_ROWS rows are returned (not the last)."""
        os.environ["MAX_RESPONSE_ROWS"] = "2"
        get_settings.cache_clear()

        rows = [["first"], ["second"], ["third"]]
        mock_query_execute.return_value = (["name"], rows)

        resp = client.post("/query", headers=AUTH, json={"sql": "SELECT name FROM t LIMIT 3"})
        body = resp.json()
        assert body["rows"] == [["first"], ["second"]]

    def test_exactly_max_response_rows_not_truncated(self, client, mock_query_execute):
        """Exactly MAX_RESPONSE_ROWS rows returned → truncated=False."""
        os.environ["MAX_RESPONSE_ROWS"] = "2"
        get_settings.cache_clear()

        mock_query_execute.return_value = (["id"], [["1"], ["2"]])

        resp = client.post("/query", headers=AUTH, json={"sql": "SELECT id FROM t LIMIT 2"})
        body = resp.json()
        assert body["truncated"] is False


# ===========================================================================
# /query/explain — POST
# ===========================================================================

class TestExplainRoute:

    def test_explain_returns_200_for_valid_sql(self, client, mock_query_execute):
        mock_query_execute.return_value = (["explain"], [["ReadFromStorage"]])
        resp = client.post("/query/explain", headers=AUTH, json={"sql": "SELECT 1"})
        assert resp.status_code == 200

    def test_explain_wraps_sql_in_explain(self, client, mock_query_execute):
        """execute_query must be called with 'EXPLAIN ...' prefix."""
        mock_query_execute.return_value = (["explain"], [["plan"]])
        client.post("/query/explain", headers=AUTH, json={"sql": "SELECT id FROM t"})
        mock_query_execute.assert_called_once()
        sql_arg = mock_query_execute.call_args[0][0]
        assert sql_arg.upper().startswith("EXPLAIN")

    def test_explain_rejects_bad_sql_with_400(self, client, mock_query_execute):
        """Inner SQL is still validated — mutation inside EXPLAIN body must be blocked."""
        resp = client.post("/query/explain", headers=AUTH, json={"sql": "DROP TABLE t"})
        assert resp.status_code == 400

    def test_explain_never_calls_execute_on_bad_inner_sql(self, client, mock_query_execute):
        client.post("/query/explain", headers=AUTH, json={"sql": "INSERT INTO t VALUES (1)"})
        mock_query_execute.assert_not_called()

    def test_explain_truncated_always_false(self, client, mock_query_execute):
        """EXPLAIN response always has truncated=False."""
        mock_query_execute.return_value = (["explain"], [["step1"], ["step2"]])
        resp = client.post("/query/explain", headers=AUTH, json={"sql": "SELECT 1"})
        assert resp.json()["truncated"] is False

    def test_explain_requires_auth(self, client, mock_query_execute):
        resp = client.post("/query/explain", json={"sql": "SELECT 1"})
        assert resp.status_code == 401


# ===========================================================================
# /databases — GET
# ===========================================================================

class TestDatabasesRoute:

    def test_databases_requires_auth(self, client, mock_schema_execute):
        resp = client.get("/databases")
        assert resp.status_code == 401

    def test_databases_returns_200_with_valid_auth(self, client, mock_schema_execute):
        # execute_query for list_databases returns single-element tuples: [(name,), ...]
        mock_schema_execute.return_value = (["name"], [["default"], ["analytics"]])
        resp = client.get("/databases", headers=AUTH)
        assert resp.status_code == 200, resp.text

    def test_databases_response_is_list(self, client, mock_schema_execute):
        mock_schema_execute.return_value = (["name"], [["default"]])
        resp = client.get("/databases", headers=AUTH)
        body = resp.json()
        assert isinstance(body, list)

    def test_databases_each_item_has_name_key(self, client, mock_schema_execute):
        mock_schema_execute.return_value = (["name"], [["db1"], ["db2"]])
        resp = client.get("/databases", headers=AUTH)
        for item in resp.json():
            assert "name" in item

    def test_databases_filtered_by_allowed_databases(self, client, mock_schema_execute):
        """When ALLOWED_DATABASES is set, only allowed DBs are returned."""
        os.environ["ALLOWED_DATABASES"] = "analytics"
        get_settings.cache_clear()

        mock_schema_execute.return_value = (["name"], [["default"], ["analytics"], ["system"]])
        resp = client.get("/databases", headers=AUTH)
        names = [item["name"] for item in resp.json()]
        assert names == ["analytics"]


# ===========================================================================
# /tables — GET
# ===========================================================================

class TestTablesRoute:

    def test_tables_requires_auth(self, client, mock_schema_execute):
        resp = client.get("/tables", params={"database": "default"})
        assert resp.status_code == 401

    def test_tables_with_valid_auth_returns_200(self, client, mock_schema_execute):
        # list_tables expects rows of (database, name, engine)
        mock_schema_execute.return_value = (
            ["database", "name", "engine"],
            [["default", "events", "MergeTree"]],
        )
        resp = client.get("/tables", headers=AUTH, params={"database": "default"})
        assert resp.status_code == 200, resp.text

    def test_tables_response_shape(self, client, mock_schema_execute):
        mock_schema_execute.return_value = (
            ["database", "name", "engine"],
            [["default", "events", "MergeTree"]],
        )
        resp = client.get("/tables", headers=AUTH, params={"database": "default"})
        items = resp.json()
        assert len(items) > 0
        item = items[0]
        assert {"database", "name", "engine"} <= set(item.keys())

    def test_tables_forbidden_when_database_not_in_allowlist(self, client, mock_schema_execute):
        """Access to a database not in ALLOWED_DATABASES must return 403."""
        os.environ["ALLOWED_DATABASES"] = "analytics"
        get_settings.cache_clear()

        resp = client.get("/tables", headers=AUTH, params={"database": "secret_db"})
        assert resp.status_code == 403


# ===========================================================================
# RESTRICT_TO_CATALOGUED_TABLES — only tables in the semantic catalog are exposed
# ===========================================================================

class TestCataloguedTableRestriction:
    """When RESTRICT_TO_CATALOGUED_TABLES is on, only tables present in the
    semantic catalog (the 'schema dictionary') may be listed/described/sampled.

    The real catalog contains dbpcm_warehouse.employee (catalogued) and does NOT
    contain dbpcm_warehouse.ghost_table (uncatalogued) — see
    app/semantic_catalog/data/*.yaml.
    """

    _DB = "dbpcm_warehouse"
    _CATALOGUED = "employee"
    _UNCATALOGUED = "ghost_table"

    @pytest.fixture(autouse=True)
    def _enable_restriction(self):
        os.environ["RESTRICT_TO_CATALOGUED_TABLES"] = "true"
        get_settings.cache_clear()
        yield
        os.environ.pop("RESTRICT_TO_CATALOGUED_TABLES", None)
        get_settings.cache_clear()

    def test_list_tables_hides_uncatalogued(self, client, mock_schema_execute):
        mock_schema_execute.return_value = (
            ["database", "name", "engine"],
            [
                [self._DB, self._CATALOGUED, "MergeTree"],
                [self._DB, self._UNCATALOGUED, "MergeTree"],
            ],
        )
        resp = client.get("/tables", headers=AUTH, params={"database": self._DB})
        assert resp.status_code == 200, resp.text
        names = [t["name"] for t in resp.json()]
        assert names == [self._CATALOGUED]

    def test_schema_forbidden_for_uncatalogued(self, client, mock_schema_execute):
        resp = client.get(
            "/tables/schema",
            headers=AUTH,
            params={"database": self._DB, "table": self._UNCATALOGUED},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "TABLE_NOT_ALLOWED"

    def test_sample_forbidden_for_uncatalogued(self, client, mock_schema_execute):
        resp = client.get(
            "/tables/sample",
            headers=AUTH,
            params={"database": self._DB, "table": self._UNCATALOGUED},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "TABLE_NOT_ALLOWED"

    def test_schema_allowed_for_catalogued(self, client, mock_schema_execute):
        mock_schema_execute.return_value = (
            ["name", "type", "comment"],
            [["employee_id", "UInt64", ""]],
        )
        resp = client.get(
            "/tables/schema",
            headers=AUTH,
            params={"database": self._DB, "table": self._CATALOGUED},
        )
        assert resp.status_code == 200, resp.text

    def test_uncatalogued_accessible_when_restriction_off(self, client, mock_schema_execute):
        """Regression guard: with the flag OFF (default), uncatalogued tables are
        still reachable — the feature must be strictly opt-in."""
        os.environ["RESTRICT_TO_CATALOGUED_TABLES"] = "false"
        get_settings.cache_clear()
        mock_schema_execute.return_value = (
            ["name", "type", "comment"],
            [["id", "UInt64", ""]],
        )
        resp = client.get(
            "/tables/schema",
            headers=AUTH,
            params={"database": self._DB, "table": self._UNCATALOGUED},
        )
        assert resp.status_code == 200, resp.text

    # --- runQuery / explainQuery enforcement -------------------------------

    def test_run_query_forbidden_for_uncatalogued(self, client, mock_query_execute):
        resp = client.post(
            "/query", headers=AUTH, json={"sql": "SELECT * FROM ghost_table"}
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "TABLE_NOT_ALLOWED"
        # Fail-closed BEFORE ClickHouse is ever hit.
        mock_query_execute.assert_not_called()

    def test_run_query_forbidden_for_uncatalogued_in_subquery(self, client, mock_query_execute):
        """A nested/subquery reference to an uncatalogued table is still caught."""
        resp = client.post(
            "/query",
            headers=AUTH,
            json={"sql": "SELECT count() FROM (SELECT * FROM ghost2) t"},
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "TABLE_NOT_ALLOWED"
        mock_query_execute.assert_not_called()

    def test_run_query_allowed_for_catalogued(self, client, mock_query_execute):
        mock_query_execute.return_value = (["employee_id"], [["1"]])
        resp = client.post(
            "/query",
            headers=AUTH,
            json={"sql": "SELECT employee_id FROM dbpcm_warehouse.employee"},
        )
        assert resp.status_code == 200, resp.text
        mock_query_execute.assert_called_once()

    def test_explain_query_forbidden_for_uncatalogued(self, client, mock_query_execute):
        resp = client.post(
            "/query/explain", headers=AUTH, json={"sql": "SELECT * FROM ghost_table"}
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "TABLE_NOT_ALLOWED"
        mock_query_execute.assert_not_called()

    def test_run_query_uncatalogued_allowed_when_restriction_off(self, client, mock_query_execute):
        """Regression guard: runQuery is unaffected when the flag is OFF."""
        os.environ["RESTRICT_TO_CATALOGUED_TABLES"] = "false"
        get_settings.cache_clear()
        mock_query_execute.return_value = (["id"], [["1"]])
        resp = client.post(
            "/query", headers=AUTH, json={"sql": "SELECT * FROM ghost_table"}
        )
        assert resp.status_code == 200, resp.text
        mock_query_execute.assert_called_once()


# ===========================================================================
# /tables/schema — GET
# ===========================================================================

class TestTableSchemaRoute:

    def test_schema_requires_auth(self, client, mock_schema_execute):
        resp = client.get("/tables/schema", params={"database": "default", "table": "events"})
        assert resp.status_code == 401

    def test_schema_returns_200_with_valid_auth(self, client, mock_schema_execute):
        # get_table_schema expects rows of (name, type, comment)
        mock_schema_execute.return_value = (
            ["name", "type", "comment"],
            [["id", "UInt64", ""], ["name", "String", "User name"]],
        )
        resp = client.get(
            "/tables/schema",
            headers=AUTH,
            params={"database": "default", "table": "users"},
        )
        assert resp.status_code == 200

    def test_schema_response_shape(self, client, mock_schema_execute):
        mock_schema_execute.return_value = (
            ["name", "type", "comment"],
            [["id", "UInt64", ""], ["email", "String", ""]],
        )
        resp = client.get(
            "/tables/schema",
            headers=AUTH,
            params={"database": "default", "table": "users"},
        )
        body = resp.json()
        assert "database" in body
        assert "table" in body
        assert "columns" in body
        assert isinstance(body["columns"], list)
        for col in body["columns"]:
            assert {"name", "type", "comment"} <= set(col.keys())

    def test_schema_404_when_table_has_no_columns(self, client, mock_schema_execute):
        """Empty rows means table not found → 404."""
        mock_schema_execute.return_value = (["name", "type", "comment"], [])

        resp = client.get(
            "/tables/schema",
            headers=AUTH,
            params={"database": "default", "table": "nonexistent"},
        )
        assert resp.status_code == 404

    def test_schema_forbidden_for_disallowed_database(self, client, mock_schema_execute):
        os.environ["ALLOWED_DATABASES"] = "analytics"
        get_settings.cache_clear()

        resp = client.get(
            "/tables/schema",
            headers=AUTH,
            params={"database": "secret_db", "table": "users"},
        )
        assert resp.status_code == 403


# ===========================================================================
# /tables/sample — GET
# Uses execute_query bound in schema router
# ===========================================================================

class TestSampleRowsRoute:

    def test_sample_requires_auth(self, client, mock_schema_execute):
        resp = client.get("/tables/sample", params={"database": "default", "table": "t"})
        assert resp.status_code == 401

    def test_sample_returns_200(self, client, mock_schema_execute):
        mock_schema_execute.return_value = (["id", "name"], [["1", "Alice"]])
        resp = client.get(
            "/tables/sample",
            headers=AUTH,
            params={"database": "default", "table": "events"},
        )
        assert resp.status_code == 200

    def test_sample_response_shape(self, client, mock_schema_execute):
        mock_schema_execute.return_value = (["id"], [["1"]])
        resp = client.get(
            "/tables/sample",
            headers=AUTH,
            params={"database": "default", "table": "t"},
        )
        body = resp.json()
        assert set(body.keys()) == {"columns", "rows", "row_count", "truncated"}
        assert body["truncated"] is False  # sample never truncates

    def test_sample_default_limit_is_5(self, client, mock_schema_execute):
        """Default sample size is 5 — verify the SQL sent to execute_query contains LIMIT 5."""
        mock_schema_execute.return_value = (["id"], [["1"]])
        client.get(
            "/tables/sample",
            headers=AUTH,
            params={"database": "default", "table": "t"},
        )
        mock_schema_execute.assert_called_once()
        sql_arg = mock_schema_execute.call_args[0][0]
        assert "LIMIT 5" in sql_arg

    def test_sample_custom_limit(self, client, mock_schema_execute):
        mock_schema_execute.return_value = (["id"], [["1"]] * 10)
        client.get(
            "/tables/sample",
            headers=AUTH,
            params={"database": "default", "table": "t", "limit": "10"},
        )
        mock_schema_execute.assert_called_once()
        sql_arg = mock_schema_execute.call_args[0][0]
        assert "LIMIT 10" in sql_arg

    def test_sample_forbidden_for_disallowed_database(self, client, mock_schema_execute):
        os.environ["ALLOWED_DATABASES"] = "analytics"
        get_settings.cache_clear()

        resp = client.get(
            "/tables/sample",
            headers=AUTH,
            params={"database": "secret_db", "table": "t"},
        )
        assert resp.status_code == 403


# ===========================================================================
# /health — GET (no auth)
# ===========================================================================

class TestHealthRoute:

    def test_health_returns_200_no_auth(self, client):
        with patch("app.routers.health.ping", return_value=True):
            resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_body_when_ch_ok(self, client):
        with patch("app.routers.health.ping", return_value=True):
            resp = client.get("/health")
        body = resp.json()
        assert body["status"] == "ok"
        assert body["clickhouse"] == "ok"

    def test_health_body_when_ch_down(self, client):
        with patch("app.routers.health.ping", return_value=False):
            resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["clickhouse"] == "error"

    def test_health_no_auth_required_even_with_wrong_key(self, client):
        """Health must respond 200 regardless of Authorization header."""
        with patch("app.routers.health.ping", return_value=True):
            resp = client.get("/health", headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 200

    def test_health_check_reuses_cached_client(self):
        """health_check() must call ping() with NO arguments.

        Passing an explicit settings object routes through get_client(settings)
        -> _build_client, which constructs (and logs) a brand-new ClickHouse
        client on every probe instead of reusing the lru_cache singleton.
        """
        from app.routers.health import health_check

        with patch("app.routers.health.ping", return_value=True) as mock_ping:
            health_check()

        mock_ping.assert_called_once_with()
        assert mock_ping.call_args.args == ()
        assert mock_ping.call_args.kwargs == {}


# ===========================================================================
# Schema routes — ClickHouse domain errors map to clean HTTP codes (not 500)
# ===========================================================================

class TestSchemaRouteErrorTranslation:
    """Regression: schema routes let ClickHouse domain errors propagate; the
    app-level handler must translate them (was a 500 on /databases)."""

    def test_databases_clickhouse_query_error_returns_400(self, client, mock_execute):
        from fastapi import HTTPException

        mock_execute.side_effect = HTTPException(
            status_code=400, detail={"error": "boom", "code": "CLICKHOUSE_QUERY_ERROR"}
        )
        resp = client.get("/databases", headers=AUTH)
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "CLICKHOUSE_QUERY_ERROR"

    def test_databases_clickhouse_unavailable_returns_502(self, client, mock_execute):
        from fastapi import HTTPException

        mock_execute.side_effect = HTTPException(
            status_code=502, detail={"error": "down", "code": "CLICKHOUSE_UNAVAILABLE"}
        )
        resp = client.get("/databases", headers=AUTH)
        assert resp.status_code == 502, resp.text
        assert resp.json()["code"] == "CLICKHOUSE_UNAVAILABLE"
