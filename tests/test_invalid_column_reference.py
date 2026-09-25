"""Missing physical columns must produce actionable, fail-closed diagnostics."""
from unittest.mock import Mock

import pytest

from app import service
from app.errors import ParseFailedError, QueryValidationError
from app.sqlparse.provenance import (
    InvalidColumnReferenceError,
    ProvenanceExtractionError,
    extract_column_provenance,
)

TABLE = 'dbpcm_warehouse.personnel_action_form_changes'
SCHEMA = {TABLE: {'paf_transaction_id': 'Int32', 'field_label': 'String'}}


@pytest.mark.parametrize('sql', [
    f'SELECT field_id FROM {TABLE}',
    f'SELECT p.field_id FROM {TABLE} p',
    f'SELECT DISTINCT field_id FROM {TABLE} WHERE field_label ILIKE \'%pay%\'',
    f'SELECT paf_transaction_id FROM {TABLE} WHERE field_id IN (\'pay\')',
    f'WITH fields AS (SELECT field_id FROM {TABLE}) SELECT * FROM fields',
    f'SELECT x FROM (SELECT field_id AS x FROM {TABLE}) s',
])
def test_missing_physical_column(sql):
    with pytest.raises(InvalidColumnReferenceError) as error:
        extract_column_provenance(sql, SCHEMA)
    assert error.value.column == 'field_id'
    assert error.value.tables == (TABLE,)


def test_column_present_extracts():
    schema = {TABLE: {**SCHEMA[TABLE], 'field_id': 'String'}}
    assert extract_column_provenance(f'SELECT field_id FROM {TABLE}', schema) == {(TABLE, 'field_id')}


def test_computed_alias_remains_valid():
    assert extract_column_provenance(
        f'SELECT count(*) AS field_id FROM {TABLE} ORDER BY field_id', SCHEMA,
    ) == frozenset()


def test_ambiguous_column_remains_extraction_failure():
    with pytest.raises(ProvenanceExtractionError) as error:
        extract_column_provenance(
            'SELECT id FROM db.a a JOIN db.b b ON a.id = b.id',
            {'db.a': {'id': 'Int32'}, 'db.b': {'id': 'Int32'}},
        )
    assert not isinstance(error.value, InvalidColumnReferenceError)


@pytest.mark.parametrize('entrypoint', [service.run_query, service.explain_query])
def test_service_rejects_before_execution(monkeypatch, entrypoint):
    monkeypatch.setattr(service, 'get_current_scope', lambda: frozenset())
    monkeypatch.setattr(service, 'get_catalog_schema', lambda: SCHEMA)
    execute = Mock(side_effect=AssertionError('Rejected query executed'))
    monkeypatch.setattr(service, '_execute', execute)
    with pytest.raises(QueryValidationError) as error:
        entrypoint(f'SELECT field_id FROM {TABLE}')
    assert error.value.code == 'INVALID_COLUMN_REFERENCE'
    assert TABLE in error.value.message
    assert 'field_id' in error.value.message
    execute.assert_not_called()


def test_genuine_extraction_failure_retains_code(monkeypatch):
    monkeypatch.setattr(service, 'get_current_scope', lambda: frozenset())
    monkeypatch.setattr(service, 'get_catalog_schema', lambda: SCHEMA)
    monkeypatch.setattr(service, 'extract_column_provenance', Mock(side_effect=ProvenanceExtractionError()))
    with pytest.raises(ParseFailedError) as error:
        service.run_query(f'SELECT field_label FROM {TABLE}')
    assert error.value.code == 'PARSE_FAILED_CLOSED'


def test_mcp_preserves_column_diagnostic():
    from app.mcp_server import _domain_to_tool_error
    error = QueryValidationError(
        message=str(InvalidColumnReferenceError('field_id', [TABLE])),
        code='INVALID_COLUMN_REFERENCE',
    )
    message = str(_domain_to_tool_error(error))
    assert message == f'[INVALID_COLUMN_REFERENCE] {error.message}'
