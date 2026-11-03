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


def test_public_clickhouse_cause_survives_without_private_body():
    from app.ch_errors import public_clickhouse_error
    raw = "Code: 1. DB::Exception: secret literal and private_view.sql (UNSUPPORTED_METHOD)"
    safe = public_clickhouse_error(raw)
    p = model_error_payload(ClickHouseQueryError(safe, 'CLICKHOUSE_QUERY_ERROR'))
    assert p['cause'] == 'UNSUPPORTED_METHOD' and p['code_num'] == 1
    assert 'secret' not in str(p) and 'private_view' not in str(p)
    assert 'precise cause is unavailable' not in p['message']
    wrong = public_clickhouse_error(raw.replace('Code: 1.', 'Code: 999.'))
    assert 'code_num' not in model_error_payload(ClickHouseQueryError(wrong, 'CLICKHOUSE_QUERY_ERROR'))


def test_unsupported_method_advice_is_structural_and_does_not_claim_a_location():
    from app.ch_errors import public_clickhouse_error
    raw = "Code: 1. DB::Exception: failed at position 987 (line 17, col 44): private_view.secret = 'private-literal' (UNSUPPORTED_METHOD)"
    payload = model_error_payload(ClickHouseQueryError(public_clickhouse_error(raw), 'CLICKHOUSE_QUERY_ERROR'))
    assert payload['cause'] == 'UNSUPPORTED_METHOD' and payload['code_num'] == 1
    hint = payload['guidance']
    assert 'IN expressions' in hint and 'correlated scalar subqueries in SELECT' in hint
    assert 'possible causes, not a confirmed failing clause' in hint
    assert 'NULL behavior and join cardinality' in hint
    assert 'Do not change the requested measure or scope' in hint
    assert all(value not in str(payload) for value in ('private_view', 'private-literal', '987', 'line 17', 'col 44'))


def test_public_syntax_hint_does_not_refer_to_an_unmapped_server_position():
    from app.ch_errors import public_clickhouse_error
    safe = public_clickhouse_error("Code: 62. DB::Exception: failed at position 987 (line 17, col 44): private SQL (SYNTAX_ERROR)")
    assert 'reported position' not in safe
    assert '987' not in safe and 'private SQL' not in safe
    assert 'trailing comma' in safe


@pytest.mark.parametrize('name,category,retry', [
    ('NOT_SUPPORTED', 'SQL_UNSUPPORTED_CONSTRUCT', 'after_query_change'),
    ('FUNCTION_NOT_FOUND', 'SQL_UNSUPPORTED_FUNCTION', 'after_query_change'),
    ('EXPRESSION_NOT_AGGREGATE', 'SQL_AGGREGATION_ERROR', 'after_query_change'),
    ('QUERY_TIMEOUT', 'QUERY_RESOURCE_LIMIT', 'after_scope_or_capacity_change'),
    ('EXCEEDED_CPU_LIMIT', 'QUERY_RESOURCE_LIMIT', 'after_scope_or_capacity_change'),
    ('PERMISSION_DENIED', 'EXECUTION_ACCESS_DENIED', 'after_access_change'),
    ('VIEW_IS_STALE', 'EXECUTION_CONFIGURATION_ERROR', 'after_operator_action'),
    ('INVALID_ROW_FILTER', 'EXECUTION_CONFIGURATION_ERROR', 'after_operator_action'),
    ('CATALOG_UNAVAILABLE', 'EXECUTION_SERVICE_UNAVAILABLE', 'after_service_recovery'),
    ('QUERY_QUEUE_FULL', 'EXECUTION_SERVICE_UNAVAILABLE', 'after_service_recovery'),
    ('USER_CANCELED', 'EXECUTION_CANCELLED', 'after_user_request'),
    ('ADMINISTRATIVELY_KILLED', 'EXECUTION_CANCELLED', 'after_operator_action'),
])
def test_trino_recovery_category_and_cause(name, category, retry):
    from types import SimpleNamespace
    from app.trino_errors import rewrite_trino_error
    exc = ClickHouseQueryError(rewrite_trino_error(SimpleNamespace(error_name=name)), 'TRINO_QUERY_ERROR')
    enrich_query_error(exc, "SELECT amount FROM payroll WHERE employee_code = 'secret'")
    p = model_error_payload(exc)
    assert (p['code'], p['retry'], p['cause']) == (category, retry, name)
    assert 'code_num' not in p  # Never assign a ClickHouse numeric code to Trino.
    assert 'secret' not in str(p)
    if retry != 'after_query_change':
        assert 'candidate_expression' not in p
        assert 'Inspect getTableSchema and correct' not in p['guidance']
    if category == 'QUERY_RESOURCE_LIMIT':
        assert 'preserves the requested answer' in p['guidance']


def test_trino_hint_registry_has_no_unclassified_names():
    from app.trino_errors import _HINTS, ERROR_POLICIES
    assert set(_HINTS) == set(ERROR_POLICIES)
    for name, (category, retry) in ERROR_POLICIES.items():
        p = model_error_payload(ClickHouseQueryError(name + '. Hint: unused', 'TRINO_QUERY_ERROR'))
        assert (p['code'], p['retry'], p['cause']) == (category, retry, name)


@pytest.mark.parametrize('error_type', ['USER_ERROR', 'EXTERNAL'])
def test_trino_exception_to_model_preserves_known_operational_cause(error_type):
    from trino.exceptions import TrinoUserError, TrinoQueryError
    from app.trino_client import _raise_execution_error
    cls = TrinoUserError if error_type == 'USER_ERROR' else TrinoQueryError
    exc = cls({'errorType': error_type, 'errorName': 'VIEW_IS_STALE',
               'message': "private_view secret-literal", 'errorCode': 1,
               'errorLocation': {'lineNumber': 12, 'columnNumber': 34}}, 'test-query-id')
    with pytest.raises(ClickHouseQueryError) as caught:
        _raise_execution_error(exc)
    p = model_error_payload(caught.value)
    assert p['code'] == 'EXECUTION_CONFIGURATION_ERROR'
    assert p['cause'] == 'VIEW_IS_STALE'
    assert all(secret not in str(p) for secret in ('private_view', 'secret-literal', 'lineNumber', 'test-query-id'))


def test_trino_names_inside_error_body_do_not_create_a_cause_or_permission_denial():
    p = model_error_payload(ClickHouseQueryError('Unclassified message contains PERMISSION_DENIED and VIEW_IS_STALE', 'TRINO_QUERY_ERROR'))
    assert p['code'] == 'SQL_EXECUTION_ERROR'
    assert 'cause' not in p


@pytest.mark.parametrize('code,public,retry', [
    ('EMPLOYEE_ACCESS_UNAVAILABLE', 'EXECUTION_SERVICE_UNAVAILABLE', 'after_service_recovery'),
    ('EMPLOYEE_ACCESS_NOT_PROVISIONED', 'EXECUTION_CONFIGURATION_ERROR', 'after_operator_action'),
    ('EMPLOYEE_ACCESS_MISCONFIGURED', 'EXECUTION_CONFIGURATION_ERROR', 'after_operator_action'),
    ('EMPLOYEE_ACCESS_NO_TOKEN', 'EXECUTION_IDENTITY_REQUIRED', 'after_access_change'),
    ('EMPLOYEE_ACCESS_NO_JTI', 'EXECUTION_IDENTITY_REQUIRED', 'after_access_change'),
])
def test_employee_access_failure_keeps_its_cause_and_is_not_an_empty_result(code, public, retry):
    from app.employee_access import EmployeeAccessError
    wire = model_tool_error(EmployeeAccessError('private access detail', code))
    p = json.loads(wire.split('] ', 1)[1])
    assert wire.startswith(f'[{public}] ')
    assert p['code'] == public and p['retry'] == retry
    assert p['cause'] == code and p['message'].endswith(f' Cause: {code}.')
    assert 'not an empty result' in p['guidance']
    assert 'private access detail' not in wire


def test_other_unavailable_errors_carry_no_access_cause():
    from app.errors import ClickHouseUnavailableError
    p = model_error_payload(ClickHouseUnavailableError('down', 'CLICKHOUSE_UNAVAILABLE'))
    assert p['code'] == 'EXECUTION_SERVICE_UNAVAILABLE' and 'cause' not in p
