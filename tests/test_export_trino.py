"""Snapshot routing, cancellation and caller isolation in Trino mode."""
from unittest.mock import MagicMock

import pytest

from app import trino_client, export_service
from app.config import Settings
from app.errors import QueryValidationError
from app.principal import Principal, current_principal


@pytest.fixture
def settings():
    return Settings(_env_file=None, clickhouse_host='localhost', clickhouse_user='default',
                    employee_access_enabled=False, trino_enabled=True,
                    trino_security_ready=True, trino_host='localhost', export_max_execution_time=81,
                    max_result_rows=1, max_execution_time=2)


@pytest.fixture
def caller():
    t = current_principal.set(Principal('alice', {'proc_center': 'pc', 'clientcode': 'CLIENT_A',
                                               'user_name': 'alice', 'jti': 'a'}))
    yield
    current_principal.reset(t)


def test_stream_uses_export_timeout_and_does_not_apply_interactive_row_limit(settings, caller, monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.description = [('id', 'bigint')]
    cursor.fetchmany.side_effect = [[[1], [2]], [[3]], []]
    monkeypatch.setattr(trino_client, 'rewrite_query', lambda sql, settings: 'SELECT physical')
    monkeypatch.setattr(trino_client, '_uses_source', lambda *args: False)
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(trino_client, '_connect', connect)
    with trino_client.execute_query_rows_stream('SELECT 1', settings) as stream:
        assert stream.column_names == ['id']
        assert list(stream) == [[1], [2], [3]]
    routed_settings, identity = connect.call_args.args
    assert routed_settings.max_execution_time == 81
    assert routed_settings.max_result_rows == settings.export_total_max_rows + 1
    assert routed_settings.max_result_bytes == settings.export_total_max_bytes
    assert settings.max_execution_time == 2
    assert identity == 'pc|CLIENT_A|alice'
    cursor.close.assert_called_once()
    connection.close.assert_called_once()


def test_stream_cancels_on_export_abort(settings, caller, monkeypatch):
    conn = MagicMock(); cursor = conn.cursor.return_value
    cursor.description = [('id', 'bigint')]
    cursor.fetchmany.return_value = [[1], [2]]
    monkeypatch.setattr(trino_client, 'rewrite_query', lambda *args: 'SELECT 1')
    monkeypatch.setattr(trino_client, '_uses_source', lambda *args: False)
    monkeypatch.setattr(trino_client, '_connect', lambda *args, **kwargs: conn)
    with pytest.raises(RuntimeError):
        with trino_client.execute_query_rows_stream('SELECT 1', settings) as stream:
            next(iter(stream))
            raise RuntimeError('export budget abort')
    cursor.close.assert_called_once(); conn.close.assert_called_once()


def test_stream_fail_closed_before_connection(settings, caller, monkeypatch):
    connect = MagicMock(); monkeypatch.setattr(trino_client, '_connect', connect)
    settings.trino_security_ready = False
    with pytest.raises(QueryValidationError):
        with trino_client.execute_query_rows_stream('SELECT 1', settings):
            pass
    connect.assert_not_called()


def test_snapshot_identity_includes_mysql_username(settings, caller):
    first = export_service._snapshot_identity(settings)
    token = current_principal.set(Principal('bob', {'proc_center':'pc','clientcode':'CLIENT_A',
                                                'user_name':'bob','jti':'a'}))
    try:
        assert first != export_service._snapshot_identity(settings)
    finally:
        current_principal.reset(token)
