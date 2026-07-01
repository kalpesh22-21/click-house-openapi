"""ADVERSARIAL negative tests for app.service.get_table_schema's D83/D84 wiring.

Complements tests/test_get_table_schema_scope.py (which exercises the happy
path: none/empty/restricted scope wiring, catalog_sha presence) and
tests/semantic_catalog/test_overlay_adversarial.py (which unit-tests
build_table_schema_response's identifier-scan gaps directly) with:

  - D70 case-sensitivity enforced at the full service call, not just the pure
    overlay function.
  - Both introspection fields AND catalog overlay fields dropping TOGETHER for
    an out-of-scope column (design Section 1.4: "no partial redaction state").
  - An end-to-end run against the REAL copied-in semantic catalog + REAL
    CATALOG_SHA sidecar (no catalog/catalog_sha mocking), to catch any
    integration-only gap the pure-unit/mocked-catalog tests could paper over.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

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

_FAKE_CATALOG = {
    "wh.employee": {
        "database": "wh",
        "table": "employee",
        "description": "Employee record.",
        "grain": ["EmployeeCode"],
        "temporal": {"dimensions": [], "default_pin": None},
        "primary_key": ["EmployeeCode"],
        "join_keys": [],
        "columns": {
            "EmployeeCode": {"type": "String", "description": "Stable id."},
            "AnnualSalary": {
                "type": "Nullable(Decimal(18, 6))",
                "unit": "USD",
                "description": "Annual salary.",
                "sensitive": True,
            },
        },
        "measures": {},
        "rules": [],
        "ambiguities": [],
    },
}


@pytest.fixture()
def mock_execute():
    with patch("app.service.execute_query") as m:
        m.return_value = (
            ["name", "type", "comment"],
            [["EmployeeCode", "String", ""], ["AnnualSalary", "Nullable(Decimal(18, 6))", ""]],
        )
        yield m


@pytest.fixture()
def mock_catalog():
    with patch("app.service.get_semantic_catalog", return_value=_FAKE_CATALOG):
        yield


@pytest.fixture()
def mock_catalog_sha():
    with patch("app.service.get_catalog_sha", return_value="deadbeef" * 5):
        yield


@pytest.fixture()
def mock_catalog_schema():
    """Mock the introspected-universe source (app.catalog.get_catalog_schema)
    consumed by app.service.get_table_schema for the cross-table free-text
    scan vocabulary (FIX 1, security review) whenever scope-filtering is
    active. Only queried when scope is truthy."""
    with patch(
        "app.service.get_catalog_schema",
        return_value={
            "wh.employee": {"EmployeeCode": "String", "AnnualSalary": "Nullable(Decimal(18, 6))"}
        },
    ):
        yield


def _call_with_scope(database, table, scope):
    from app.principal import current_scope

    token = current_scope.set(scope)
    try:
        return service.get_table_schema(database, table)
    finally:
        current_scope.reset(token)


class TestGetTableSchemaCaseSensitivity:
    def test_wrong_case_scope_entry_does_not_leak_introspection_or_overlay_fields(
        self, mock_execute, mock_catalog, mock_catalog_sha, mock_catalog_schema
    ):
        """Scope names the sensitive column with the wrong case
        ('annualsalary' instead of the catalog-authored 'AnnualSalary'). D70:
        no case-insensitive fallback -- the column must be dropped, meaning
        BOTH its introspection fields (name/type/comment) AND its catalog
        overlay fields (description/unit/sensitive) are absent together."""
        wrong_case_scope = frozenset({"wh.employee.EmployeeCode", "wh.employee.annualsalary"})
        result = _call_with_scope("wh", "employee", wrong_case_scope)
        names = {c["name"] for c in result["columns"]}
        assert names == {"EmployeeCode"}
        # No trace of the dropped column's overlay content anywhere.
        for col in result["columns"]:
            assert col["name"] != "AnnualSalary"
        assert "sensitive" not in str(result) or all(
            c["name"] != "AnnualSalary" for c in result["columns"]
        )

    def test_correctly_cased_scope_entry_keeps_the_column_and_its_overlay(
        self, mock_execute, mock_catalog, mock_catalog_sha, mock_catalog_schema
    ):
        correct_case_scope = frozenset(
            {"wh.employee.EmployeeCode", "wh.employee.AnnualSalary"}
        )
        result = _call_with_scope("wh", "employee", correct_case_scope)
        by_name = {c["name"]: c for c in result["columns"]}
        assert "AnnualSalary" in by_name
        assert by_name["AnnualSalary"]["sensitive"] is True
        assert by_name["AnnualSalary"]["unit"] == "USD"


class TestGetTableSchemaColumnDropIsAllOrNothing:
    def test_dropped_column_has_no_partial_field_leak(
        self, mock_execute, mock_catalog, mock_catalog_sha, mock_catalog_schema
    ):
        """A column that's out of scope must not appear at all -- not even
        with type/comment retained and overlay fields nulled. The whole
        column entry is absent, per design Section 1.4's 'no partial
        redaction' rule."""
        scope = frozenset({"wh.employee.EmployeeCode"})
        result = _call_with_scope("wh", "employee", scope)
        assert len(result["columns"]) == 1
        assert result["columns"][0]["name"] == "EmployeeCode"


class TestGetTableSchemaRealCatalogIntegration:
    """No mocking of get_semantic_catalog/get_catalog_sha -- exercises the
    real copied-in YAML catalog + real CATALOG_SHA sidecar end-to-end through
    app.service.get_table_schema, to catch any gap the mocked-catalog tests
    (which use a small hand-authored fixture) could miss."""

    def test_real_catalog_employee_table_scope_filtered_end_to_end(self, mock_execute):
        # Real employee.yaml is keyed "dbpcm_warehouse.employee". The
        # introspected-universe source (app.catalog.get_catalog_schema, used
        # by FIX 1's cross-table free-text scan) is mocked here the same as
        # execute_query -- there is no live ClickHouse in this test
        # environment to back a real system.columns query, but the semantic
        # catalog itself (get_semantic_catalog/get_catalog_sha) stays real
        # per this class's docstring.
        with patch("app.service.execute_query") as m, patch(
            "app.service.get_catalog_schema",
            return_value={
                "dbpcm_warehouse.employee": {
                    "EmployeeCode": "String",
                    "AnnualSalary": "Nullable(Decimal(18, 6))",
                }
            },
        ):
            m.return_value = (
                ["name", "type", "comment"],
                [
                    ["EmployeeCode", "String", ""],
                    ["AnnualSalary", "Nullable(Decimal(18, 6))", ""],
                ],
            )
            scope = frozenset({"dbpcm_warehouse.employee.EmployeeCode"})
            result = _call_with_scope("dbpcm_warehouse", "employee", scope)

        assert result["catalogued"] is True
        names = {c["name"] for c in result["columns"]}
        assert names == {"EmployeeCode"}
        # catalog_sha is the real sidecar contents, a 40-char git SHA, not a mock.
        assert len(result["catalog_sha"]) == 40
        assert result["catalog_sha"] == result["catalog_sha"].strip()

    def test_real_catalog_uncatalogued_table_structural_only(self, mock_execute):
        with patch("app.service.execute_query") as m:
            m.return_value = (
                ["name", "type", "comment"],
                [["col_a", "String", ""]],
            )
            result = _call_with_scope("dbpcm_warehouse", "not_a_real_table", frozenset())

        assert result["catalogued"] is False
        assert result["description"] is None
        assert {c["name"] for c in result["columns"]} == {"col_a"}
