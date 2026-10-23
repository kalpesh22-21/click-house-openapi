"""Trino scratch routing preserves session ownership and live CH types."""

# Fixtures are imported from the shared Trino harness.
# ruff: noqa: F401, F811
from unittest.mock import MagicMock

import pytest

from app import scratch_metadata, service, trino_client
from app.errors import ColumnScopeError, TableNotFoundError
from app.principal import current_session_id
from tests.test_trino import principal, shard_registry_path, trino_settings

OWN = "s_scratchtest_bp_0123456789abcdef0123456789abcdef"
FOREIGN = "s_another_bp_0123456789abcdef0123456789abcdef"


@pytest.fixture
def scratch(trino_settings, monkeypatch):
    settings = trino_settings.model_copy(
        update={"allowed_databases": "dbpcm_warehouse,dbpcm_scratch"}
    )
    token = current_session_id.set("scratchtest")
    client = MagicMock()
    client.query.return_value.result_rows = [
        ["employee_code", "String", ""],
        ["earnings", "Nullable(Float64)", ""],
    ]
    monkeypatch.setattr(scratch_metadata, "build_scratch_client", lambda _: client)
    yield settings, client
    current_session_id.reset(token)


def test_scratch_rewrite_uses_physical_ch_table_and_live_nullable_types(
    scratch, principal
):
    settings, client = scratch
    sql = trino_client.rewrite_query(
        f"SELECT sum(earnings) FROM dbpcm_scratch.{OWN}", settings
    )
    assert f'"clickhouse"."dbpcm_scratch"."{OWN}"' in sql
    assert "COALESCE" not in sql
    assert "system.query" not in sql
    client.close.assert_called_once()


@pytest.mark.parametrize("session", ["different", None])
def test_foreign_or_missing_session_is_rejected_before_metadata(
    scratch, principal, session
):
    settings, client = scratch
    token = current_session_id.set(session)
    try:
        with pytest.raises(ColumnScopeError, match="session"):
            trino_client.rewrite_query(
                f"SELECT count(*) FROM dbpcm_scratch.{OWN}", settings
            )
        client.query.assert_not_called()
    finally:
        current_session_id.reset(token)


def test_all_scratch_relations_authorized_before_any_metadata(scratch, principal):
    settings, client = scratch
    with pytest.raises(ColumnScopeError):
        trino_client.rewrite_query(
            f"SELECT * FROM dbpcm_scratch.{OWN} a JOIN dbpcm_scratch.{FOREIGN} b ON a.employee_code=b.employee_code",
            settings,
        )
    client.query.assert_not_called()


def test_missing_or_expired_scratch_table_fails(scratch, principal):
    settings, client = scratch
    client.query.return_value.result_rows = []
    with pytest.raises(TableNotFoundError):
        trino_client.rewrite_query(
            f"SELECT count(*) FROM dbpcm_scratch.{OWN}", settings
        )


def test_trino_advertises_only_owned_uploads(scratch):
    settings, client = scratch
    upload = OWN.replace("_bp_", "_upload_")
    client.query.return_value.result_rows = [
        ["dbpcm_scratch", OWN, "MergeTree"],
        ["dbpcm_scratch", upload, "MergeTree"],
        ["dbpcm_scratch", FOREIGN.replace("_bp_", "_upload_"), "MergeTree"],
    ]
    assert {"name": "dbpcm_scratch"} in service.list_databases(settings)
    assert [t["name"] for t in service.list_tables("dbpcm_scratch", settings)] == [
        upload
    ]


def test_trino_scratch_schema_uses_live_columns(scratch):
    settings, _ = scratch
    result = service.get_table_schema("dbpcm_scratch", OWN, settings)
    assert [c["name"] for c in result["columns"]] == ["employee_code", "earnings"]
    assert result["catalogued"] is False


def test_owned_scratch_schema_is_not_trimmed_by_static_catalog_scope(scratch):
    from app.principal import current_scope

    settings, _ = scratch
    token = current_scope.set(frozenset({"dbpcm_warehouse.employee.employee_code"}))
    try:
        result = service.get_table_schema("dbpcm_scratch", OWN, settings)
        assert [c["name"] for c in result["columns"]] == ["employee_code", "earnings"]
    finally:
        current_scope.reset(token)


def test_scratch_join_preserves_alias_and_warehouse_routing(scratch, principal):
    settings, _ = scratch
    sql = trino_client.rewrite_query(
        f"SELECT p.employee_code, sum(s.earnings) FROM dbpcm_scratch.{OWN} s "
        "JOIN dbpcm_warehouse.payroll p ON p.employee_code=s.employee_code GROUP BY p.employee_code",
        settings,
    )
    assert f'"clickhouse"."dbpcm_scratch"."{OWN}" AS s' in sql
    assert "system.query" in sql


@pytest.mark.parametrize("expression", ["lowerUTF8('ÉCOLE')", "lowerUTF8(NULL)"])
def test_blueprint_domain_lower_utf8_conversion(scratch, principal, expression):
    settings, _ = scratch
    sql = trino_client.rewrite_query(f"SELECT {expression}", settings)
    assert "LOWER(" in sql
    assert "LOWERUTF8" not in sql.upper()


def test_lower_utf8_does_not_accept_numeric_input(scratch, principal):
    from app.errors import QueryValidationError

    settings, _ = scratch
    with pytest.raises(QueryValidationError):
        trino_client.rewrite_query("SELECT lowerUTF8(123)", settings)


def test_scratch_sample_star_has_live_provenance_metadata(
    scratch, principal, monkeypatch
):
    settings, _ = scratch
    monkeypatch.setattr(
        service,
        "_execute",
        lambda *args, **kwargs: (["employee_code", "earnings"], [["E001", 10.0]]),
    )
    result = service.sample_rows(
        "dbpcm_scratch", OWN, limit=1, settings=settings, include_provenance=True
    )
    assert result["rows"] == [["E001", 10.0]]


def test_uploaded_cte_star_can_be_scope_validated(scratch, principal, monkeypatch):
    settings, _ = scratch
    monkeypatch.setattr(service, '_execute', lambda *args, **kwargs: (['earnings'], [[10.0]]))
    result = service.run_query(
        f'WITH uploaded AS (SELECT * FROM dbpcm_scratch.{OWN}) SELECT earnings FROM uploaded',
        settings=settings, include_provenance=True,
    )
    assert result['rows'] == [[10.0]]
