"""Unit tests for app.clickhouse_client construction guarantees.

These tests do NOT require a live ClickHouse server — they patch
clickhouse_connect.get_client and assert how we construct the client.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.clickhouse_client import _build_client, ping
from app.config import get_settings


def _settings():
    get_settings.cache_clear()
    return get_settings()


def test_build_client_disables_session_id_for_thread_safety():
    """The shared singleton must be built with autogenerate_session_id=False.

    THREAD SAFETY REGRESSION GUARD: clickhouse-connect's sync client defaults
    autogenerate_session_id=True, which stamps one session_id on every query
    from the process-wide singleton. ClickHouse forbids concurrent queries in a
    single session, so the driver raises ProgrammingError the moment two
    threadpool requests share the client. Passing autogenerate_session_id=False
    keeps the query path stateless and safe to share across threads. If someone
    drops this kwarg, this test fails before the bug reaches production.
    """
    with patch("app.clickhouse_client.clickhouse_connect.get_client") as mock_get:
        mock_get.return_value = MagicMock()
        _build_client(_settings())

    assert mock_get.call_count == 1
    kwargs = mock_get.call_args.kwargs
    assert kwargs.get("autogenerate_session_id") is False, (
        "ClickHouse client must be built with autogenerate_session_id=False so "
        "the shared singleton is safe under concurrent threadpool requests."
    )


def test_build_client_sets_max_connection_age_from_config():
    """_build_client must apply the configured max_connection_age global.

    STALE-SOCKET REGRESSION GUARD: behind CHProxy/an LB, a pooled connection that
    outlives the proxy's idle timeout goes stale and the next request hits
    RemoteDisconnected. clickhouse-connect's own default (600s) is longer than a
    typical proxy idle timeout, so we drive the process-global `max_connection_age`
    from config to rotate connections before the proxy kills them. If someone drops
    this call, the stale-socket window silently reopens.
    """
    settings = _settings()
    settings.clickhouse_max_connection_age = 45

    with patch("app.clickhouse_client.clickhouse_connect.get_client") as mock_get, patch(
        "app.clickhouse_client.cc_common.set_setting"
    ) as mock_set:
        mock_get.return_value = MagicMock()
        _build_client(settings)

    assert ("max_connection_age", 45) in [
        c.args for c in mock_set.call_args_list
    ], (
        "clickhouse-connect max_connection_age must be set from "
        "settings.clickhouse_max_connection_age so pooled sockets rotate before a "
        "proxy/LB idle-timeout can make them stale."
    )


# ---------------------------------------------------------------------------
# ping() stale-socket resilience
# ---------------------------------------------------------------------------


def test_ping_retries_once_on_stale_singleton_socket():
    """A stale keep-alive socket makes the first singleton ping return False;
    the retry rides a fresh connection and succeeds, so /health does not flap.
    """
    client = MagicMock()
    client.ping.side_effect = [False, True]  # stale socket, then fresh socket
    with patch("app.clickhouse_client.get_client", return_value=client):
        assert ping() is True
    assert client.ping.call_count == 2


def test_ping_recovers_when_stale_socket_raises():
    """If the stale socket raises (RemoteDisconnected) instead of returning
    False, the singleton path still retries on a fresh connection.
    """
    client = MagicMock()
    client.ping.side_effect = [ConnectionResetError("remote disconnected"), True]
    with patch("app.clickhouse_client.get_client", return_value=client):
        assert ping() is True
    assert client.ping.call_count == 2


def test_ping_returns_false_when_server_genuinely_down():
    """A truly unreachable server fails both attempts and reports False."""
    client = MagicMock()
    client.ping.return_value = False
    with patch("app.clickhouse_client.get_client", return_value=client):
        assert ping() is False
    assert client.ping.call_count == 2


def test_ping_explicit_settings_does_not_retry():
    """The explicit-settings path builds a fresh client per call, so there is no
    pooled socket to go stale — only a single ping is attempted (no retry churn).
    """
    client = MagicMock()
    client.ping.return_value = False
    with patch("app.clickhouse_client.get_client", return_value=client):
        assert ping(_settings()) is False
    assert client.ping.call_count == 1
