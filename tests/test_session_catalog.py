"""Session initialization, signed grants, replay binding and live enforcement."""
from dataclasses import replace
from unittest.mock import MagicMock
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.catalog_scope import CatalogScope
from app.config import Settings
from app.principal import Principal
from app.session_catalog import materialize, verify_scope, SessionScopeError
from tests.jwt_helpers import make_jwt


@pytest.fixture
def setup(monkeypatch):
    settings = Settings(_env_file=None, mcp_service_key='test-scope-signing-secret-at-least-32-bytes', require_sid_binding=False)
    catalog = {'db.t': {'database': 'db', 'table': 't', 'columns': {
        'id': {'type': 'UInt64'}, 'secret': {'type': 'String'},
        'name': {'type': 'String', 'context_sql': "SELECT 'private'"},
    }}}
    monkeypatch.setattr('app.session_catalog.get_semantic_catalog', lambda: catalog)
    monkeypatch.setattr('app.session_catalog.get_catalog_sha', lambda: 'v1')
    permissions = MagicMock(return_value=CatalogScope(columns=frozenset({'db.t.id', 'db.t.name'})))
    monkeypatch.setattr('app.catalog_scope.query_permission_scope', permissions)
    principal = Principal('alice', {'sub': 'alice', 'clientcode': 'A', 'user_name': 'alice', 'jti': 'alice', 'proc_center': 'pc'})
    return settings, principal, permissions


def test_materialize_once_then_verify_without_permission_sql(setup):
    settings, principal, permissions = setup
    snapshot = materialize(principal, 's1', settings)
    assert set(snapshot['catalog']['db.t']['columns']) == {'id', 'name'}
    assert 'context_sql' not in snapshot['catalog']['db.t']['columns']['name']
    for _ in range(3):
        scoped = verify_scope(principal, 's1', snapshot['scope_token'], settings)
        assert scoped.catalog_scope.allows_column('db.t', 'id')
        assert not scoped.catalog_scope.allows_column('db.t', 'secret')
    assert permissions.call_count == 1


@pytest.mark.parametrize('change', ['tenant', 'subject', 'session', 'tamper', 'expired', 'wrong_key', 'missing', 'stale'])
def test_scope_replay_rejected(setup, monkeypatch, change):
    settings, principal, _ = setup
    signed = materialize(principal, 's1', settings)['scope_token']
    sid = 's1'
    if change == 'tenant': principal = replace(principal, claims={**principal.claims, 'clientcode': 'B'})
    if change == 'subject': principal = replace(principal, subject='bob')
    if change == 'session': sid = 's2'
    if change == 'tamper': signed = signed[:-8] + 'AAAAAAAA'
    if change == 'wrong_key': settings.mcp_service_key = 'another-signing-secret-at-least-32-bytes'
    if change == 'missing': signed = None
    if change == 'stale': monkeypatch.setattr('app.session_catalog.get_catalog_sha', lambda: 'v2')
    if change == 'expired':
        claims = jwt.decode(signed, options={'verify_signature': False})
        claims['exp'] = int(time.time()) - 1
        signed = jwt.encode(claims, settings.mcp_service_key, algorithm='HS256', headers={'typ':'mcp-session-scope+jwt'})
    with pytest.raises(SessionScopeError): verify_scope(principal, sid, signed, settings)


def test_deny_all_stays_empty(setup):
    settings, principal, permissions = setup
    permissions.return_value = CatalogScope(columns=frozenset())
    snapshot = materialize(principal, 's', settings)
    assert snapshot['catalog'] == {}
    assert verify_scope(principal, 's', snapshot['scope_token'], settings).catalog_scope.columns == frozenset()


def test_access_token_renewal_preserves_scope(setup):
    settings, principal, _ = setup
    snapshot = materialize(principal, 's', settings)
    renewed = replace(principal, claims={**principal.claims, 'exp':time.time()+100, 'iat':time.time()})
    verify_scope(renewed, 's', snapshot['scope_token'], settings)


def test_mcp_bootstrap_and_scope_header_required(setup, monkeypatch):
    import app.mcp_server as server
    settings, _, permissions = setup
    monkeypatch.setattr(server, 'settings', settings)
    headers = {'Authorization': 'Bearer '+make_jwt(), 'X-Session-Id':'s1'}
    app = server.JWTAuthMiddleware(server.create_http_app(), settings=settings)
    with TestClient(app) as client:
        missing = client.get('/catalog/export', headers=headers)
        assert missing.status_code == 403
        response = client.post('/session/catalog', headers=headers)
        assert response.status_code == 200, response.text
        assert permissions.call_count == 1
        signed = response.json()['scope_token']
        # Protocol discovery is also protected without recalculating permissions.
        response = client.post('/mcp', headers={**headers, 'X-Catalog-Scope':signed,
            'Accept':'application/json, text/event-stream'}, json={'jsonrpc':'2.0','id':1,'method':'initialize',
            'params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'test','version':'1'}}})
        assert response.status_code == 200, response.text
        assert permissions.call_count == 1
        response = client.post('/mcp', headers={**headers,'X-Catalog-Scope':signed,'X-Session-Id':'s2'},json={})
        assert response.status_code == 403


def test_missing_service_key_fails_closed(setup):
    settings, principal, _ = setup
    settings.mcp_service_key = ''
    with pytest.raises(SessionScopeError, match='Session catalog'):
        materialize(principal, 's', settings)


def test_rest_bootstrap_and_scope_enforcement(setup):
    from app.main import app
    from app.config import get_settings
    settings, _, permissions = setup
    app.dependency_overrides[get_settings] = lambda: settings
    headers = {'Authorization': 'Bearer '+make_jwt(), 'X-Session-Id': 'rest-session'}
    try:
        with TestClient(app) as client:
            response = client.post('/session/catalog', headers=headers)
            assert response.status_code == 200, response.text
            assert response.headers['cache-control'] == 'no-store'
            signed = response.json()['scope_token']
            assert permissions.call_count == 1
            denied = client.get('/databases', headers=headers)
            assert denied.status_code == 403
            allowed = client.get('/databases', headers={**headers, 'X-Catalog-Scope': signed})
            assert allowed.status_code == 200, allowed.text
            assert permissions.call_count == 1
    finally:
        app.dependency_overrides.pop(get_settings, None)
