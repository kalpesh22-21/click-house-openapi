"""Unit tests for app.clickhouse_client construction guarantees.

These tests do NOT require a live ClickHouse server — they patch
clickhouse_connect.get_client and assert how we construct the client.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.clickhouse_client import _build_client
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
