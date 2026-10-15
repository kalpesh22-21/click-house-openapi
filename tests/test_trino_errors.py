"""Tests for app.trino_errors.rewrite_trino_error.

Canned errors are constructed exactly the way the installed trino client
builds them (``TrinoUserError(error_dict, query_id)`` — see
trino/exceptions.py, TrinoQueryError.__init__); ``rewrite_trino_error`` is
deliberately duck-typed on ``.error_name`` / ``.message`` (getattr-guarded),
so a namespace double exercises the identical code path without depending on
the client's exception graph — the construction above is regression cover,
not a requirement.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.trino_errors import rewrite_trino_error as rw

QUERY_ID = "20261008_000000_00001_test"


def _trino_user_error(name: str, message, query_id: str = QUERY_ID):
    """Build a TrinoUserError through the installed client's constructor."""
    from trino.exceptions import TrinoUserError

    error = {
        "errorType": "USER_ERROR",
        "errorName": name,
        "errorCode": {"code": 1, "name": name, "type": "USER_ERROR"},
        "message": message,
    }
    return TrinoUserError(error, query_id)


# Every name mapped in app.trino_errors._HINTS, with a representative canned
# coordinator message (wire shape: "<location>: <body>" for analysis errors).
_MAPPED = {
    "FUNCTION_NOT_FOUND": "line 1:8: Function 'uniqexact' not registered",
    "COLUMN_NOT_FOUND": "line 1:8: Column 'hire_dat' cannot be resolved",
    "AMBIGUOUS_COLUMN": "line 1:8: Column 'employee_code' is ambiguous",
    "AMBIGUOUS_COLUMN_NAME": "line 1:8: Column 'employee_code' is ambiguous",
    "TABLE_NOT_FOUND": "line 1:15: Table 'dbpcm_datawarehouse.employee' does not exist",
    "SCHEMA_NOT_FOUND": "line 1:15: Schema 'dbpcm_warehouse' does not exist",
    "CATALOG_NOT_FOUND": "line 1:15: Catalog 'dbpcm' does not exist",
    "SYNTAX_ERROR": "line 1:24: mismatched input 'WHER'. Expecting: ',', <expression>",
    "EXPRESSION_NOT_IN_GROUP_BY": (
        "line 1:8: 'department_name' must be an aggregate expression or "
        "appear in GROUP BY clause"
    ),
    "NOT_AN_AGGREGATE": (
        "line 1:8: 'department_name' must be an aggregate expression or "
        "appear in GROUP BY clause"
    ),
    "TYPE_MISMATCH": "line 1:40: Cannot apply operator: date = varchar(10)",
    "INVALID_CAST_ARGUMENT": "line 1:14: Cannot cast 'not-a-date' to DATE",
    "INVALID_FUNCTION_ARGUMENT": "line 1:20: X must be between 0 and 1: 1.5",
    "DIVISION_BY_ZERO": "line 1:15: Division by zero",
    "EXCEEDED_LOCAL_MEMORY_LIMIT": "Query exceeded per-node memory limit of 2GB",
    "GENERIC_INSUFFICIENT_RESOURCES": "No nodes available to run query",
    # Trino embeds the query id verbatim in timeout text — it must be
    # scrubbed from caller output (it stays log-side only).
    "QUERY_TIMEOUT": f"Query {QUERY_ID} timeout: exceeded 5m",
    "EXCEEDED_TIME_LIMIT": "Query exceeded max run time of 30s",
    "PERMISSION_DENIED": "Access Denied: Cannot select from table employee",
    "ACCESS_DENIED": "Access Denied: Cannot select from table employee",
}


class TestMappedHints:
    @pytest.mark.parametrize("name", sorted(_MAPPED))
    def test_label_hint_and_no_query_id(self, name):
        out = rw(_trino_user_error(name, _MAPPED[name]))
        # Only the allowlisted symbolic label and authored hint are public.
        assert out.startswith(f"{name}.")
        assert _MAPPED[name] not in out
        assert " Hint: " in out
        # query_id never reaches caller output, even when embedded in the
        # coordinator's own message text (the QUERY_TIMEOUT canned case).
        assert QUERY_ID not in out
        # One module spot-check that the verb area references the LOGICAL
        # surface — never a physical shard catalog.
        assert "mysql_shard" not in out and "clickhouse." not in out

    def test_function_not_found_points_at_rewrites(self):
        out = rw(_trino_user_error("FUNCTION_NOT_FOUND", _MAPPED["FUNCTION_NOT_FOUND"]))
        assert "ClickHouse SQL" in out
        assert "getTableSchema" in out
        assert "approx_percentile" not in out

    def test_column_not_found_points_at_get_table_schema(self):
        out = rw(_trino_user_error("COLUMN_NOT_FOUND", _MAPPED["COLUMN_NOT_FOUND"]))
        assert "getTableSchema" in out

    def test_ambiguous_column_points_at_qualification(self):
        for name in ("AMBIGUOUS_COLUMN", "AMBIGUOUS_COLUMN_NAME"):
            out = rw(_trino_user_error(name, _MAPPED[name]))
            assert "table alias" in out

    def test_table_not_found_points_at_list_tables_and_view_reapply(self):
        out = rw(_trino_user_error("TABLE_NOT_FOUND", _MAPPED["TABLE_NOT_FOUND"]))
        assert "listTables" in out
        assert "view-apply" in out

    def test_schema_and_catalog_are_operator_problems(self):
        for name in ("SCHEMA_NOT_FOUND", "CATALOG_NOT_FOUND"):
            out = rw(_trino_user_error(name, _MAPPED[name]))
            assert "operator" in out

    def test_group_by_family_points_at_group_by(self):
        for name in ("EXPRESSION_NOT_IN_GROUP_BY", "NOT_AN_AGGREGATE"):
            out = rw(_trino_user_error(name, _MAPPED[name]))
            assert "GROUP BY the exact expression" in out

    def test_type_family_points_at_typed_literals(self):
        for name in ("TYPE_MISMATCH", "INVALID_CAST_ARGUMENT"):
            out = rw(_trino_user_error(name, _MAPPED[name]))
            assert "toDate('YYYY-MM-DD')" in out

    def test_division_by_zero_points_at_nullif(self):
        out = rw(_trino_user_error("DIVISION_BY_ZERO", _MAPPED["DIVISION_BY_ZERO"]))
        assert "NULLIF(divisor, 0)" in out

    def test_resource_family_points_at_narrowing(self):
        for name in (
            "EXCEEDED_LOCAL_MEMORY_LIMIT",
            "GENERIC_INSUFFICIENT_RESOURCES",
            "QUERY_TIMEOUT",
            "EXCEEDED_TIME_LIMIT",
        ):
            out = rw(_trino_user_error(name, _MAPPED[name]))
            assert "Narrow the date range" in out
            assert "docs/trino.md" in out

    def test_permission_family_points_at_list_tables_and_operator(self):
        for name in ("PERMISSION_DENIED", "ACCESS_DENIED"):
            out = rw(_trino_user_error(name, _MAPPED[name]))
            assert "listTables" in out


class TestHygiene:
    @pytest.mark.parametrize("name", [*_MAPPED, "NEW_ERROR", "private_catalog.secret"])
    def test_backend_details_never_reach_caller(self, name):
        secret = "private_catalog.private_schema.secret_view token=private-token value=private-value"
        out = rw(_trino_user_error(name, secret + " " + QUERY_ID))
        for text in (
            "private_catalog",
            "secret_view",
            "private-token",
            "private-value",
            QUERY_ID,
        ):
            assert text not in out
        assert len(out) < 800
        assert "\n" not in out

    def test_unknown_name_is_not_forwarded(self):
        out = rw(_trino_user_error("SOME_NEW_ERROR", "private message"))
        assert (
            out
            == "Trino rejected the query. Contact the operator if the problem persists."
        )

    def test_empty_and_none_ish_inputs_never_raise(self):
        for exc in (None, object(), SimpleNamespace(), SimpleNamespace(error_name=[])):
            assert rw(exc).startswith("Trino rejected the query.")

    def test_message_and_query_id_are_never_read(self):
        class Error:
            error_name = "COLUMN_NOT_FOUND"

            @property
            def message(self):
                raise AssertionError("message must not be read")

            @property
            def query_id(self):
                raise AssertionError("query id must not be read")

        assert "getTableSchema" in rw(Error())

    def test_hostile_error_name_is_safe(self):
        class Error:
            @property
            def error_name(self):
                raise RuntimeError("private failure")

        assert "private" not in rw(Error())

    def test_duck_typed_namespace_double_drives_same_path(self):
        assert rw(_trino_user_error("COLUMN_NOT_FOUND", "secret")) == rw(
            SimpleNamespace(error_name="COLUMN_NOT_FOUND", message="other secret")
        )


@pytest.mark.parametrize(
    "name",
    [
        "AMBIGUOUS_NAME",
        "EXPRESSION_NOT_AGGREGATE",
        "OPERATOR_NOT_FOUND",
        "INVALID_LITERAL",
        "NUMERIC_VALUE_OUT_OF_RANGE",
        "NOT_SUPPORTED",
        "SUBQUERY_MULTIPLE_ROWS",
        "VIEW_IS_STALE",
        "INVALID_VIEW",
        "INVALID_ROW_FILTER",
        "INVALID_COLUMN_MASK",
        "USER_CANCELED",
        "ADMINISTRATIVELY_KILLED",
        "ABANDONED_QUERY",
        "QUERY_QUEUE_FULL",
        "CLUSTER_OUT_OF_MEMORY",
        "CATALOG_UNAVAILABLE",
        "EXCEEDED_GLOBAL_MEMORY_LIMIT",
        "EXCEEDED_CPU_LIMIT",
        "EXCEEDED_SPILL_LIMIT",
        "EXCEEDED_SCAN_LIMIT",
        "EXCEEDED_FUNCTION_MEMORY_LIMIT",
    ],
)
def test_current_coordinator_codes_have_sanitized_hints(name):
    out = rw(_trino_user_error(name, "secret SQL and token"))
    assert out.startswith(name + ". Hint: ")
    assert "secret" not in out
