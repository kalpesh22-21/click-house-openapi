"""Service- and MCP-level tests for the M3 `columns` narrowing argument on
getTableSchema (as opposed to tests/semantic_catalog/test_overlay_column_filter.py,
which unit-tests the pure filter semantics inside build_table_schema_response).

Covers the argument-validation boundary (the caps that bound a caller-supplied,
echoed-back list), the service -> overlay wiring, and the MCP tool surface.
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
from app.errors import ArgumentValidationError  # noqa: E402

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

_SCOPE_WITHOUT_SALARY = frozenset({"wh.employee.EmployeeCode"})


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
    with patch(
        "app.service.get_catalog_schema",
        return_value={
            "wh.employee": {"EmployeeCode": "String", "AnnualSalary": "Nullable(Decimal(18, 6))"}
        },
    ):
        yield


@pytest.fixture()
def wired(mock_execute, mock_catalog, mock_catalog_sha, mock_catalog_schema):
    """The whole get_table_schema pipeline, mocked end to end."""
    yield mock_execute


def _call(columns=None, scope=None):
    from app.principal import current_scope

    token = current_scope.set(scope)
    try:
        return service.get_table_schema("wh", "employee", columns=columns)
    finally:
        current_scope.reset(token)


def _names(result):
    return [c["name"] for c in result["columns"]]


# ===========================================================================
# Argument validation — the caps on a caller-supplied, echoed-back list
# ===========================================================================

class TestColumnsArgumentValidation:
    def test_oversized_list_rejected_cleanly(self, wired):
        with pytest.raises(ArgumentValidationError) as exc_info:
            _call(columns=[f"c{i}" for i in range(65)])
        assert exc_info.value.code == "INVALID_ARGUMENT"
        assert "65" in exc_info.value.message

    def test_list_at_the_cap_is_accepted(self, wired):
        result = _call(columns=[f"c{i}" for i in range(63)] + ["EmployeeCode"])
        assert _names(result) == ["EmployeeCode"]

    @pytest.mark.parametrize("bad", [1, None, True, 3.5, ["nested"], {"a": 1}, b"bytes"])
    def test_non_string_entry_rejected(self, wired, bad):
        with pytest.raises(ArgumentValidationError) as exc_info:
            _call(columns=["EmployeeCode", bad])
        assert exc_info.value.code == "INVALID_ARGUMENT"

    def test_bare_string_rejected_rather_than_iterated_per_character(self, wired):
        """A model that sends columns="EmployeeCode" instead of ["EmployeeCode"]
        must get a clear error, not a per-character filter that matches nothing."""
        with pytest.raises(ArgumentValidationError) as exc_info:
            _call(columns="EmployeeCode")
        assert exc_info.value.code == "INVALID_ARGUMENT"

    def test_non_list_container_rejected(self, wired):
        with pytest.raises(ArgumentValidationError):
            _call(columns={"EmployeeCode": True})

    def test_overlong_name_rejected(self, wired):
        with pytest.raises(ArgumentValidationError) as exc_info:
            _call(columns=["x" * 129])
        assert exc_info.value.code == "INVALID_ARGUMENT"

    def test_name_at_the_length_cap_is_accepted(self, wired):
        result = _call(columns=["x" * 128])
        assert result["columns"] == []
        assert result["columns_not_found"] == ["x" * 128]

    def test_validation_runs_before_any_io(self, wired):
        """A malformed argument is the caller's bug — do not hit ClickHouse for it."""
        with pytest.raises(ArgumentValidationError):
            _call(columns=[f"c{i}" for i in range(200)])
        wired.assert_not_called()

    def test_rejection_message_does_not_echo_the_offending_value(self, wired):
        """The caps exist because entries are echoed back; the REJECTION path echoes
        nothing at all, so an oversized payload cannot ride out in the error."""
        with pytest.raises(ArgumentValidationError) as exc_info:
            _call(columns=["z" * 5000])
        assert "z" * 200 not in exc_info.value.message


# ===========================================================================
# Service -> overlay wiring
# ===========================================================================

class TestServiceWiring:
    def test_narrowed_call_returns_only_the_named_column(self, wired):
        result = _call(columns=["AnnualSalary"])
        assert _names(result) == ["AnnualSalary"]
        assert result["columns"][0]["unit"] == "USD"

    def test_base_sections_and_catalog_sha_still_complete(self, wired):
        result = _call(columns=["AnnualSalary"])
        assert result["description"] == "Employee record."
        assert result["grain"] == ["EmployeeCode"]
        assert result["primary_key"] == ["EmployeeCode"]
        assert result["catalog_sha"] == "deadbeef" * 5

    def test_none_and_empty_list_are_equivalent(self, wired):
        assert _call(columns=[]) == _call(columns=None)

    def test_unfiltered_response_shape_is_unchanged(self, wired):
        """Pre-M3 callers see no new key."""
        assert "columns_not_found" not in _call(columns=None)
        assert "columns_not_found" not in _call(columns=[])

    def test_unknown_name_echoed(self, wired):
        result = _call(columns=["Nope"])
        assert result["columns"] == []
        assert result["columns_not_found"] == ["Nope"]

    def test_scoped_out_column_unreachable_by_name(self, wired):
        """ADVERSARIAL, at the full service call: the filter is applied after scope
        enforcement, so naming a scoped-out column cannot surface it."""
        result = _call(columns=["AnnualSalary"], scope=_SCOPE_WITHOUT_SALARY)
        assert result["columns"] == []
        assert result["columns_not_found"] == ["AnnualSalary"]

    def test_scoped_out_column_indistinguishable_from_a_nonexistent_one(self, wired):
        scoped_out = _call(columns=["AnnualSalary"], scope=_SCOPE_WITHOUT_SALARY)
        unknown = _call(columns=["NoSuchColumn"], scope=_SCOPE_WITHOUT_SALARY)
        assert {k: v for k, v in scoped_out.items() if k != "columns_not_found"} == {
            k: v for k, v in unknown.items() if k != "columns_not_found"
        }

    def test_filter_is_a_subset_of_the_unfiltered_response_under_scope(self, wired):
        allowed = _names(_call(scope=_SCOPE_WITHOUT_SALARY))
        got = _names(
            _call(columns=["EmployeeCode", "AnnualSalary"], scope=_SCOPE_WITHOUT_SALARY)
        )
        assert got == allowed == ["EmployeeCode"]

    def test_works_on_an_uncatalogued_table(self, mock_execute, mock_catalog_sha):
        """§1.3 structural-only path still narrows."""
        with patch("app.service.get_semantic_catalog", return_value={}):
            result = service.get_table_schema("wh", "employee", columns=["AnnualSalary"])
        assert result["catalogued"] is False
        assert _names(result) == ["AnnualSalary"]
        assert result["columns_not_found"] == []


# ===========================================================================
# MCP tool surface
# ===========================================================================

class TestMcpToolSurface:
    def test_tool_forwards_the_columns_argument(self, wired):
        from app.mcp_server import get_table_schema as mcp_get_table_schema

        result = mcp_get_table_schema(database="wh", table="employee", columns=["AnnualSalary"])
        assert _names(result) == ["AnnualSalary"]

    def test_tool_defaults_to_every_column(self, wired):
        from app.mcp_server import get_table_schema as mcp_get_table_schema

        result = mcp_get_table_schema(database="wh", table="employee")
        assert _names(result) == ["EmployeeCode", "AnnualSalary"]
        assert "columns_not_found" not in result

    def test_malformed_argument_becomes_a_tool_error_naming_the_code(self, wired):
        from mcp.server.fastmcp.exceptions import ToolError
        from app.mcp_server import get_table_schema as mcp_get_table_schema

        with pytest.raises(ToolError) as exc_info:
            mcp_get_table_schema(
                database="wh", table="employee", columns=[f"c{i}" for i in range(65)]
            )
        assert "[INVALID_ARGUMENT]" in str(exc_info.value)

    def test_argument_error_is_not_reported_as_a_sql_validation_failure(self):
        """A malformed `columns` list has no SQL to fix — the model must not be told
        to go edit a query it never sent."""
        from app.mcp_server import _domain_to_tool_error

        text = str(
            _domain_to_tool_error(
                ArgumentValidationError(message="bad columns list", code="INVALID_ARGUMENT")
            )
        )
        assert "bad columns list" in text
        assert "SQL" not in text

    def test_tool_description_documents_the_narrowing_argument(self):
        """The model only uses the affordance if the tool schema explains it."""
        import inspect

        from app import mcp_server

        source = inspect.getsource(mcp_server)
        assert "columns_not_found" in source
        params = inspect.signature(mcp_server.get_table_schema).parameters
        assert "columns" in params
        assert params["columns"].default is None
