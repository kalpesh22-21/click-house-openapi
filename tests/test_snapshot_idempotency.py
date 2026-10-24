"""Cross-worker idempotency, caller isolation, request fingerprints and commit recovery."""
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, Lock
from unittest.mock import patch

import pytest

from app import export_service as svc
from app.catalog_scope import CatalogScope
from app.errors import QueryValidationError
from app.export_client import SnapshotObjectMissingError
from app.principal import current_session_id, current_principal
from app.snapshot_idempotency import SnapshotIdempotencyError
from tests.test_export_service import FakeSnapshotStore, _settings, _principal, _FakeChStream


class AtomicStore(FakeSnapshotStore):
    def __init__(self, backend=None):
        if backend is None:
            backend = ({}, {}, Lock())
        self.objects, self.versions, self.lock = self.backend = backend
        self.uploads = []
        self.presign_calls = []
        self.deleted_prefixes = []

    def read_idempotency(self, key):
        with self.lock:
            if key not in self.objects:
                raise SnapshotObjectMissingError(key)
            return json.loads(self.objects[key]), self.versions[key]

    def write_idempotency(self, key, value, *, version=None):
        with self.lock:
            if self.versions.get(key) != version:
                return None
            current = (self.versions.get(key) or 0) + 1
            self.objects[key] = json.dumps(value).encode()
            self.versions[key] = current
            return current


@contextmanager
def caller(user='TESTJTI001', session='s1', columns=None, schemas=None, token="", extra_claims=None):
    from dataclasses import replace
    principal = _principal(jti=user)
    principal = replace(principal, token=token, claims={**principal.claims, **(extra_claims or {})},
                        catalog_scope=CatalogScope(
                            columns=frozenset(columns) if columns is not None else None,
                            schemas=frozenset(schemas) if schemas is not None else None))
    p = current_principal.set(principal); s = current_session_id.set(session)
    try: yield
    finally: current_session_id.reset(s); current_principal.reset(p)


@pytest.fixture
def env(monkeypatch):
    settings = _settings()
    store = AtomicStore()
    monkeypatch.setattr(svc, '_enforce_query_guardrails', lambda *a, **kw: None)
    monkeypatch.setattr(svc, 'ensure_employee_access_fresh', lambda *a: None)
    query = []
    def execute(sql, *a, **kw):
        query.append(sql)
        return _FakeChStream(['id'], ['UInt64'], [[1], [2]])
    monkeypatch.setattr(svc, 'execute_query_rows_stream', execute)
    return settings, store, query


def mint(env, sql='SELECT id FROM t', store=None):
    settings, default_store, _ = env
    return svc.create_snapshot(sql, settings, store=store or default_store)


def test_replay_reuses_snapshot_without_query_or_schema_refresh(env, monkeypatch):
    with caller():
        result = mint(env)
        monkeypatch.setattr(svc, '_enforce_query_guardrails', lambda *a, **k: pytest.fail('replay queried schema'))
        assert mint(env) == result
    assert len(env[2]) == 1


def test_concurrent_replicas_execute_once(env, monkeypatch):
    entered = Event(); release = Event()
    original = svc.execute_query_rows_stream
    def slow(*args, **kwargs):
        entered.set(); assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(svc, 'execute_query_rows_stream', slow)
    def request():
        with caller():
            return mint(env, store=AtomicStore(env[1].backend))
    with ThreadPoolExecutor(16) as pool:
        owner = pool.submit(request)
        try:
            assert entered.wait(5)
            contenders = [pool.submit(request) for _ in range(15)]
            for future in contenders:
                with pytest.raises(SnapshotIdempotencyError) as exc:
                    future.result()
                assert exc.value.code == 'SNAPSHOT_IN_PROGRESS'
        finally: release.set()
        result = owner.result()
        assert all(reply == result for reply in pool.map(lambda _: request(), range(16)))
    assert len(env[2]) == 1


def test_changed_sql_columns_or_schemas_get_separate_snapshots(env):
    with caller(columns=['db.t.id']):
        first = mint(env)
        assert mint(env, sql='SELECT id FROM t LIMIT 1')['snapshot_id'] != first['snapshot_id']
    with caller(columns=['db.t.secret']):
        assert mint(env)['snapshot_id'] != first['snapshot_id']
    with caller(columns=['db.t.id'], schemas=[]):
        assert mint(env)['snapshot_id'] != first['snapshot_id']
    assert len(env[2]) == 4


def test_keys_are_caller_and_session_scoped(env):
    results=[]
    for user, session in [('alice','s1'), ('bob','s1'), ('alice','s2')]:
        with caller(user,session): results.append(mint(env)['snapshot_id'])
    assert len(set(results)) == len(env[2]) == 3


def test_failure_does_not_automatically_reexecute(env, monkeypatch):
    original = svc.execute_query_rows_stream
    calls=[]
    def fail(*a, **kw):
        calls.append(1); raise RuntimeError('backend failed')
    monkeypatch.setattr(svc,'execute_query_rows_stream',fail)
    with caller():
        with pytest.raises(RuntimeError): mint(env)
        with pytest.raises(SnapshotIdempotencyError) as exc: mint(env)
        assert exc.value.code == 'SNAPSHOT_CREATION_FAILED'
        assert len(calls)==1
        monkeypatch.setattr(svc,'execute_query_rows_stream',original)
    with caller(session='new-session'):
        assert mint(env)['row_count']==2


def test_metadata_commit_recovers_lost_response(env, monkeypatch):
    original = svc._create_snapshot
    def lost_response(*a, **kw):
        original(*a, **kw)
        raise RuntimeError('worker died after metadata commit')
    monkeypatch.setattr(svc,'_create_snapshot',lost_response)
    with caller():
        with pytest.raises(RuntimeError): mint(env)
        result=mint(env)
        assert result['row_count']==2
    assert len(env[2])==1


def test_failed_claim_status_update_does_not_discard_success(env, monkeypatch):
    original=env[1].write_idempotency
    def fail_status(key,value,**kw):
        if value['status']!='pending':raise OSError('store unavailable')
        return original(key,value,**kw)
    monkeypatch.setattr(env[1],'write_idempotency',fail_status)
    with caller():
        result=mint(env)
        assert mint(env)==result
    assert len(env[2])==1


def test_expired_claim_replaced_with_new_generation(env):
    with caller():
        first=mint(env)
        key=next(iter(env[1].versions))
        old,version=env[1].read_idempotency(key)
        env[1].write_idempotency(key,{**old,'expires_at':0},version=version)
        assert mint(env)['snapshot_id']!=first['snapshot_id']
        # A late former owner cannot overwrite the new generation.
        assert env[1].write_idempotency(key,old,version=version) is None
    assert len(env[2])==2


def test_token_refresh_reuses_snapshot_if_access_identity_is_unchanged(env):
    with caller(token='old-token', extra_claims={'exp':100, 'iat':1}):
        first = mint(env)
    with caller(token='refreshed-token', extra_claims={'exp':200, 'iat':2}):
        assert mint(env) == first
    assert len(env[2]) == 1


def test_row_access_jti_change_must_not_reuse_snapshot(env):
    with caller(user='jti-old'):
        first = mint(env)
    with caller(user='jti-new'):
        assert mint(env)['snapshot_id'] != first['snapshot_id']
    assert len(env[2]) == 2


def test_missing_principal_fails_before_claim_or_query(env):
    token = current_principal.set(None)
    try:
        with pytest.raises(QueryValidationError): mint(env)
    finally:
        current_principal.reset(token)
    assert env[2] == [] and env[1].objects == {}


def test_uncertain_metadata_write_retains_commit_for_replay(env, monkeypatch):
    original=env[1].put_file
    def timeout_after_write(path,key):
        original(path,key)
        if key.endswith('/meta.json'):
            raise TimeoutError('response lost after storage commit')
    monkeypatch.setattr(env[1],'put_file',timeout_after_write)
    with caller():
        with pytest.raises(TimeoutError): mint(env)
        recovered=mint(env)
        assert recovered['row_count']==2
        assert svc.page_snapshot(recovered['snapshot_id'],0,10,env[0],store=env[1])['rows']==[[1],[2]]
    assert len(env[2])==1


def test_column_order_does_not_change_fingerprint(env):
    with caller(columns=['db.t.id','db.t.name']):
        first = mint(env)
    with caller(columns=['db.t.name','db.t.id']):
        assert mint(env) == first
    assert len(env[2]) == 1


def test_same_row_access_claims_but_different_subject_is_isolated(env):
    from dataclasses import replace
    with caller():
        first = mint(env)
        token = current_principal.set(replace(current_principal.get(), subject='another-user'))
        try:
            assert mint(env)['snapshot_id'] != first['snapshot_id']
        finally:
            current_principal.reset(token)
    assert len(env[2]) == 2


@pytest.mark.parametrize('operation', ['page', 'urls', 'download', 'refresh'])
def test_snapshot_cannot_be_read_or_refreshed_by_another_session(env, operation):
    from app.errors import SnapshotNotFoundError
    with caller(session='owner'):
        snapshot = mint(env)['snapshot_id']
    with caller(session='other'), pytest.raises(SnapshotNotFoundError):
        if operation == 'page':
            svc.page_snapshot(snapshot, 0, 10, env[0], store=env[1])
        elif operation == 'urls':
            svc.download_urls(snapshot, env[0], store=env[1])
        elif operation == 'download':
            svc.download_part(snapshot, 1, env[0], store=env[1])
        else:
            svc.create_snapshot('SELECT id FROM t', env[0], store=env[1], refresh_of=snapshot)
    assert len(env[2]) == 1


def test_refresh_executes_once_then_reuses_replacement(env):
    with caller():
        first = mint(env)
        refresh = lambda sid: svc.create_snapshot('SELECT id FROM t', env[0], store=env[1], refresh_of=sid)
        second = refresh(first['snapshot_id'])
        assert second['snapshot_id'] != first['snapshot_id']
        assert refresh(first['snapshot_id']) == second
        third = refresh(second['snapshot_id'])
        assert third['snapshot_id'] not in {first['snapshot_id'], second['snapshot_id']}
        assert svc.page_snapshot(first['snapshot_id'], 0, 10, env[0], store=env[1])['row_count'] == 2
    assert len(env[2]) == 3


def test_removed_refresh_source_does_not_block_replay_of_committed_replacement(env):
    with caller():
        first = mint(env)
        refresh = lambda: svc.create_snapshot('SELECT id FROM t', env[0], store=env[1], refresh_of=first['snapshot_id'])
        second = refresh()
        for key in list(env[1].objects):
            if first['snapshot_id'] in key:
                del env[1].objects[key]
        assert refresh() == second
    assert len(env[2]) == 2


def test_existing_snapshot_is_not_readable_under_changed_scope(env):
    from app.errors import SnapshotScopeChangedError
    with caller(columns=['db.t.id']):
        first = mint(env)
    with caller(columns=[]), pytest.raises(SnapshotScopeChangedError):
        svc.page_snapshot(first['snapshot_id'], 0, 10, env[0], store=env[1])


def test_refresh_can_use_expired_but_retained_source_metadata(env):
    with caller():
        first = mint(env)
        for key, value in list(env[1].objects.items()):
            if key.endswith('/meta.json'):
                meta = json.loads(value)
                meta['expires_at'] = '2000-01-01T00:00:00Z'
                env[1].objects[key] = json.dumps(meta).encode()
        refreshed = svc.create_snapshot('SELECT id FROM t', env[0], store=env[1],
                                       refresh_of=first['snapshot_id'])
        assert refreshed['snapshot_id'] != first['snapshot_id']
    assert len(env[2]) == 2


@pytest.mark.parametrize('operation', ['page', 'urls', 'download'])
def test_scope_change_blocks_all_old_data_then_rebuilds_once(env, operation):
    from app.errors import SnapshotScopeChangedError
    with caller(columns=['db.t.id', 'db.t.name']):
        first = mint(env)['snapshot_id']
    with caller(columns=['db.t.id']):
        with pytest.raises(SnapshotScopeChangedError):
            if operation == 'page':
                svc.page_snapshot(first, 0, 10, env[0], store=env[1])
            elif operation == 'urls':
                svc.download_urls(first, env[0], store=env[1])
            else:
                svc.download_part(first, 1, env[0], store=env[1])
        second = svc.rebuild_snapshot(first, env[0], store=env[1])
        assert svc.rebuild_snapshot(first, env[0], store=env[1]) == second
        assert second['snapshot_id'] != first
        assert 'source_sql' not in second
        assert svc.page_snapshot(second['snapshot_id'], 0, 10, env[0], store=env[1])['row_count'] == 2
    assert len(env[2]) == 2


def test_rebuild_denies_query_no_longer_allowed(env, monkeypatch):
    from app.errors import ColumnScopeError
    with caller(columns=['db.t.id']):
        first = mint(env)['snapshot_id']
    def deny(*a, **kw):
        raise ColumnScopeError('Column no longer allowed.', 'COLUMN_NOT_ALLOWED')
    monkeypatch.setattr(svc, '_enforce_query_guardrails', deny)
    with caller(columns=[]), pytest.raises(ColumnScopeError):
        svc.rebuild_snapshot(first, env[0], store=env[1])
    assert len(env[2]) == 1


def test_foreign_session_cannot_rebuild_stored_sql(env):
    from app.errors import SnapshotNotFoundError
    with caller(session='owner'):
        first = mint(env)['snapshot_id']
    with caller(session='foreign'), pytest.raises(SnapshotNotFoundError):
        svc.rebuild_snapshot(first, env[0], store=env[1])
    assert len(env[2]) == 1
