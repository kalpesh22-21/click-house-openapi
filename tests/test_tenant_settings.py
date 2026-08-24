"""Tests for per-tenant ClickHouse settings injection.

Covers the query-path behaviour added for row-level isolation:
  - tenant_settings() maps JWT claims to ClickHouse custom settings via the
    CLICKHOUSE_TENANT_SETTINGS env object, keyed off the request principal.
  - Safety caps (readonly + row/time limits) are applied LAST in the merge, so a
    tenant setting can never loosen them.
  - The principal is bound for the request and reset afterwards (no leakage onto
    pooled worker threads).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import anyio
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.clickhouse_client import readonly_settings, tenant_settings
from app.config import get_settings
from app.mcp_server import JWTAuthMiddleware
from app.principal import Principal, current_principal
from tests.jwt_helpers import make_jwt


def _principal(
    client_code: str = "CLIENT_A", proc_center: str = "PC01", jti: str = "TESTJTI001"
) -> Principal:
    return Principal(
        subject="sub-1",
        claims={"clientcode": client_code, "proc_center": proc_center, "jti": jti},
    )


# ---------------------------------------------------------------------------
# tenant_settings() — the claim -> setting mapping
# ---------------------------------------------------------------------------

class TestTenantSettingsBuilder:

    def test_no_principal_returns_empty(self):
        assert current_principal.get() is None
        assert tenant_settings(get_settings()) == {}

    def test_principal_maps_claims_to_paycom_settings(self):
        # The default map fills the three paycom_* settings the warehouse row
        # policies read from the caller's clientcode / proc_center / jti claims.
        token = current_principal.set(_principal())
        try:
            assert tenant_settings(get_settings()) == {
                "paycom_client_code": "CLIENT_A",
                "paycom_proc_center": "PC01",
                "paycom_authenticated_user": "TESTJTI001",
            }
        finally:
            current_principal.reset(token)

    def test_missing_mapped_claim_raises_403(self):
        # Defensive guard: a principal missing a mapped tenant claim must not
        # silently produce an untenanted query.
        token = current_principal.set(Principal(subject="s", claims={}))
        try:
            with pytest.raises(HTTPException) as exc:
                tenant_settings(get_settings())
            assert exc.value.status_code == 403
        finally:
            current_principal.reset(token)


# ---------------------------------------------------------------------------
# Merge precedence — safety caps always win
# ---------------------------------------------------------------------------

class TestSafetyCapsWinMerge:

    def test_safety_caps_applied_last(self):
        # Even if a tenant dict somehow carried a reserved key, the safety caps
        # merged last must overwrite it.  (The config validator also forbids
        # configuring such a key — this asserts the merge order independently.)
        tenant = {"paycom_client_code": "CLIENT_A", "readonly": 0, "max_result_rows": 999_999_999}
        s = get_settings()
        merged = {**tenant, **readonly_settings(s)}
        assert merged["readonly"] == s.clickhouse_readonly
        assert merged["max_result_rows"] == s.max_result_rows
        assert merged["paycom_client_code"] == "CLIENT_A"

    def test_configurable_readonly_level(self):
        with patch.dict(os.environ, {"CLICKHOUSE_READONLY": "2"}):
            get_settings.cache_clear()
            assert readonly_settings(get_settings())["readonly"] == 2
        # env restored; rebuild a clean (readonly=1) settings for later tests.
        get_settings.cache_clear()

    def test_final_applied_by_default(self):
        # Correctness-first default: FINAL rides every query so an LLM caller never
        # sees un-merged duplicate rows from a Replacing/Collapsing table.
        assert readonly_settings(get_settings())["final"] == 1

    def test_final_can_be_disabled(self):
        with patch.dict(os.environ, {"CLICKHOUSE_SELECT_FINAL": "false"}):
            get_settings.cache_clear()
            assert "final" not in readonly_settings(get_settings())
        # env restored; rebuild a clean settings for later tests.
        get_settings.cache_clear()

    def test_sequential_consistency_off_by_default(self):
        # Single-node default: no read-your-writes wait imposed on reads.
        assert "select_sequential_consistency" not in readonly_settings(get_settings())

    def test_sequential_consistency_on_in_cluster_mode(self):
        with patch.dict(os.environ, {"SCRATCH_CLUSTER": "prod_cluster"}):
            get_settings.cache_clear()
            assert readonly_settings(get_settings())["select_sequential_consistency"] == 1
        # env restored; rebuild a clean settings for later tests.
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# End-to-end: the tenant setting reaches client.query, safety caps intact
# ---------------------------------------------------------------------------

class TestEndToEndInjection:

    def test_query_injects_tenant_setting(self):
        result = MagicMock()
        result.column_names = ["x"]
        result.result_rows = [[1]]
        mock_client = MagicMock()
        mock_client.query.return_value = result
        mock_client.ping.return_value = True

        # Patch get_client (NOT execute_query) so the real merge runs.
        with patch("app.clickhouse_client.get_client", return_value=mock_client):
            from app.main import app

            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/query",
                headers={
                    "Authorization": f"Bearer {make_jwt(client_code='CLIENT_B', jti='JTI-BOB')}"
                },
                json={"sql": "SELECT 1"},
            )

        assert resp.status_code == 200, resp.text
        settings_arg = mock_client.query.call_args.kwargs["settings"]
        assert settings_arg["paycom_client_code"] == "CLIENT_B"
        assert settings_arg["paycom_authenticated_user"] == "JTI-BOB"
        assert settings_arg["readonly"] == 1  # safety cap intact alongside tenant


# ---------------------------------------------------------------------------
# Context propagation: principal is bound during, reset after (no leak)
# ---------------------------------------------------------------------------

class TestDriverCustomSettingTransmission:

    def test_invalid_setting_action_is_send(self):
        # Regression: clickhouse-connect validates setting names against the
        # server's known settings and, by default, REFUSES to transmit unknown
        # ones ("Setting SQL_tenant is unknown or readonly") — which silently
        # breaks per-tenant isolation, since custom settings aren't in
        # system.settings. Importing app.clickhouse_client must flip this to
        # 'send' so custom settings reach the server.
        import app.clickhouse_client  # noqa: F401 — import sets the global
        from clickhouse_connect import common

        assert common.get_setting("invalid_setting_action") == "send"


class TestPrincipalContextLifecycle:

    def test_middleware_sets_then_resets_principal(self):
        captured: dict = {}

        async def inner(scope, receive, send):
            captured["during"] = current_principal.get()

        async def _recv():
            return {"type": "http.request"}

        async def _send(message):
            pass

        mw = JWTAuthMiddleware(inner, settings=get_settings())
        token = make_jwt(user_name="carol")
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }

        async def driver():
            await mw(scope, _recv, _send)
            captured["after"] = current_principal.get()

        anyio.run(driver)

        assert captured["during"] is not None
        assert captured["during"].claims["user_name"] == "carol"
        # Reset in the middleware's finally must clear it — no leak to the next request.
        assert captured["after"] is None
