"""Hermetic ITDBA inventory, cache and pre-existing catalog contracts."""
import json
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app import shard_routing as routing, trino_client
from app.http_security import NoRedirects
from app.config import Settings
from app.errors import QueryValidationError


def row(**changes):
    return dict(dict(active="true", active_api="false", env_type="QA2", name="online",
                     mysqldb="dbonline", site="okc", level="76", mysqlhost="synthetic.test",
                     mysqlport="3306", clientstart="0OF00", clientend="0OJ99"), **changes)


@pytest.fixture
def settings(monkeypatch):
    routing.invalidate_shard_registry_cache()
    monkeypatch.setattr(routing, "_fetch_itdba_rows", lambda *args: [row()])
    yield Settings(_env_file=None, trino_host="synthetic.test", trino_env_type="QA2",
                   trino_catalog_check_enabled=False, trino_user="", trino_password="")
    routing.invalidate_shard_registry_cache()


def resolve(settings, code="0OG04"):
    return routing.resolve_mysql_shard(code, settings)


def test_catalog_and_boundaries(settings):
    for code in ("0OF00", "0OG04", "0OJ99"):
        assert resolve(settings, code) == "online__okc__76__dbonline"
    for code in ("0OE99", "0OK00"):
        with pytest.raises(QueryValidationError) as exc:
            resolve(settings, code)
        assert exc.value.code == "MYSQL_SHARD_NOT_FOUND"


def test_filters_replicas_and_spelling():
    rows = [row(site=" OKC ", level="0011", active="True"),
            row(site=" OKC ", level="0011", mysqlhost="replica.test"),
            row(active="false"), row(mysqldb="payroll"), row(name="bigdata_x"),
            row(clientstart=""), row(level=""), row(env_type="DR")]
    entries = routing.normalize_itdba_rows(rows)
    assert len(entries) == 1
    assert routing._catalog_name_for(entries[0]) == "online__OKC__0011__dbonline"
    assert routing._catalog_name_for(dict(entries[0], site="my-site")) == "online__my_site__0011__dbonline"


def test_group_conflict(settings, monkeypatch, caplog):
    monkeypatch.setattr(routing, "_fetch_itdba_rows", lambda *a: [row(), row(site="other")])
    with pytest.raises(QueryValidationError) as exc:
        resolve(settings)
    assert exc.value.code == "MYSQL_SHARD_REGISTRY_INVALID"
    assert "distinct_targets=2" in caplog.text


@pytest.mark.parametrize("rows", [[], [row(level="")], [row(site="bad;sql")], [row(clientend=None)]])
def test_invalid_inventory(rows):
    with pytest.raises(routing.ShardRegistryError):
        routing.normalize_itdba_rows(rows)


def test_count_band():
    with pytest.raises(routing.ShardRegistryError):
        routing.normalize_itdba_rows([row(clientstart=f"{i:05}", clientend=f"{i:05}") for i in range(10001)])


def offline(*args):
    raise OSError("sensitive upstream details")


def test_cache_write_ttl_and_cold_boot(settings, tmp_path, monkeypatch, caplog):
    clock = [0.0]
    monkeypatch.setattr(routing.time, "monotonic", lambda: clock[0])
    fetch = MagicMock(return_value=[row()])
    monkeypatch.setattr(routing, "_fetch_itdba_rows", fetch)
    settings.trino_itdba_cache_path = str(tmp_path / "cache.json")
    assert resolve(settings) == "online__okc__76__dbonline"
    payload = json.loads((tmp_path / "cache.json").read_text())
    assert payload["source"] == "itdba"
    fetch.side_effect = offline
    clock[0] = 299
    resolve(settings)
    assert fetch.call_count == 1
    clock[0] = 300
    resolve(settings)
    assert fetch.call_count == 2
    routing.invalidate_shard_registry_cache()
    resolve(settings)
    assert "event=shard_registry_stale_served" in caplog.text
    assert "sensitive upstream" not in caplog.text
    settings.trino_itdba_allow_stale = False
    with pytest.raises(QueryValidationError) as exc:
        resolve(settings)
    assert exc.value.code == "MYSQL_SHARD_REGISTRY_UNAVAILABLE"
    assert exc.value.message == "MySQL shard routing is temporarily unavailable."


@pytest.mark.parametrize("poison", ["truncated", "empty", "oversize", "unsafe", "wrong_filter", "bad_timestamp"])
def test_poisoned_cache_refused(settings, tmp_path, monkeypatch, poison):
    payload = routing.snapshot_for(routing.normalize_itdba_rows([row()]))
    if poison == "empty": payload["entries"] = []
    if poison == "oversize": payload["entries"] *= 10001
    if poison == "unsafe": payload["entries"][0]["name"] = "x; DROP"
    if poison == "wrong_filter": payload["entries"][0]["name"] = "other"
    if poison == "bad_timestamp": payload["generated_at"] = "no"
    path = tmp_path / "cache.json"
    path.write_text("{" if poison == "truncated" else json.dumps(payload))
    settings.trino_itdba_cache_path = str(path)
    monkeypatch.setattr(routing, "_fetch_itdba_rows", offline)
    with pytest.raises(QueryValidationError) as exc:
        resolve(settings)
    assert exc.value.code == "MYSQL_SHARD_REGISTRY_UNAVAILABLE"


@pytest.mark.parametrize("names, expected", [
    (["online__okc__76__dbonline"], "online__okc__76__dbonline"),
    (["online__OKC__76__dbonline"], "online__OKC__76__dbonline"),
    (["err__online__OKC__76__dbonline"], "online__okc__76__dbonline"),
    (["online__OKC__76__dbonline", "ONLINE__okc__76__dbonline"], "online__okc__76__dbonline"),
])
def test_catalog_check(settings, monkeypatch, names, expected):
    settings.trino_catalog_check_enabled = True
    conn = MagicMock()
    conn.cursor.return_value.fetchall.return_value = [[n] for n in names]
    connect = MagicMock(return_value=conn)
    monkeypatch.setattr(trino_client, "_connect", connect)
    assert resolve(settings) == expected
    assert resolve(settings) == expected
    connect.assert_called_once()
    conn.cursor.return_value.execute.assert_called_once_with("SHOW CATALOGS")
    conn.close.assert_called_once()


def test_catalog_check_failure(settings, monkeypatch, caplog):
    settings.trino_catalog_check_enabled = True
    monkeypatch.setattr(trino_client, "_connect", offline)
    assert resolve(settings) == "online__okc__76__dbonline"
    assert "catalog check unavailable" in caplog.text


@pytest.mark.parametrize("changes", [dict(trino_enabled=True, trino_host=""), dict(trino_user="u"),
    dict(trino_password="p"), dict(trino_itdba_name="bad;sql"),
    dict(trino_itdba_timeout_seconds=0), dict(trino_itdba_cache_ttl_seconds=0)])
def test_config_validation(changes):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **changes)


def test_shared_connection_auth_and_noauth(settings, monkeypatch):
    connect = MagicMock()
    monkeypatch.setattr("trino.dbapi.connect", connect)
    trino_client._connect(settings, "pc|client|user")
    assert connect.call_args.kwargs["auth"] is None
    assert connect.call_args.kwargs["user"] == "pc|client|user"
    settings.trino_user, settings.trino_password = "service", "fake-password"
    trino_client._connect(settings, "pc|client|user")
    kw = connect.call_args.kwargs
    assert kw["user"] == "pc|client|user" and kw["auth"] is not None
    assert kw["port"] == 443 and kw["http_scheme"] == "https"


@pytest.mark.parametrize("url", ["http://registry.test/api", "file:///etc/passwd",
    "https://user:secret@registry.test/api", "https://registry.test/api#fragment",
    "https://registry.test:bad/api", "https://registry.test/\napi"])
def test_registry_url_rejects_unsafe_configuration(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, trino_itdba_url=url)


def test_registry_url_is_not_bundled():
    assert Settings(_env_file=None).trino_itdba_url == ""


def test_registry_transport_boundaries(monkeypatch):
    response = MagicMock()
    response.status = 200
    response.read.return_value = json.dumps({"data": [row()]}).encode()
    response.__enter__.return_value = response
    opener = MagicMock()
    opener.open.return_value = response
    build = MagicMock(return_value=opener)
    monkeypatch.setattr(routing.urllib.request, "build_opener", build)
    assert routing._fetch_itdba_rows("https://registry.test/api", 2) == [row()]
    response.read.assert_called_once_with(routing.MAX_REGISTRY_BYTES + 1)
    assert opener.open.call_args.kwargs["timeout"] == 2
    assert any(isinstance(h, NoRedirects) for h in build.call_args.args)
    response.read.return_value = b"x" * (routing.MAX_REGISTRY_BYTES + 1)
    with pytest.raises(routing.ShardRegistryError):
        routing._fetch_itdba_rows("https://registry.test/api", 2)
    with pytest.raises(OSError):
        NoRedirects().redirect_request(None, None, 302, "", {}, "http://other.test")


def test_view_catalog_casing_is_preserved():
    from app.trino_metadata import physical_target
    settings = Settings(_env_file=None)
    entry = {"source": "mysql", "table": "employee"}
    target = physical_target(entry, "online__OKC__0011__dbonline", settings)
    assert target == ("online__OKC__0011__dbonline", "dbonline", "employee")
    with pytest.raises(QueryValidationError):
        physical_target(entry, 'bad"; SELECT 1', settings)


def test_authenticated_query_keeps_identity_and_aggregation_in_trino(settings, monkeypatch):
    from trino.auth import BasicAuthentication

    connect = MagicMock()
    monkeypatch.setattr("trino.dbapi.connect", connect)
    settings.trino_user, settings.trino_password = "service", "fake-password"
    for identity in ("pc|tenant_a|alice", "pc|tenant_b|bob"):
        trino_client._connect(settings, identity, mysql_catalog="online__OKC__0011__dbonline")
        kwargs = connect.call_args.kwargs
        assert kwargs["user"] == identity
        assert kwargs["auth"] == BasicAuthentication("service", "fake-password")
        assert kwargs["session_properties"]["online__OKC__0011__dbonline.aggregation_pushdown_enabled"] == "false"
    trino_client._connect(settings, "service")
    assert not any("aggregation_pushdown" in key for key in connect.call_args.kwargs["session_properties"])


def test_real_client_headers_separate_query_user_from_service_auth(settings):
    """Exercise the installed client's wire headers without opening a network connection."""
    from requests.auth import HTTPBasicAuth

    settings.trino_user, settings.trino_password = "service", "fake-password"
    conn = trino_client._connect(settings, "pc|tenant|alice", mysql_catalog="online__okc__01__dbonline")
    try:
        request = conn._create_request()
        headers = request.http_headers
        assert headers["X-Trino-User"] == "pc|tenant|alice"
        assert "online__okc__01__dbonline.aggregation_pushdown_enabled=false" in headers["X-Trino-Session"]
        assert conn._http_session.auth == HTTPBasicAuth("service", "fake-password")
    finally:
        conn.close()
