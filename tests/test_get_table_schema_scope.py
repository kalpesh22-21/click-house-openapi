"""Service-level tests for app.service.get_table_schema's D83/D84 overlay + scope-filter
wiring (as opposed to tests/semantic_catalog/test_overlay.py, which unit-tests the pure
merge/filter function directly).

Mocks execute_query (introspection) and app.service.get_semantic_catalog /
app.service.get_catalog_sha so the whole pipeline runs in-process without a live
ClickHouse or the real copied-in YAML files.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Ensure env vars are set before any app module loads (matches conftest pattern)
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


class TestGetTableSchemaCatalogSha:
    def test_catalog_sha_present(self, mock_execute, mock_catalog, mock_catalog_sha):
        result = _call_with_scope("wh", "employee", None)
        assert result["catalog_sha"] == "deadbeef" * 5


class TestGetTableSchemaScopeWiring:
    def test_none_scope_no_filtering(self, mock_execute, mock_catalog, mock_catalog_sha):
        result = _call_with_scope("wh", "employee", None)
        names = {c["name"] for c in result["columns"]}
        assert names == {"EmployeeCode", "AnnualSalary"}
        assert result["catalogued"] is True

    def test_empty_scope_no_filtering(self, mock_execute, mock_catalog, mock_catalog_sha):
        result = _call_with_scope("wh", "employee", frozenset())
        names = {c["name"] for c in result["columns"]}
        assert names == {"EmployeeCode", "AnnualSalary"}

    def test_restricted_scope_drops_out_of_scope_column(
        self, mock_execute, mock_catalog, mock_catalog_sha, mock_catalog_schema
    ):
        scope = frozenset({"wh.employee.EmployeeCode"})
        result = _call_with_scope("wh", "employee", scope)
        names = {c["name"] for c in result["columns"]}
        assert names == {"EmployeeCode"}
        # AnnualSalary's sensitive/description overlay disappears WITH the column
        # (no separate leak path, per mcp-overlay-design.md OQ-5(2)).
        assert all(c["name"] != "AnnualSalary" for c in result["columns"])
