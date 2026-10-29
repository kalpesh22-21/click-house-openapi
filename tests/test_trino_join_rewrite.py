# ruff: noqa: F811, F401
"""View translation preserves joins and executes each query only once."""
from unittest.mock import MagicMock

import pytest
from trino.exceptions import TrinoUserError

from app import trino_client
from app.errors import ClickHouseQueryError, QueryValidationError
from tests.test_trino import principal as join_principal  # noqa: F401
from tests.test_trino import shard_registry_path, trino_settings as join_settings


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    monkeypatch.setattr(trino_client, 'get_semantic_catalog', lambda: {
        f'dbpcm_warehouse.{name}': {'source': source, 'table': name, 'database': 'dbpcm_warehouse',
            'trino_join_keys': {'employee_code': {'column': 'employee_code_num', 'encoding': 'employee_ascii4_be_v1'}}}
        for name, source in [('employee', 'mysql'), ('time_off_balance', 'mysql'), ('payroll', 'clickhouse')]
    })


SQL = ('SELECT e.employee_code FROM employee e JOIN time_off_balance b '
       'ON e.client_code=b.client_code AND e.employee_code=b.employee_code')


def test_direct_join_preserves_projection_and_tenant(join_settings, join_principal):
    sql = trino_client.rewrite_query(SQL, join_settings)
    assert 'SELECT e.employee_code FROM' in sql
    assert 'e.employee_code = b.employee_code' in sql
    assert 'e.client_code = b.client_code' in sql
    assert 'employee_code_num' not in sql
    assert 'system.query' not in sql


@pytest.mark.parametrize('query', [
    'SELECT e.employee_code FROM employee e JOIN payroll p ON e.employee_code=p.employee_code',
    'SELECT e.employee_code FROM employee e JOIN employee m ON e.primary_supervisor_ee_code=m.employee_code',
    'SELECT employee_code FROM employee JOIN time_off_balance USING (employee_code)',
    'WITH e AS (SELECT employee_code FROM employee) SELECT e.employee_code FROM e JOIN time_off_balance b ON e.employee_code=b.employee_code',
    'SELECT e.employee_code FROM employee e JOIN time_off_balance b ON e.employee_code=b.employee_code OR e.client_code=b.client_code',
    'SELECT e.employee_code FROM employee e JOIN time_off_balance b ON trim(e.employee_code)=b.employee_code',
])
def test_unsupported_shapes_unchanged(query, join_settings, join_principal):
    assert 'employee_code_num' not in trino_client.rewrite_query(query, join_settings)


def error(name='COLUMN_NOT_FOUND'):
    return TrinoUserError({'errorName': name, 'errorType': 'USER_ERROR', 'errorCode': 1, 'message': 'private SQL secret'})


def connections(monkeypatch, first_error, second_error=None, fetch_error=False):
    conns = [MagicMock(), MagicMock()]
    for conn in conns:
        conn.cursor.return_value.description = [('employee_code',)]
        conn.cursor.return_value.fetchmany.side_effect = [[['AB12']], []]
    if fetch_error:
        conns[0].cursor.return_value.fetchmany.side_effect = [[['DISCARD']], first_error]
    else:
        conns[0].cursor.return_value.execute.side_effect = first_error
    if second_error:
        conns[1].cursor.return_value.execute.side_effect = second_error
    connect = MagicMock(side_effect=conns)
    monkeypatch.setattr(trino_client, '_connect', connect)
    return connect, conns


@pytest.mark.parametrize('fetch_error', [False, True])
@pytest.mark.parametrize('name', ['COLUMN_NOT_FOUND', 'TYPE_MISMATCH', 'OPERATOR_NOT_FOUND'])
def test_query_error_not_retried(monkeypatch, join_settings, join_principal, fetch_error, name):
    connect, conns = connections(monkeypatch, error(name), fetch_error=fetch_error)
    with pytest.raises(ClickHouseQueryError) as caught:
        trino_client.execute_query(SQL, join_settings)
    assert connect.call_count == 1
    assert 'private SQL secret' not in str(caught.value)
    assert 'employee_code_num' not in conns[0].cursor.return_value.execute.call_args.args[0]
    conns[0].close.assert_called_once()
    conns[0].cursor.return_value.close.assert_called_once()


@pytest.mark.parametrize('name', ['PERMISSION_DENIED', 'EXCEEDED_TIME_LIMIT'])
def test_denial_or_timeout_not_retried(monkeypatch, join_settings, join_principal, name):
    connect, _ = connections(monkeypatch, error(name))
    with pytest.raises(ClickHouseQueryError):
        trino_client.execute_query(SQL, join_settings)
    assert connect.call_count == 1


def test_no_rewrite_no_retry(monkeypatch, join_settings, join_principal):
    connect, _ = connections(monkeypatch, error())
    with pytest.raises(ClickHouseQueryError):
        trino_client.execute_query('SELECT employee_code FROM employee', join_settings)
    assert connect.call_count == 1


def test_result_cap_not_retried(monkeypatch, join_settings, join_principal):
    connect, conns = connections(monkeypatch, None)
    join_settings.max_result_rows = 1
    conns[0].cursor.return_value.fetchmany.side_effect = [[['A'], ['B']]]
    with pytest.raises(QueryValidationError):
        trino_client.execute_query(SQL, join_settings)
    assert connect.call_count == 1


def test_catalog_driven_new_entity_and_encoding_match(monkeypatch, join_settings, join_principal):
    catalog = trino_client.get_semantic_catalog()
    catalog['dbpcm_warehouse.new_entity'] = dict(catalog['dbpcm_warehouse.time_off_balance'], table='new_entity')
    monkeypatch.setattr(trino_client, 'get_semantic_catalog', lambda: catalog)
    query = SQL.replace('time_off_balance', 'new_entity')
    assert 'e.employee_code = b.employee_code' in trino_client.rewrite_query(query, join_settings)
    catalog['dbpcm_warehouse.new_entity']['trino_join_keys'] = {'employee_code': {'column': 'other_key', 'encoding': 'different_v2'}}
    assert 'employee_code_num' not in trino_client.rewrite_query(query, join_settings)
    catalog['dbpcm_warehouse.new_entity'].pop('trino_join_keys')
    assert 'employee_code_num' not in trino_client.rewrite_query(query, join_settings)


def test_internal_mapping_not_exported():
    from app.semantic_catalog.export import build_catalog_export_from
    entry = {'columns': {'employee_code': {'type': 'String'}},
             'trino_join_keys': {'employee_code': {'column': 'employee_code_num', 'encoding': 'employee_ascii4_be_v1'}}}
    for enabled in (True, False):
        exported = build_catalog_export_from({'db.employee': entry}, 'test', trino_enabled=enabled)
        assert 'trino_join_keys' not in exported['catalog']['db.employee']
    assert 'trino_join_keys' in entry


@pytest.mark.parametrize('mapping', [None, [], {'missing_column': {'column': 'key', 'encoding': 'v1'}}, {'employee_code': {'column': 'bad;sql', 'encoding': 'v1'}}])
def test_legacy_catalog_mapping_ignored(tmp_path, mapping):
    import yaml

    from app.semantic_catalog.loader import (
        CatalogValidationError,
        _load_raw_table_entries,
    )
    (tmp_path / 'employee.yaml').write_text(yaml.safe_dump({'table': 'employee', 'source': 'mysql', 'columns': {'employee_code': {'type': 'String'}}, 'trino_join_keys': mapping}))
    assert "trino_join_keys" not in _load_raw_table_entries(tmp_path)["dbpcm_warehouse.employee"]


@pytest.mark.parametrize('enabled', [False, True])
def test_internal_mapping_not_in_model_schema(enabled):
    import json

    from app.semantic_catalog.overlay import build_table_schema_response
    entry = {'columns': {'employee_code': {'type': 'String'}},
             'trino_join_keys': {'employee_code': {'column': 'employee_code_num', 'encoding': 'employee_ascii4_be_v1'}}}
    response = build_table_schema_response(
        database='dbpcm_warehouse', table='employee',
        introspected_columns=[{'name': 'employee_code', 'type': 'String'}],
        catalog={'dbpcm_warehouse.employee': entry}, scope=None,
        trino_enabled=enabled,
    )
    serialized = json.dumps(response)
    assert 'trino_join_keys' not in serialized
    assert 'employee_ascii4_be_v1' not in serialized
    assert 'employee_code_num' not in serialized


@pytest.mark.parametrize('name', [
    'ADMINISTRATIVELY_KILLED', 'ABANDONED_QUERY', 'QUERY_REJECTED',
    'EXCEEDED_FUNCTION_MEMORY_LIMIT', 'CATALOG_UNAVAILABLE',
    'INVALID_ROW_FILTER', 'SYNTAX_ERROR', 'FUNCTION_NOT_FOUND',
    'DIVISION_BY_ZERO', 'INVALID_CAST_ARGUMENT', 'VIEW_IS_STALE',
])
def test_unrelated_user_errors_not_retried(monkeypatch, join_settings, join_principal, name):
    connect, _ = connections(monkeypatch, error(name))
    with pytest.raises(ClickHouseQueryError):
        trino_client.execute_query(SQL, join_settings)
    assert connect.call_count == 1


@pytest.mark.parametrize('name', ['EXCEEDED_LOCAL_MEMORY_LIMIT', 'EXCEEDED_TIME_LIMIT', 'EXCEEDED_CPU_LIMIT', 'QUERY_QUEUE_FULL'])
def test_real_resource_exception_class_gets_guidance(monkeypatch, join_settings, join_principal, name):
    from trino.exceptions import TrinoQueryError
    failure = TrinoQueryError({'errorName': name, 'errorType': 'INSUFFICIENT_RESOURCES', 'message': 'secret SQL'})
    connect, conns = connections(monkeypatch, failure, fetch_error=True)
    with pytest.raises(ClickHouseQueryError) as caught:
        trino_client.execute_query(SQL, join_settings)
    assert name in str(caught.value)
    assert 'secret' not in str(caught.value)
    assert connect.call_count == 1
    conns[0].cursor.return_value.close.assert_called_once()
    conns[0].close.assert_called_once()


@pytest.mark.parametrize('kind', ['EXTERNAL', 'INTERNAL_ERROR'])
def test_backend_failures_are_not_query_retries(monkeypatch, join_settings, join_principal, kind):
    from trino.exceptions import TrinoQueryError

    from app.errors import ClickHouseUnavailableError
    failure = TrinoQueryError({'errorName': 'PRIVATE_CONNECTOR_ERROR', 'errorType': kind, 'message': 'private host password SQL'})
    connect, _ = connections(monkeypatch, failure)
    with pytest.raises(ClickHouseUnavailableError) as caught:
        trino_client.execute_query(SQL, join_settings)
    assert 'private' not in str(caught.value)
    assert connect.call_count == 1


@pytest.mark.parametrize('kind', ['auth', 'data', 'timeout', 'connection'])
def test_client_failures_have_safe_handling(monkeypatch, join_settings, join_principal, kind):
    from requests.exceptions import Timeout
    from trino.exceptions import TrinoAuthError, TrinoConnectionError, TrinoDataError

    from app.errors import ClickHouseUnavailableError
    classes = {'auth': TrinoAuthError, 'data': TrinoDataError, 'timeout': Timeout, 'connection': TrinoConnectionError}
    connect, conns = connections(monkeypatch, classes[kind]('secret password row-value'), fetch_error=True)
    expected = ClickHouseQueryError if kind == 'data' else ClickHouseUnavailableError
    with pytest.raises(expected) as caught:
        trino_client.execute_query(SQL, join_settings)
    assert 'secret' not in str(caught.value)
    assert 'row-value' not in str(caught.value)
    if kind == 'data':assert 'decode' in str(caught.value)
    if kind == 'auth':assert 'credentials' in str(caught.value)
    assert connect.call_count == 1
    conns[0].cursor.return_value.close.assert_called_once()
    conns[0].close.assert_called_once()
