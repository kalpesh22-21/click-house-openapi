"""Public failure feedback uses only caller-owned SQL, never backend details."""
from unittest.mock import patch

import pytest
from app import service
from app.config import Settings
from app.errors import QueryValidationError, ClickHouseQueryError, ColumnScopeError
from app.query_feedback import enrich_query_error, logical_excerpt
from app.ch_errors import public_clickhouse_error
from app.trino_errors import rewrite_trino_error


def test_unsupported_function_has_clickhouse_alternative_and_targeted_excerpt():
    error = QueryValidationError('formatReadableQuantity is unsupported.', 'TRINO_UNSUPPORTED_SQL')
    enrich_query_error(error, "SELECT formatReadableQuantity(salary), 'private-literal' FROM employee /* secret-comment */")
    assert 'Alternative:' in error.message and 'toString(bucket)' in error.message
    assert 'Write the corrected query in ClickHouse SQL' in error.message
    assert 'identified expression: formatReadableQuantity(salary)' in error.message
    assert 'private-literal' not in error.message and 'secret-comment' not in error.message
    assert str(error) == error.message
    before = error.message
    enrich_query_error(error, 'SELECT 1')
    assert error.message == before


def test_execution_context_does_not_claim_a_precise_backend_location():
    error = ClickHouseQueryError('COLUMN_NOT_FOUND. Call getTableSchema.', 'TRINO_QUERY_ERROR')
    enrich_query_error(error, "SELECT misspelled_salary FROM employee WHERE name='Private Name'")
    assert 'misspelled_salary' in error.message
    assert 'exact failure location unavailable' in error.message
    assert 'Private Name' not in error.message


def test_redacts_literals_comments_and_bounds_context():
    excerpt = logical_excerpt("SELECT concat('private', salary) FROM employee WHERE salary=123456789 /* password */", 'unknown')
    assert 'private' not in excerpt and '123456789' not in excerpt and 'password' not in excerpt
    assert len(logical_excerpt('SELECT '+','.join('col'+str(i) for i in range(1000))+' FROM employee', 'unknown')) < 330


def test_invalid_quote_never_uses_raw_fallback():
    excerpt = logical_excerpt("SELECT 'private unterminated", 'syntax')
    assert 'private' not in excerpt
    assert 'could not be parsed safely' in excerpt


@pytest.mark.parametrize('entrypoint', [service.run_query, service.explain_query])
@pytest.mark.parametrize('phase', ['validation', 'execution'])
def test_shared_service_delivers_guidance_to_both_transports(entrypoint, phase):
    settings = Settings(trino_enabled=False)
    error = QueryValidationError('Unsupported formatReadableQuantity.', 'TRINO_UNSUPPORTED_SQL') if phase == 'validation' else ClickHouseQueryError('SYNTAX_ERROR. Check syntax.', 'TRINO_QUERY_ERROR')
    with patch.object(service, 'validate_and_sanitize', return_value='SELECT salary FROM employee'), patch.object(service, '_enforce_query_guardrails', side_effect=error if phase == 'validation' else None), patch.object(service, 'ensure_employee_access_fresh'), patch.object(service, '_execute', side_effect=error):
        with pytest.raises(type(error)) as caught:
            entrypoint(sql='SELECT formatReadableQuantity(salary) FROM employee', settings=settings)
    assert 'ClickHouse SQL' in caught.value.message
    assert 'Submitted ClickHouse excerpt' in caught.value.message
    from app.routers.query import _map_query_error
    assert _map_query_error(caught.value).detail['error'] == caught.value.message
    from app.mcp_server import _domain_to_tool_error
    import json
    public = json.loads(str(_domain_to_tool_error(caught.value)).split('] ', 1)[1])
    assert public['input_dialect'] == 'clickhouse'
    assert public['retry'] == 'after_query_change'
    assert 'formatReadableQuantity' in public['candidate_expression']
    assert 'TRINO_' not in json.dumps(public)


def test_permission_denial_does_not_encourage_workarounds():
    error = ColumnScopeError('Remove forbidden salary column.', 'COLUMN_SCOPE_VIOLATION')
    enrich_query_error(error, 'SELECT salary FROM employee')
    assert error.message == 'Remove forbidden salary column.'


@pytest.mark.parametrize('raw', [
    'Code: 47. DB::Exception: private_schema.secret_view token=secret row=private-data (UNKNOWN_IDENTIFIER)',
    'unknown backend text private_schema.secret_view token=secret',
])
def test_clickhouse_backend_details_are_not_forwarded(raw):
    result = public_clickhouse_error(raw)
    assert 'secret' not in result and 'private' not in result
    assert 'ClickHouse' in result or 'getTableSchema' in result


def test_trino_hints_never_suggest_trino_only_approximation():
    class Error:
        error_name = 'FUNCTION_NOT_FOUND'
        @property
        def message(self):
            raise AssertionError('backend body must never be read')
    result = rewrite_trino_error(Error())
    assert 'ClickHouse SQL' in result
    assert 'approx_percentile' not in result
    assert 'getTableSchema' in result


def test_runtime_alternative_is_not_mistaken_for_failure_location():
    message = 'FUNCTION_NOT_FOUND. Hint: Use quantileExactLow(0.5)(salary).'
    excerpt = logical_excerpt('SELECT quantileExactLow(0.5)(salary), missingFunction(salary) FROM employee', message)
    assert 'identified expression:' not in excerpt
    assert 'exact failure location unavailable' in excerpt


def test_type_conflict_candidates_are_redacted_and_not_claimed_as_exact():
    error = ClickHouseQueryError('NO_COMMON_TYPE.', 'CLICKHOUSE_QUERY_ERROR')
    enrich_query_error(error, "SELECT coalesce(amount, 'secret-value') FROM employee UNION ALL SELECT 123456789 /* private-comment */")
    assert 'UNION output position 1' in error.message
    assert 'conditional/default expression' in error.message
    assert 'not a confirmed error location' in error.message
    assert 'Nullable(Decimal(18,6))' in error.message
    assert 'Write the corrected query in ClickHouse SQL' in error.message
    for secret in ['secret-value', '123456789', 'private-comment']:
        assert secret not in error.message


def test_type_conflict_window_default_and_date_operands():
    error = ClickHouseQueryError('TYPE_MISMATCH.', 'TRINO_QUERY_ERROR')
    enrich_query_error(error, "SELECT lag(amount, 1, 0) OVER (ORDER BY pay_date) FROM payroll WHERE pay_date >= 'private-date'")
    assert 'window value/default' in error.message
    assert 'comparison operands' in error.message
    assert 'private-date' not in error.message


def test_type_feedback_does_not_leak_unparseable_sql():
    error = ClickHouseQueryError('NO_COMMON_TYPE.', 'CLICKHOUSE_QUERY_ERROR')
    enrich_query_error(error, "SELECT 'private-unterminated")
    assert 'private-unterminated' not in error.message
    assert 'Candidate type-conflict' not in error.message


def test_to_int64_runtime_guard_explains_domain_without_inventing_defaults():
    error = ClickHouseQueryError('INVALID_CAST_ARGUMENT.', 'TRINO_QUERY_ERROR')
    enrich_query_error(error, 'SELECT toInt64(amount) FROM payroll')
    assert 'finite and within [-2^63, 2^63)' in error.message
    assert 'do not silently discard rows' in error.message


@pytest.mark.parametrize('function, expected', [
    ('toInt64(amount)', 'truncates toward zero'),
    ('toYear(hire_date)', 'catalog date/timestamp'),
    ('arrayStringConcat(arraySort(groupUniqArray(department_name)), delimiter)', 'directory labels'),
    ('uniqExact(employee_code, punch_date)', 'SELECT DISTINCT employee_code'),
    ('quantileExactLow(0.9)(salary)', 'do not replace exact results with estimates'),
])
def test_observed_runtime_functions_get_candidate_guidance(function, expected):
    error = ClickHouseQueryError('FUNCTION_NOT_FOUND. Hint: Check function signatures.', 'TRINO_QUERY_ERROR')
    enrich_query_error(error, f'SELECT {function} FROM employee')
    assert expected in error.query_feedback['alternative']
    assert 'failing function is not confirmed' in error.query_feedback['alternative']
    from app.model_errors import model_error_payload
    payload = model_error_payload(error)
    assert expected in payload['guidance']
    assert payload['input_dialect'] == 'clickhouse'


@pytest.mark.parametrize('message', ['FUNCTION_NOT_FOUND.', 'SYNTAX_ERROR.', 'NO_COMMON_TYPE.'])
def test_unknown_functions_do_not_get_unrelated_replacements(message):
    error = ClickHouseQueryError(message, 'TRINO_QUERY_ERROR')
    enrich_query_error(error, 'SELECT inventedFunction(amount) FROM employee')
    assert not error.query_feedback['alternative']


def test_runtime_guidance_ignores_literal_comment_and_hint_function_names():
    error = ClickHouseQueryError('FUNCTION_NOT_FOUND. Hint: Try toYear.', 'TRINO_QUERY_ERROR')
    enrich_query_error(error, "SELECT unknownFn('toInt64 private') FROM employee /* quantileExactLow */")
    assert not error.query_feedback['alternative']
    assert 'private' not in error.message


def test_arity_error_has_observed_pair_count_guidance():
    error = ClickHouseQueryError('NUMBER_OF_ARGUMENTS_DOESNT_MATCH.', 'CLICKHOUSE_QUERY_ERROR')
    enrich_query_error(error, 'SELECT uniqExact(employee_code, punch_date) FROM punch_pair')
    assert 'NULL-exclusion rules' in error.query_feedback['alternative']


def test_signature_index_is_conditional_and_compact():
    from app.query_feedback import observed_function_guide
    guide = observed_function_guide()
    assert len(guide) < 1500
    assert 'not a complete allowlist' in guide
    assert 'quantileExactLow(0.5)' in guide
    assert 'Native ClickHouse' in guide
    assert 'trino_join_keys' not in guide
