from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app import query_routing, service, trino_client
from app.config import Settings
from app.errors import ClickHouseUnavailableError


@pytest.fixture
def routing(monkeypatch):
    catalog = {
        "dbpcm_warehouse." + name: {"source": source}
        for name, source in [("payroll", "clickhouse"), ("employee", "mysql")]
    }
    monkeypatch.setattr(query_routing, "get_semantic_catalog", lambda: catalog)
    direct = MagicMock(return_value=(["total"], [[42]]))
    trino = MagicMock(return_value=(["total"], [[43]]))
    fresh = MagicMock()
    monkeypatch.setattr(service, "execute_query", direct)
    monkeypatch.setattr(trino_client, "execute_query", trino)
    monkeypatch.setattr(service, "ensure_employee_access_fresh", fresh)
    settings = Settings(
        _env_file=None, trino_enabled=True, employee_access_enabled=False
    )
    return settings, direct, trino, fresh


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT avg(amount),sumIf(amount,kind='A') FROM payroll",
        "SELECT toStartOfMonth(pay_date) AS month,sum(amount) FROM payroll GROUP BY month",
        "WITH employee AS (SELECT amount FROM payroll) SELECT sum(amount) FROM employee",
        "SELECT sum(amount) FROM payroll UNION ALL SELECT sum(amount) FROM payroll",
        "SELECT p.amount FROM payroll p JOIN payroll q ON p.employee_code=q.employee_code",
        "SELECT sum(amount) FROM dbpcm_datawarehouse.payroll",
        "EXPLAIN SELECT avg(amount) FROM payroll",
    ],
)
def test_clickhouse_only_direct(sql, routing):
    settings, direct, trino, fresh = routing
    assert service._execute(sql, settings) == (["total"], [[42]])
    direct.assert_called_once()
    trino.assert_not_called()
    fresh.assert_called_once_with(settings)
    rendered = direct.call_args.args[0]
    assert "dbpcm_warehouse.payroll" in rendered
    assert "system.query" not in rendered
    if "sumIf" in sql:
        assert "sumIf" in rendered
    if sql.startswith("EXPLAIN"):
        assert rendered.startswith("EXPLAIN ")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM employee",
        "SELECT * FROM payroll p JOIN employee e ON p.employee_code=e.employee_code",
        "SELECT * FROM payroll WHERE employee_code IN (SELECT employee_code FROM employee)",
        "WITH x AS (SELECT * FROM employee) SELECT * FROM payroll",
        "SELECT * FROM payroll UNION ALL SELECT * FROM employee",
        "SELECT * FROM unknown_table",
        "SELECT * FROM payroll JOIN numbers(10) ON true",
        "SELECT * FROM clickhouse.dbpcm_warehouse.payroll",
        "SELECT 1",
    ],
)
def test_other_shapes_retain_trino(sql, routing):
    settings, direct, trino, fresh = routing
    assert service._execute(sql, settings) == (["total"], [[43]])
    trino.assert_called_once_with(sql, settings, None)
    direct.assert_not_called()
    fresh.assert_not_called()


def test_refresh_failure_blocks_both_backends(routing):
    settings, direct, trino, fresh = routing
    fresh.side_effect = RuntimeError("refresh failed")
    with pytest.raises(RuntimeError):
        service._execute("SELECT sum(amount) FROM payroll", settings)
    direct.assert_not_called()
    trino.assert_not_called()


def test_direct_error_never_falls_back_to_trino(routing):
    settings, direct, trino, _ = routing
    direct.side_effect = HTTPException(502, {"error": "unavailable"})
    with pytest.raises(ClickHouseUnavailableError):
        service._execute("SELECT sum(amount) FROM payroll", settings)
    trino.assert_not_called()


def test_disabled_trino_retains_original_direct_path(routing):
    settings, direct, trino, fresh = routing
    settings.trino_enabled = False
    service._execute("SELECT 1", settings)
    direct.assert_called_once_with("SELECT 1", settings, None)
    trino.assert_not_called()
    fresh.assert_not_called()


def test_allowlist_cannot_trigger_direct_route(routing):
    settings, direct, trino, _ = routing
    settings.allowed_databases = "another_database"
    service._execute("SELECT * FROM payroll", settings)
    direct.assert_not_called()
    trino.assert_called_once()
