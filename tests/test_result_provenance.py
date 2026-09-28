"""Provenance in a response is the set validated by the execution API."""

from unittest.mock import patch
import pytest
from app import service
from app.errors import ColumnScopeError, ClickHouseQueryError
from app.principal import current_scope
from tests.test_scope_enforcement import _CATALOG


@pytest.fixture(autouse=True)
def environment():
    token = current_scope.set(frozenset())
    with (
        patch("app.service.get_catalog_schema", return_value=_CATALOG),
        patch("app.service.get_semantic_catalog", return_value={}),
        patch("app.service.execute_query", return_value=(["Total"], [[42]])) as execute,
    ):
        yield execute
    current_scope.reset(token)


@pytest.mark.parametrize("method", [service.run_query, service.explain_query])
def test_receipt_contains_filter_and_aggregate_dependencies(method):
    extract = service.extract_column_provenance
    with patch("app.service.extract_column_provenance", wraps=extract) as spy:
        result = method(
            "SELECT sum(gross_pay) AS Total FROM analytics.orders WHERE status = 'approved'",
            include_provenance=True,
        )
    assert spy.call_count == 1
    assert result["provenance"] == {
        "version": 1,
        "columns": [["analytics.orders", "gross_pay"], ["analytics.orders", "status"]],
    }


def test_sample_receipt_is_the_validated_projection():
    result = service.sample_rows("analytics", "orders", include_provenance=True)
    assert result["provenance"]["columns"] == [
        ["analytics.orders", c] for c in sorted(_CATALOG["analytics.orders"])
    ]


def test_mcp_provenance_requested_even_without_bound_scope():
    current_scope.set(None)
    assert service.run_query("SELECT 1", include_provenance=True)["provenance"]["columns"] == []


def test_denial_has_no_data_or_receipt(environment):
    current_scope.set(frozenset({"analytics.orders.order_id"}))
    with pytest.raises(ColumnScopeError):
        service.run_query("SELECT gross_pay FROM analytics.orders", include_provenance=True)
    environment.assert_not_called()


def test_engine_error_is_not_replaced_with_provenance_error(environment):
    from fastapi import HTTPException

    environment.side_effect = HTTPException(
        400,
        {
            "code": "CLICKHOUSE_QUERY_ERROR",
            "error": "Code: 184. ILLEGAL_AGGREGATION: nested aggregate",
        },
    )
    with pytest.raises(ClickHouseQueryError, match="ILLEGAL_AGGREGATION"):
        service.run_query("SELECT sum(gross_pay) FROM analytics.orders", include_provenance=True)


def test_hidden_policy_dependencies_do_not_require_caller_scope():
    catalog = {"analytics.orders": {"mcp_projection": {"hidden_columns": ["status"]}}}
    current_scope.set(frozenset({"analytics.orders.gross_pay"}))
    with patch("app.service.get_semantic_catalog", return_value=catalog):
        result = service.run_query(
            "SELECT gross_pay FROM analytics.orders WHERE status = 'approved'",
            include_provenance=True,
        )
    assert result["provenance"]["columns"] == [["analytics.orders", "gross_pay"]]


@pytest.mark.parametrize(
    "method,args",
    [
        (service.run_query, ("SELECT 1",)),
        (service.explain_query, ("SELECT 1",)),
        (service.sample_rows, ("analytics", "orders")),
    ],
)
def test_legacy_service_response_shape_is_unchanged(method, args):
    assert "provenance" not in method(*args)


def test_empty_result_still_carries_validated_dependencies(environment):
    environment.return_value = (["gross_pay"], [])
    result = service.run_query("SELECT gross_pay FROM analytics.orders", include_provenance=True)
    assert result["row_count"] == 0
    assert result["provenance"]["columns"] == [["analytics.orders", "gross_pay"]]
