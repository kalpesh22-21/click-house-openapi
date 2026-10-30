from email.parser import BytesParser
from email.policy import default
from unittest.mock import Mock

import httpx
import pytest

from app import result_freshness as module
from app.config import Settings
from app.principal import Principal, current_principal

NOW = "2026-10-30T12:00:00.000Z"
PAY = "2026-10-12T05:30:00.000Z"


@pytest.fixture
def setup(monkeypatch):
    catalog = {
        "dbpcm_warehouse." + name: {"source": source}
        for name, source in [
            ("employee", "mysql"),
            ("payroll", "clickhouse"),
            ("department", "clickhouse"),
        ]
    }
    monkeypatch.setattr(module, "get_semantic_catalog", lambda: catalog)
    receipts = Mock(
        return_value={"payroll": PAY, "department": "2026-10-13T00:00:00.000Z"}
    )
    monkeypatch.setattr(module, "_receipts", receipts)
    settings = Settings(
        _env_file=None,
        trino_enabled=True,
        trino_host="localhost",
        data_as_of_enabled=True,
    )
    return settings, receipts


def test_live_only_never_queries_etl_receipts(setup):
    settings, receipts = setup
    result = module.result_freshness("SELECT count(*) FROM employee", settings, NOW)
    assert result["freshness"] == {
        "mode": "live",
        "sources": [{"table": "dbpcm_warehouse.employee", "kind": "live"}],
    }
    assert result["retrieved_at"] == NOW
    assert "data_as_of" not in result
    receipts.assert_not_called()


def test_mixed_uses_only_ingested_timestamp_and_minimum(setup):
    settings, receipts = setup
    sql = "SELECT * FROM employee e JOIN payroll p ON e.id=p.id JOIN department d ON e.id=d.id"
    result = module.result_freshness(sql, settings, NOW)
    assert result["freshness"]["mode"] == "mixed"
    assert result["data_as_of"] == PAY
    assert result["freshness"]["sources"][1] == {
        "table": "dbpcm_warehouse.employee",
        "kind": "live",
    }
    receipts.assert_called_once_with(["department", "payroll"], settings)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM payroll",
        "WITH employee AS (SELECT * FROM payroll) SELECT * FROM employee",
        "SELECT * FROM dbpcm_datawarehouse.payroll",
    ],
)
def test_direct_ingested_aliases_and_cte_resolution(sql, setup):
    settings, receipts = setup
    receipts.return_value = {"payroll": PAY}
    result = module.result_freshness(sql, settings, NOW)
    assert result["freshness"]["mode"] == "ingested"
    assert result["data_as_of"] == PAY


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM employee e JOIN payroll p ON e.id=p.id",
        "SELECT * FROM payroll p JOIN unknown_table u ON p.id=u.id",
        "SELECT * FROM payroll p JOIN dbpcm_scratch.saved s ON p.id=s.id",
        "SELECT * FROM numbers(5)",
        "SELECT 1",
        "SELECT * FROM mysql.dbpcm_warehouse.employee",
    ],
)
def test_missing_or_unmapped_sources_never_borrow_known_date(sql, setup):
    settings, receipts = setup
    receipts.return_value = {}
    result = module.result_freshness(sql, settings, NOW)
    assert result["freshness"]["mode"] == "unknown"
    assert "data_as_of" not in result


def test_failure_is_optional_and_disabled_is_noop(setup):
    settings, receipts = setup
    receipts.side_effect = RuntimeError("secret internal message")
    result = module.result_freshness("SELECT * FROM payroll", settings, NOW)
    assert result["freshness"]["mode"] == "unknown"
    assert "secret" not in str(result)
    receipts.reset_mock()
    assert (
        module.result_freshness(
            "SELECT * FROM payroll",
            settings.model_copy(update={"data_as_of_enabled": False}),
            NOW,
        )
        == {}
    )
    receipts.assert_not_called()


def test_non_trino_execution_does_not_call_a_mysql_catalog_entry_live(setup):
    settings, receipts = setup
    receipts.return_value = {"employee": PAY}
    result = module.result_freshness(
        "SELECT * FROM employee",
        settings.model_copy(update={"trino_enabled": False}),
        NOW,
    )
    assert result["freshness"]["mode"] == "ingested"
    assert result["data_as_of"] == PAY


def test_bound_tenant_fixed_query_and_utc_conversion(monkeypatch):
    calls = []

    def handler(request):
        message = BytesParser(policy=default).parsebytes(
            b"Content-Type: "
            + request.headers["content-type"].encode()
            + b"\r\n\r\n"
            + request.content
        )
        fields = {
            p.get_param("name", header="content-disposition"): p.get_content()
            for p in message.iter_parts()
        }
        calls.append(fields)
        assert fields["param_pc"] == "PC01"
        assert fields["param_client"] == "CLIENT' OR 1=1"
        assert "CLIENT' OR 1=1" not in fields["query"]
        assert " FINAL " in fields["query"] and fields["readonly"] == "1"
        return httpx.Response(200, text='["payroll","1791783000000"]\n')

    real = httpx.Client
    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )
    token = current_principal.set(
        Principal(
            subject="alice",
            claims={"proc_center": "PC01", "clientcode": "CLIENT' OR 1=1"},
        )
    )
    try:
        assert module._receipts(["payroll"], Settings(_env_file=None)) == {
            "payroll": PAY
        }
        assert len(calls) == 1
    finally:
        current_principal.reset(token)


def test_partial_receipts_preserved_without_claiming_overall_date(setup):
    settings, receipts = setup
    receipts.return_value = {"payroll": PAY}
    result = module.result_freshness(
        "SELECT * FROM payroll p JOIN department d ON p.id=d.id", settings, NOW
    )
    assert result["freshness"]["mode"] == "unknown"
    assert "data_as_of" not in result
    assert result["freshness"]["sources"][1]["data_as_of"] == PAY


def test_service_mcp_and_rest_preserve_mixed_metadata(setup, monkeypatch):
    from app import service
    from app.mcp_server import _clean_query_result
    from app.models import QueryResponse

    settings, receipts = setup
    receipts.return_value = {"payroll": PAY}
    execute = Mock(return_value=(["total"], [[42]]))
    monkeypatch.setattr(service, "_execute", execute)
    monkeypatch.setattr(
        service, "_enforce_query_guardrails", Mock(return_value=frozenset())
    )
    result = service.run_query(
        "SELECT count(*) FROM employee e JOIN payroll p ON e.id=p.id",
        settings=settings,
        include_provenance=True,
    )
    assert result["data_as_of"] == PAY
    assert result["freshness"]["mode"] == "mixed"
    assert result["provenance"] == {"version": 1, "columns": []}
    execute.assert_called_once()
    assert _clean_query_result(result)["freshness"] == result["freshness"]
    rest = QueryResponse(**result).model_dump(exclude_none=True)
    assert rest["freshness"] == result["freshness"]
    assert rest["retrieved_at"] == result["retrieved_at"]
