"""Integration tests for the live REST API at IT_API_BASE.

Every test is marked ``@pytest.mark.integration`` and auto-skips unless
both the RUN_INTEGRATION env var is set and the stack is reachable
(gating logic in conftest.py).

Coverage
--------
- /health — no auth required, returns correct shape
- Auth: 401 on missing token, 401 on wrong token, 200 on correct token
- /databases — contains it_integration
- /tables — lists orders + customers for it_integration
- /tables/schema — exact column names and types for orders
- /tables/sample — limit parameter caps rows returned
- /query — aggregate == ground truth, body limit caps, auto-LIMIT injection
- /query/explain — returns plan rows (contains "ReadFromMergeTree"), does not execute
- Guardrails — INSERT, url(), multi-statement, DROP, SET all return 400 with correct codes
- Unknown table — returns 400 with code CLICKHOUSE_QUERY_ERROR containing UNKNOWN_TABLE
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import IT_API_BASE, IT_API_KEY


# ---------------------------------------------------------------------------
# /health — no auth
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestHealthEndpoint:

    def test_health_returns_200_without_auth(self, seed: dict, api) -> None:
        """GET /health must respond 200 with no Authorization header."""
        import httpx
        resp = httpx.get(f"{IT_API_BASE}/health", timeout=5.0)
        assert resp.status_code == 200

    def test_health_body_shape(self, seed: dict, api) -> None:
        import httpx
        resp = httpx.get(f"{IT_API_BASE}/health", timeout=5.0)
        body = resp.json()
        assert body.get("status") == "ok"
        assert body.get("clickhouse") == "ok"


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAuthEnforcement:

    def test_missing_auth_returns_401(self, seed: dict) -> None:
        import httpx
        resp = httpx.get(f"{IT_API_BASE}/databases", timeout=5.0)
        assert resp.status_code == 401

    def test_wrong_token_returns_401(self, seed: dict) -> None:
        import httpx
        resp = httpx.get(
            f"{IT_API_BASE}/databases",
            headers={"Authorization": "Bearer totally-wrong-key"},
            timeout=5.0,
        )
        assert resp.status_code == 401

    def test_correct_token_returns_200(self, seed: dict, api) -> None:
        resp = api.get("/databases")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /databases
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDatabasesEndpoint:

    def test_databases_contains_it_integration(self, seed: dict, api) -> None:
        resp = api.get("/databases")
        assert resp.status_code == 200
        names = [item["name"] for item in resp.json()]
        assert seed["database"] in names

    def test_databases_response_is_list_of_name_dicts(self, seed: dict, api) -> None:
        resp = api.get("/databases")
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) > 0
        for item in body:
            assert "name" in item, f"Missing 'name' key in {item!r}"


# ---------------------------------------------------------------------------
# /tables
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTablesEndpoint:

    def test_tables_lists_orders_and_customers(self, seed: dict, api) -> None:
        resp = api.get("/tables", params={"database": seed["database"]})
        assert resp.status_code == 200
        table_names = {item["name"] for item in resp.json()}
        assert seed["orders_table"] in table_names
        assert seed["customers_table"] in table_names

    def test_tables_response_shape(self, seed: dict, api) -> None:
        resp = api.get("/tables", params={"database": seed["database"]})
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) >= 2
        for item in items:
            assert {"database", "name", "engine"} <= set(item.keys())
            assert item["database"] == seed["database"]


# ---------------------------------------------------------------------------
# /tables/schema
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTableSchemaEndpoint:

    def test_orders_schema_returns_correct_columns(self, seed: dict, api) -> None:
        resp = api.get(
            "/tables/schema",
            params={"database": seed["database"], "table": seed["orders_table"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["database"] == seed["database"]
        assert body["table"] == seed["orders_table"]
        assert isinstance(body["columns"], list)

        col_map = {c["name"]: c["type"] for c in body["columns"]}
        for expected in seed["orders_columns"]:
            assert expected["name"] in col_map, (
                f"Column '{expected['name']}' not found; got {list(col_map)}"
            )
            assert col_map[expected["name"]] == expected["type"], (
                f"Column '{expected['name']}': expected type '{expected['type']}', "
                f"got '{col_map[expected['name']]}'"
            )

    def test_orders_schema_column_count(self, seed: dict, api) -> None:
        resp = api.get(
            "/tables/schema",
            params={"database": seed["database"], "table": seed["orders_table"]},
        )
        body = resp.json()
        assert len(body["columns"]) == len(seed["orders_columns"])

    def test_schema_columns_have_name_type_comment_keys(self, seed: dict, api) -> None:
        resp = api.get(
            "/tables/schema",
            params={"database": seed["database"], "table": seed["orders_table"]},
        )
        for col in resp.json()["columns"]:
            assert {"name", "type", "comment"} <= set(col.keys())


# ---------------------------------------------------------------------------
# /tables/sample
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSampleRowsEndpoint:

    def test_sample_returns_correct_shape(self, seed: dict, api) -> None:
        resp = api.get(
            "/tables/sample",
            params={"database": seed["database"], "table": seed["orders_table"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert {"columns", "rows", "row_count", "truncated"} == set(body.keys())

    def test_sample_default_limit_is_5_rows(self, seed: dict, api) -> None:
        """Default sample returns at most 5 rows from the 6-row table."""
        resp = api.get(
            "/tables/sample",
            params={"database": seed["database"], "table": seed["orders_table"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] <= 5
        assert len(body["rows"]) == body["row_count"]

    def test_sample_custom_limit_caps_rows(self, seed: dict, api) -> None:
        """Requesting limit=2 returns at most 2 rows."""
        resp = api.get(
            "/tables/sample",
            params={"database": seed["database"], "table": seed["orders_table"], "limit": 2},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] <= 2

    def test_sample_includes_expected_columns(self, seed: dict, api) -> None:
        resp = api.get(
            "/tables/sample",
            params={"database": seed["database"], "table": seed["orders_table"]},
        )
        body = resp.json()
        expected_cols = {c["name"] for c in seed["orders_columns"]}
        actual_cols = set(body["columns"])
        assert expected_cols <= actual_cols


# ---------------------------------------------------------------------------
# /query
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestQueryEndpoint:

    def test_count_aggregate_equals_ground_truth(self, seed: dict, api) -> None:
        resp = api.post(
            "/query",
            json={"sql": f"SELECT count() as cnt FROM {seed['database']}.{seed['orders_table']}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["columns"] == ["cnt"]
        assert body["rows"][0][0] == seed["orders_count"]

    def test_sum_amount_equals_ground_truth(self, seed: dict, api) -> None:
        # Use the raw aggregate — the API serializes Decimal(10,2) as a string
        # with 2 decimal places (e.g. "605.50"), which matches GROUND_TRUTH exactly.
        # Avoid toString() which strips trailing zeros in ClickHouse.
        resp = api.post(
            "/query",
            json={
                "sql": (
                    f"SELECT sum(amount) as total "
                    f"FROM {seed['database']}.{seed['orders_table']}"
                )
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows"][0][0] == seed["orders_sum_amount"], (
            f"Expected sum={seed['orders_sum_amount']!r}, got {body['rows'][0][0]!r}"
        )

    def test_max_amount_equals_ground_truth(self, seed: dict, api) -> None:
        # Use the raw aggregate — Decimal(10,2) is serialized as "200.00".
        resp = api.post(
            "/query",
            json={
                "sql": (
                    f"SELECT max(amount) as mx "
                    f"FROM {seed['database']}.{seed['orders_table']}"
                )
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["rows"][0][0] == seed["orders_max_amount"]

    def test_group_by_country_returns_distinct_countries(self, seed: dict, api) -> None:
        resp = api.post(
            "/query",
            json={
                "sql": (
                    f"SELECT country, count() as cnt "
                    f"FROM {seed['database']}.{seed['orders_table']} "
                    f"GROUP BY country ORDER BY country"
                )
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        returned_countries = {row[0] for row in body["rows"]}
        assert returned_countries == seed["orders_countries"]

    def test_auto_limit_is_injected(self, seed: dict, api) -> None:
        """A query with no LIMIT clause should succeed and be capped by the API."""
        resp = api.post(
            "/query",
            json={"sql": f"SELECT order_id FROM {seed['database']}.{seed['orders_table']}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        # All 6 rows fit within the default limit
        assert body["row_count"] == seed["orders_count"]

    def test_body_limit_caps_rows(self, seed: dict, api) -> None:
        """Supplying limit=2 in the body caps the result to 2 rows."""
        resp = api.post(
            "/query",
            json={
                "sql": f"SELECT order_id FROM {seed['database']}.{seed['orders_table']}",
                "limit": 2,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["row_count"] == 2
        assert len(body["rows"]) == 2

    def test_response_has_correct_shape(self, seed: dict, api) -> None:
        resp = api.post(
            "/query",
            json={"sql": f"SELECT order_id FROM {seed['database']}.{seed['orders_table']} LIMIT 1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"columns", "rows", "row_count", "truncated"}
        assert body["row_count"] == len(body["rows"])
        assert isinstance(body["truncated"], bool)


# ---------------------------------------------------------------------------
# /query/explain
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestExplainEndpoint:

    def test_explain_returns_200(self, seed: dict, api) -> None:
        resp = api.post(
            "/query/explain",
            json={"sql": f"SELECT * FROM {seed['database']}.{seed['orders_table']} LIMIT 5"},
        )
        assert resp.status_code == 200

    def test_explain_plan_contains_read_from_merge_tree(self, seed: dict, api) -> None:
        resp = api.post(
            "/query/explain",
            json={"sql": f"SELECT * FROM {seed['database']}.{seed['orders_table']} LIMIT 5"},
        )
        body = resp.json()
        all_text = " ".join(str(row[0]) for row in body["rows"])
        assert "ReadFromMergeTree" in all_text, (
            f"Expected 'ReadFromMergeTree' in EXPLAIN output; got: {all_text!r}"
        )

    def test_explain_truncated_always_false(self, seed: dict, api) -> None:
        resp = api.post(
            "/query/explain",
            json={"sql": f"SELECT * FROM {seed['database']}.{seed['orders_table']} LIMIT 5"},
        )
        assert resp.json()["truncated"] is False

    def test_explain_does_not_modify_row_count(self, seed: dict, api) -> None:
        """EXPLAIN should not insert or delete rows — count should still equal ground truth."""
        resp = api.post(
            "/query/explain",
            json={"sql": f"SELECT * FROM {seed['database']}.{seed['orders_table']}"},
        )
        assert resp.status_code == 200
        # Then verify the actual data is untouched
        resp2 = api.post(
            "/query",
            json={"sql": f"SELECT count() FROM {seed['database']}.{seed['orders_table']}"},
        )
        assert resp2.json()["rows"][0][0] == seed["orders_count"]


# ---------------------------------------------------------------------------
# Guardrail rejections — all must return HTTP 400 with the correct code
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGuardrailRejections:

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
    def test_guardrail_returns_400_with_code(
        self, seed: dict, api, bad_sql: str, expected_code: str
    ) -> None:
        resp = api.post("/query", json={"sql": bad_sql})
        assert resp.status_code == 400, (
            f"Expected 400 for {bad_sql!r}, got {resp.status_code}: {resp.text}"
        )
        detail = resp.json().get("detail", {})
        assert detail.get("code") == expected_code, (
            f"Expected code={expected_code!r}, got {detail!r}"
        )


# ---------------------------------------------------------------------------
# Unknown table — CH query error
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestUnknownTableError:

    def test_unknown_table_returns_400_clickhouse_query_error(self, seed: dict, api) -> None:
        resp = api.post(
            "/query",
            json={"sql": f"SELECT * FROM {seed['database']}.no_such_table_xyz_9999"},
        )
        assert resp.status_code == 400
        detail = resp.json().get("detail", {})
        assert detail.get("code") == "CLICKHOUSE_QUERY_ERROR"

    def test_unknown_table_error_message_contains_unknown_table(self, seed: dict, api) -> None:
        resp = api.post(
            "/query",
            json={"sql": f"SELECT * FROM {seed['database']}.no_such_table_xyz_9999"},
        )
        error_msg = resp.json()["detail"].get("error", "")
        assert "UNKNOWN_TABLE" in error_msg, (
            f"Expected UNKNOWN_TABLE in error message; got: {error_msg!r}"
        )

    def test_unknown_table_host_port_redacted(self, seed: dict, api) -> None:
        """ClickHouse host:port must be redacted to <host>:<port> in the response."""
        resp = api.post(
            "/query",
            json={"sql": f"SELECT * FROM {seed['database']}.no_such_table_xyz_9999"},
        )
        error_msg = resp.json()["detail"].get("error", "")
        # The host should be redacted, not exposed
        assert "<host>:<port>" in error_msg or "localhost" not in error_msg, (
            f"Host may not be redacted: {error_msg!r}"
        )
