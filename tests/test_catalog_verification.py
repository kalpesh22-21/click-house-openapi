import json
from unittest.mock import MagicMock

import pytest

from app import catalog_verification as verification
from app.config import Settings
from app.principal import Principal, current_principal


@pytest.mark.parametrize(
    "logical,physical,expected",
    [
        ("Nullable(String)", "varchar", True),
        ("LowCardinality(FixedString(3))", "varchar(3)", True),
        ("Date32", "date", True),
        ("DateTime64(6)", "timestamp(6)", True),
        ("DateTime64(6)", "timestamp(3)", False),
        ("Nullable(Decimal(19,2))", "decimal(20,3)", True),
        ("Decimal(19,2)", "decimal(18,2)", False),
        ("UInt32", "bigint", True),
        ("UInt32", "integer", False),
        ("String", "bigint", False),
        ("Unknown", "unknown", False),
    ],
)
def test_type_contract(logical, physical, expected):
    assert verification.compatible_type(logical, physical, trino=True) is expected


@pytest.mark.parametrize(
    "columns",
    [
        [],
        [("id", "bigint")],
        [("id", "varchar"), ("unexpected", "varchar")],
        [("id", "varchar"), ("id", "varchar")],
    ],
)
def test_column_mismatches_fail(columns):
    with pytest.raises(verification.CatalogVerificationError, match="db.table"):
        verification.verify_columns(
            "db.table", {"columns": {"id": {"type": "String"}}}, columns, trino=True
        )


def test_explicit_internal_columns_are_allowed():
    verification.verify_columns(
        "db.table",
        {
            "columns": {"id": {"type": "String"}},
            "mcp_projection": {"hidden_columns": ["tenant"]},
        },
        [("id", "varchar"), ("tenant", "varchar")],
        trino=True,
    )


def test_missing_verification_config_fails_closed():
    settings = Settings(trino_enabled=True, trino_host="test", _env_file=None)
    with pytest.raises(
        verification.CatalogVerificationError, match="CATALOG_VERIFICATION_TARGETS_PATH"
    ):
        verification.verify_catalog(settings)
    assert settings._verified_catalog_shards == frozenset()


@pytest.mark.parametrize("mode", ["views", "system_query"])
def test_startup_probes_each_target_and_resets_identity(monkeypatch, tmp_path, mode):
    from sqlglot import exp

    settings = Settings(
        trino_enabled=True,
        trino_host="test",
        trino_mysql_source_mode=mode,
        _env_file=None,
    )
    entry = {
        "database": "db",
        "table": "table",
        "source": "mysql",
        "columns": {"id": {"type": "String"}},
    }
    monkeypatch.setattr(verification, "logical_catalog", lambda _: {"db.table": entry})
    claims = {"proc_center": "pc", "clientcode": "tenant", "user_name": "verifier"}
    monkeypatch.setattr(
        verification,
        "_verification_targets",
        lambda *_: [
            {"shard": "one", "claims": claims},
            {"shard": "two", "claims": claims},
        ],
    )
    monkeypatch.setattr("app.shard_routing._get_shard_registry", lambda _: [])
    monkeypatch.setattr(
        "app.shard_routing._resolve_from_entries", MagicMock(side_effect=["one", "two"])
    )
    monkeypatch.setattr(
        "app.shard_routing._catalog_name_verified", lambda name, _: name
    )
    relation = MagicMock(return_value=exp.to_table("db.table"))
    monkeypatch.setattr("app.trino_native_sources.source_relation", relation)
    cursor = MagicMock(description=[("id", "varchar")])
    conn = MagicMock()
    conn.cursor.return_value = cursor
    monkeypatch.setattr("app.trino_client._connect", lambda *_: conn)
    original = Principal("caller")
    token = current_principal.set(original)
    try:
        verification.verify_catalog(settings)
        assert current_principal.get() is original
    finally:
        current_principal.reset(token)
    assert settings._verified_catalog_shards == frozenset({"one", "two"})
    assert [call.args[1] for call in relation.call_args_list] == ["one", "two"]
    assert cursor.execute.call_count == 2
    assert all("LIMIT 0" in call.args[0] for call in cursor.execute.call_args_list)
    assert conn.close.call_count == 2
    with pytest.raises(Exception, match="not passed catalog verification"):
        verification.require_verified_shard("three", settings)


def test_target_inventory_requires_all_shards(monkeypatch, tmp_path):
    path = tmp_path / "targets.json"
    path.write_text(json.dumps([{"shard": "one", "claims": {"user": "verifier"}}]))
    settings = Settings(
        trino_enabled=True,
        trino_host="test",
        catalog_verification_targets_path=str(path),
        _env_file=None,
    )
    monkeypatch.setattr(
        "app.shard_routing._get_shard_registry",
        lambda _: [
            {
                "env_type": "PROD",
                "database": "dbonline",
                "clientstart": "A",
                "catalog": "one",
            },
            {
                "env_type": "PROD",
                "database": "dbonline",
                "clientstart": "B",
                "catalog": "two",
            },
        ],
    )
    monkeypatch.setattr("app.shard_routing._catalog_name_for", lambda e: e["catalog"])
    monkeypatch.setattr(
        "app.shard_routing._catalog_name_verified", lambda name, _: name
    )
    with pytest.raises(verification.CatalogVerificationError, match="exactly cover"):
        verification._verification_targets(settings, True)


def test_source_failure_is_sanitized_and_identity_reset(monkeypatch):
    settings = Settings(trino_enabled=True, trino_host="test", _env_file=None)
    monkeypatch.setattr(verification, "logical_catalog", lambda _: {})
    monkeypatch.setattr(
        verification,
        "_verification_targets",
        lambda *_: [
            {
                "claims": {
                    "proc_center": "pc",
                    "clientcode": "tenant",
                    "user_name": "verifier",
                }
            }
        ],
    )
    monkeypatch.setattr(
        "app.trino_client._connect", MagicMock(side_effect=RuntimeError("secret SQL"))
    )
    with pytest.raises(verification.CatalogVerificationError) as exc:
        verification.verify_catalog(settings)
    assert "secret" not in str(exc.value)
    assert current_principal.get() is None
    assert settings._verified_catalog_shards == frozenset()


def test_mcp_verifies_once_at_http_startup(monkeypatch):
    from starlette.testclient import TestClient

    from app import mcp_server

    verify = MagicMock()
    monkeypatch.setattr(verification, "verify_catalog", verify)
    app = mcp_server.create_http_app()
    with TestClient(app) as client:
        verify.assert_called_once_with(mcp_server.settings)
        # Stateless MCP creates a low-level lifespan for each request. Neither
        # that lifespan nor health requests may repeat startup verification.
        for _ in range(2):
            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream"},
            )
        verify.assert_called_once()


def test_mcp_verification_failure_prevents_http_startup(monkeypatch):
    from starlette.testclient import TestClient

    from app import mcp_server

    monkeypatch.setattr(
        verification,
        "verify_catalog",
        MagicMock(side_effect=verification.CatalogVerificationError("mismatch")),
    )
    with (
        pytest.raises(verification.CatalogVerificationError, match="mismatch"),
        TestClient(mcp_server.create_http_app()),
    ):
        pytest.fail("must not serve requests")
