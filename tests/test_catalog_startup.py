"""Startup validates the local contract without requiring tenant-source access."""

from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.semantic_catalog import loader


@pytest.fixture
def no_backend_access(monkeypatch):
    connect = MagicMock(side_effect=AssertionError("startup accessed tenant sources"))
    monkeypatch.setattr("app.trino_client._connect", connect)
    monkeypatch.setattr("app.clickhouse_client.get_client", connect)
    monkeypatch.setattr("app.shard_routing._get_shard_registry", connect)
    return connect


def test_rest_startup_needs_no_tenant_or_shard_inventory(monkeypatch, no_backend_access):
    from app import main

    settings = Settings(trino_enabled=True, trino_host="test", _env_file=None)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    with TestClient(main.app):
        pass
    no_backend_access.assert_not_called()


def test_mcp_http_validates_local_catalog_once(monkeypatch, no_backend_access):
    from app import mcp_server

    validate = MagicMock(wraps=loader.get_semantic_catalog)
    monkeypatch.setattr(loader, "get_semantic_catalog", validate)
    with TestClient(mcp_server.create_http_app()) as client:
        for _ in range(2):
            response = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert response.status_code == 200
        validate.assert_called_once_with()
    no_backend_access.assert_not_called()


def test_mcp_invalid_local_catalog_blocks_startup(monkeypatch, no_backend_access):
    from app import mcp_server

    monkeypatch.setattr(
        loader, "get_semantic_catalog",
        MagicMock(side_effect=loader.CatalogValidationError("invalid RLS projection")),
    )
    with pytest.raises(loader.CatalogValidationError, match="invalid RLS projection"):
        with TestClient(mcp_server.create_http_app()):
            pytest.fail("must not serve an invalid catalog")
    no_backend_access.assert_not_called()


def test_mcp_stdio_validates_before_serving(monkeypatch, no_backend_access):
    from argparse import Namespace
    from app import mcp_server

    monkeypatch.setattr(mcp_server, "_parse_args", lambda: Namespace(transport="stdio"))
    validate = MagicMock(wraps=loader.get_semantic_catalog)
    monkeypatch.setattr(loader, "get_semantic_catalog", validate)
    run = MagicMock(side_effect=lambda **_: validate.assert_called_once_with())
    monkeypatch.setattr(mcp_server.mcp, "run", run)
    mcp_server.main()
    run.assert_called_once_with(transport="stdio")
    no_backend_access.assert_not_called()
