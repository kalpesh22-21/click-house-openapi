"""Backend-neutral MCP error contract; original exceptions remain log-side."""
import json
import re

from app.errors import ClickHouseQueryError, ClickHouseUnavailableError

_QUERY_CATEGORIES = {
    'SQL_TYPE_MISMATCH': ('TYPE_MISMATCH', 'NO_COMMON_TYPE', 'ILLEGAL_TYPE_OF_ARGUMENT', 'OPERATOR_NOT_FOUND', 'INVALID_CAST_ARGUMENT'),
    'SQL_SYNTAX_ERROR': ('SYNTAX_ERROR',),
    'SQL_UNKNOWN_COLUMN': ('COLUMN_NOT_FOUND', 'UNKNOWN_IDENTIFIER', 'INVALID_COLUMN_REFERENCE'),
    'SQL_UNSUPPORTED_FUNCTION': ('FUNCTION_NOT_FOUND', 'UNKNOWN_FUNCTION'),
    'SQL_AGGREGATION_ERROR': ('NOT_AN_AGGREGATE', 'EXPRESSION_NOT_IN_GROUP_BY', 'ILLEGAL_AGGREGATION'),
    'QUERY_RESOURCE_LIMIT': ('EXCEEDED_LOCAL_MEMORY_LIMIT', 'EXCEEDED_GLOBAL_MEMORY_LIMIT', 'EXCEEDED_TIME_LIMIT', 'GENERIC_INSUFFICIENT_RESOURCES', 'TRINO_RESULT_LIMIT'),
}
_MESSAGES = {
    'CATALOG_CONTEXT_UNAVAILABLE': 'Column context could not be loaded.',
    'SQL_TYPE_MISMATCH': 'Some operands have incompatible types or values outside the supported conversion range.',
    'SQL_SYNTAX_ERROR': 'The submitted query has unsupported or invalid syntax.',
    'SQL_UNKNOWN_COLUMN': 'A referenced column could not be resolved.',
    'SQL_UNSUPPORTED_FUNCTION': 'This function or signature is not supported on this execution route.',
    'SQL_AGGREGATION_ERROR': 'The aggregation or grouping expressions are incompatible.',
    'SQL_UNSUPPORTED_CONSTRUCT': 'This query construct has no verified conversion on this execution route.',
    'SQL_EXECUTION_ERROR': 'The query could not be executed; the precise cause is unavailable.',
    'QUERY_RESOURCE_LIMIT': 'Execution exceeded a resource or result limit.',
    'EXECUTION_SERVICE_UNAVAILABLE': 'The execution service is temporarily unavailable.',
    'EXECUTION_CONFIGURATION_ERROR': 'The execution service requires operator configuration.',
    'EXECUTION_IDENTITY_REQUIRED': 'A valid authenticated execution identity is required.',
}


def _neutral(text):
    text = re.sub(r'\bTrino conversion\b', 'supported SQL conversion', text, flags=re.I)
    text = re.sub(r'\bTRINO_[A-Z_]+\b', 'execution error', text)
    return re.sub(r'\bTrino\b', 'execution service', text, flags=re.I)


def model_error_payload(exc):
    code = getattr(exc, 'code', '')
    feedback = getattr(exc, 'query_feedback', {})
    text = feedback.get('reason', getattr(exc, 'message', ''))
    retry = 'after_query_change'
    guidance = 'Write ClickHouse SQL using logical catalog tables. Inspect getTableSchema and correct the submitted query before retrying; explainQuery applies the same validation.'
    if code == 'TRINO_IDENTITY_REQUIRED':
        public = 'EXECUTION_IDENTITY_REQUIRED'
        retry = 'after_access_change'
        guidance = 'Restore a valid authenticated session. Do not rewrite SQL to bypass access checks.'
    elif code in {'TRINO_SECURITY_NOT_CONFIGURED', 'TRINO_TLS_REQUIRED', 'TRINO_SOURCE_DEFINITION_INVALID'} or (
        isinstance(exc, ClickHouseQueryError) and re.search(r'\b(SCHEMA_NOT_FOUND|CATALOG_NOT_FOUND)\b', text)
    ):
        public = 'EXECUTION_CONFIGURATION_ERROR'
        retry = 'after_operator_action'
        guidance = 'Ask the operator to correct execution-service configuration. Rewriting SQL will not resolve this.'
    elif code == 'CATALOG_CONTEXT_UNAVAILABLE':
        public = 'CATALOG_CONTEXT_UNAVAILABLE'
        retry = 'after_operator_action'
        guidance = 'Ask the operator to check the column context query, result shape, limits and source permissions. Do not rewrite business SQL to bypass this failure.'
    elif isinstance(exc, ClickHouseUnavailableError):
        public = 'EXECUTION_SERVICE_UNAVAILABLE'
        retry = 'after_service_recovery'
        guidance = ('Refresh discovery or ask the operator to check source availability and catalog configuration. Do not rewrite SQL to bypass missing metadata.' if code == 'SOURCE_METADATA_UNAVAILABLE' else 'Retry after service recovery. Do not change SQL merely to work around this availability failure.')
    elif isinstance(exc, ClickHouseQueryError) or code in {'TRINO_UNSUPPORTED_SQL', 'TRINO_RESULT_LIMIT'}:
        public = next((key for key, names in _QUERY_CATEGORIES.items()
                       if any(re.search(r'\b' + name + r'\b', code + ' ' + text.split('Submitted ClickHouse excerpt', 1)[0]) for name in names)),
                      'SQL_UNSUPPORTED_CONSTRUCT' if code == 'TRINO_UNSUPPORTED_SQL' else 'SQL_EXECUTION_ERROR')
        if public == 'QUERY_RESOURCE_LIMIT':
            retry = 'after_scope_or_capacity_change'
            guidance = 'Reduce scanned data or aggregate earlier only if that preserves the requested answer, or ask the operator to increase capacity. Do not retry unchanged.'
        elif code == 'TRINO_UNSUPPORTED_SQL':
            # This branch contains app-authored validation reasons, never raw
            # coordinator messages. Preserve verified limitations/alternatives.
            guidance = _neutral(text.split('Submitted ClickHouse excerpt', 1)[0])[:1600]
        elif 'Hint:' in text:
            guidance += ' ' + _neutral(text.split('Hint:', 1)[1].split('Submitted ClickHouse excerpt', 1)[0])[:1200]
    else:
        return None  # Existing scope/argument contracts are backend-neutral.
    payload = dict(version=1, code=public, message=_MESSAGES[public],
                   input_dialect='clickhouse', guidance=guidance, retry=retry)
    if retry == 'after_query_change':
        if feedback.get('excerpt'):
            payload['candidate_expression'] = _neutral(feedback['excerpt'])[:350]
        for key in ('alternative', 'type_guidance'):
            if feedback.get(key):
                payload['guidance'] += ' ' + _neutral(feedback[key])[:1200]
    payload["guidance"] = payload["guidance"].replace(
        'Write the corrected query in ClickHouse SQL using logical catalog tables; the service handles backend translation. ', ''
    )[:2200]
    return payload


def model_tool_error(exc):
    payload = model_error_payload(exc)
    if payload is None:
        return None
    return f"[{payload['code']}] " + json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
