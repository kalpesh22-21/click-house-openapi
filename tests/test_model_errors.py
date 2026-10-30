import json
import pytest
from app.errors import ClickHouseQueryError, QueryValidationError, ColumnScopeError
from app.model_errors import model_error_payload, model_tool_error
from app.query_feedback import enrich_query_error


@pytest.mark.parametrize('backend_code', ['TRINO_QUERY_ERROR', 'CLICKHOUSE_QUERY_ERROR'])
def test_type_error_has_neutral_structured_clickhouse_guidance(backend_code):
    exc = ClickHouseQueryError('TYPE_MISMATCH. Hint: Use a typed date literal.', backend_code)
    enrich_query_error(exc, "SELECT amount FROM payroll WHERE pay_date >= 'private-date'")
    wire = model_tool_error(exc)
    p = json.loads(wire.split('] ', 1)[1])
    assert p['code'] == 'SQL_TYPE_MISMATCH'
    assert p['input_dialect'] == 'clickhouse'
    assert p['retry'] == 'after_query_change'
    assert 'toDate(' in p['guidance']
    assert 'pay_date' in p['candidate_expression']
    assert 'TRINO' not in wire and 'Trino' not in wire
    assert 'private-date' not in wire
    assert exc.code == backend_code


@pytest.mark.parametrize('code,public,retry', [
    ('TRINO_SECURITY_NOT_CONFIGURED','EXECUTION_CONFIGURATION_ERROR','after_operator_action'),
    ('TRINO_SOURCE_DEFINITION_INVALID','EXECUTION_CONFIGURATION_ERROR','after_operator_action'),
    ('TRINO_TLS_REQUIRED','EXECUTION_CONFIGURATION_ERROR','after_operator_action'),
    ('TRINO_IDENTITY_REQUIRED','EXECUTION_IDENTITY_REQUIRED','after_access_change'),
    ('TRINO_RESULT_LIMIT','QUERY_RESOURCE_LIMIT','after_scope_or_capacity_change'),
])
def test_operational_errors_do_not_encourage_sql_repairs(code,public,retry):
    p = model_error_payload(QueryValidationError('private configuration', code))
    assert p['code'] == public and p['retry'] == retry
    assert 'private configuration' not in json.dumps(p)
    assert 'candidate_expression' not in p


def test_unsupported_preserves_verified_reason_without_backend_identity():
    e = QueryValidationError('Exact quantiles at this level are unsupported in Trino conversion. Never substitute estimates.', 'TRINO_UNSUPPORTED_SQL')
    p = model_error_payload(e)
    assert p['code'] == 'SQL_UNSUPPORTED_CONSTRUCT'
    assert 'Never substitute estimates' in p['guidance']
    assert 'Trino' not in json.dumps(p)


def test_resource_failure_is_not_reported_as_invalid_sql():
    p = model_error_payload(ClickHouseQueryError('EXCEEDED_LOCAL_MEMORY_LIMIT', 'TRINO_QUERY_ERROR'))
    assert p['code'] == 'QUERY_RESOURCE_LIMIT'
    assert p['retry'] == 'after_scope_or_capacity_change'


def test_unknown_error_does_not_copy_coordinator_details():
    p = model_error_payload(ClickHouseQueryError('private endpoint SQL user credentials', 'TRINO_QUERY_ERROR'))
    assert p['code'] == 'SQL_EXECUTION_ERROR'
    assert 'private endpoint' not in json.dumps(p)


def test_scope_contract_is_unchanged():
    assert model_error_payload(ColumnScopeError('Access denied', 'COLUMN_SCOPE_VIOLATION')) is None


@pytest.mark.parametrize('name', ['SCHEMA_NOT_FOUND', 'CATALOG_NOT_FOUND'])
def test_routing_configuration_error_is_not_a_query_repair(name):
    p = model_error_payload(ClickHouseQueryError(name, 'TRINO_QUERY_ERROR'))
    assert p['code'] == 'EXECUTION_CONFIGURATION_ERROR'
    assert p['retry'] == 'after_operator_action'


def test_missing_live_metadata_is_not_a_sql_repair():
    from app.errors import ClickHouseUnavailableError
    p = model_error_payload(ClickHouseUnavailableError('private source', 'SOURCE_METADATA_UNAVAILABLE'))
    assert p['code'] == 'EXECUTION_SERVICE_UNAVAILABLE'
    assert p['retry'] == 'after_service_recovery'
    assert 'catalog configuration' in p['guidance']
    assert 'private source' not in str(p)


@pytest.mark.parametrize('message', [
    "UNKNOWN_TABLE: Unknown table expression identifier 'dbpcm_warehouse.expense' in scope SELECT secret",
    "UNKNOWN_TABLE: Table dbpcm_warehouse.expense does not exist",
    "TABLE_NOT_FOUND: Table 'dbpcm_warehouse.expense' does not exist",
])
def test_missing_catalog_source_has_safe_non_retryable_contract(message):
    exc = ClickHouseQueryError(message, "CLICKHOUSE_QUERY_ERROR")
    enrich_query_error(exc, "SELECT employee_code FROM dbpcm_warehouse.expense WHERE employee_code = 'secret'")
    payload = model_error_payload(exc)
    assert payload["code"] == "SOURCE_UNAVAILABLE"
    assert payload["source"] == "dbpcm_warehouse.expense"
    assert payload["retryable"] is False
    assert payload["retry"] == "after_source_recovery"
    assert "secret" not in json.dumps(payload)
    assert "candidate_expression" not in payload


@pytest.mark.parametrize('message,sql', [
    ("UNKNOWN_TABLE: Table other.expense does not exist", "SELECT * FROM dbpcm_warehouse.expense"),
    ("UNKNOWN_TABLE: Table dbpcm_warehouse.expense does not exist", "SELECT * FROM dbpcm_warehouse.employee"),
    ("UNKNOWN_TABLE: Table dbpcm_warehouse.not_catalogued does not exist", "SELECT * FROM dbpcm_warehouse.not_catalogued"),
    ("UNKNOWN_IDENTIFIER: expense", "SELECT * FROM dbpcm_warehouse.expense"),
    ("Request timed out", "SELECT * FROM dbpcm_warehouse.expense"),
    ("UNKNOWN_TABLE: unknown cause", "SELECT * FROM dbpcm_warehouse.expense"),
])
def test_unconfirmed_missing_sources_do_not_open_circuit(message, sql):
    exc = ClickHouseQueryError(message, "CLICKHOUSE_QUERY_ERROR")
    enrich_query_error(exc, sql)
    assert model_error_payload(exc)["code"] != "SOURCE_UNAVAILABLE"


def test_missing_source_survives_sanitized_database_exception_chain():
    from fastapi import HTTPException
    from clickhouse_connect.driver.exceptions import DatabaseError

    raw = DatabaseError("Code: 60. DB::Exception: Unknown table expression identifier 'dbpcm_warehouse.expense' in scope SELECT private_value (UNKNOWN_TABLE)")
    http = HTTPException(400, detail={"error": "ClickHouse query error: UNKNOWN_TABLE."})
    http.__cause__ = raw
    exc = ClickHouseQueryError("ClickHouse query error: UNKNOWN_TABLE.", "CLICKHOUSE_QUERY_ERROR")
    exc.__cause__ = http
    enrich_query_error(exc, "SELECT employee_code FROM dbpcm_warehouse.expense")
    payload = model_error_payload(exc)
    assert payload["code"] == "SOURCE_UNAVAILABLE"
    assert payload["source"] == "dbpcm_warehouse.expense"
    assert "private_value" not in json.dumps(payload)
