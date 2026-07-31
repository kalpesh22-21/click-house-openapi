"""Tests for GET /catalog/export on the MCP HTTP app (Wave 1a bootstrap).

CRITICAL topology note: the agent reaches this service via its ``mcp_url`` — the
MCP HTTP app (``app/mcp_server.py``), NOT the REST app (``app/main.py``). In every
real split deployment those are separate processes/containers, so this endpoint
is registered as an ``@mcp.custom_route`` on the MCP app and these tests drive it
through the SAME ``JWTAuthMiddleware``-wrapped streamable-HTTP app the runtime
hits — mirroring how the scratch side-channel routes are tested
(tests/test_scratch_write_qa.py).

Coverage (security assertions preserved from the original REST test):
  1. 200 + full payload shape via the MCP app: catalog_sha + all 9 tables +
     columns carry YAML-declared types.
  2. SECURITY — scope-independence: two DIFFERENTLY column-scoped JWTs get
     byte-for-byte identical bodies. This is stronger on the MCP app than on REST
     because JWTAuthMiddleware actually binds current_scope from the JWT
     (mcp_server.py L567) — the route must still ignore it.
  3. Auth required: 401 without a token / with a malformed token (default-deny).
  4. The agent's real headers (Authorization + X-Session-Id) are accepted.
  5. Guard: exportCatalog / catalog_export_route is NOT in the model-facing MCP
     @mcp.tool catalogue.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from app.config import get_settings
from app.mcp_server import JWTAuthMiddleware, mcp
from tests.jwt_helpers import WRONG_PRIVATE_PEM, make_jwt


def _authed_app():
    """The real MCP streamable-HTTP app behind JWTAuthMiddleware (agent-facing path)."""
    return JWTAuthMiddleware(mcp.streamable_http_app(), settings=get_settings())


def _client() -> TestClient:
    # Custom routes are served without entering the MCP lifespan (same as the
    # scratch-route endpoint tests) — no `with` block needed.
    return TestClient(_authed_app(), raise_server_exceptions=False)


AUTH = {"Authorization": f"Bearer {make_jwt()}"}


# ===========================================================================
# 1. Payload shape — served by the MCP HTTP app
# ===========================================================================

class TestExportPayloadOverMcp:

    def test_mcp_app_serves_catalog_export_200(self):
        """Confirm the MCP HTTP app (the agent's mcp_url target) serves the route."""
        resp = _client().get("/catalog/export", headers=AUTH)
        assert resp.status_code == 200, resp.text

    def test_top_level_shape(self):
        body = _client().get("/catalog/export", headers=AUTH).json()
        assert set(body.keys()) == {"catalog_sha", "catalog"}
        assert isinstance(body["catalog_sha"], str) and body["catalog_sha"]
        assert isinstance(body["catalog"], dict)

    def test_all_nine_catalogued_tables_present(self):
        catalog = _client().get("/catalog/export", headers=AUTH).json()["catalog"]
        assert len(catalog) == 9
        assert "dbpcm_warehouse.employee" in catalog
        assert "dbpcm_warehouse.payroll" in catalog

    def test_columns_carry_declared_types(self):
        catalog = _client().get("/catalog/export", headers=AUTH).json()["catalog"]
        employee_cols = catalog["dbpcm_warehouse.employee"]["columns"]
        # YAML-declared catalog types, not live system.columns types.
        assert employee_cols["EmployeeCode"]["type"] == "String"
        assert employee_cols["ClientCode"]["type"] == "Nullable(String)"

    def test_sha_matches_loader_sidecar(self):
        from app.semantic_catalog.loader import get_catalog_sha

        body = _client().get("/catalog/export", headers=AUTH).json()
        assert body["catalog_sha"] == get_catalog_sha()


# ===========================================================================
# 2. SECURITY — scope-independence (the key property)
# ===========================================================================

class TestScopeIndependenceOverMcp:

    def test_identical_body_across_different_column_scopes(self):
        """Two principals with DIFFERENT column scopes get byte-identical bodies.

        JWTAuthMiddleware binds current_scope from each JWT (mcp_server.py L567),
        so this proves the route ignores that bound scope entirely.
        """
        narrow = make_jwt(column_scope=["dbpcm_warehouse.employee.EmployeeCode"])
        other = make_jwt(
            column_scope=[
                "dbpcm_warehouse.payroll.GrossPay",
                "dbpcm_warehouse.candidate_education.SchoolName",
            ]
        )

        resp_narrow = _client().get(
            "/catalog/export", headers={"Authorization": f"Bearer {narrow}"}
        )
        resp_other = _client().get(
            "/catalog/export", headers={"Authorization": f"Bearer {other}"}
        )

        assert resp_narrow.status_code == 200
        assert resp_other.status_code == 200
        # Byte-for-byte identical — no per-caller scoping whatsoever.
        assert resp_narrow.content == resp_other.content

    def test_empty_scope_and_scoped_caller_agree(self):
        """An unrestricted (empty-scope) token and a restricted token get the same body."""
        unrestricted = make_jwt()  # empty column_scope = allow-all
        restricted = make_jwt(column_scope=["dbpcm_warehouse.employee.EmployeeCode"])

        a = _client().get("/catalog/export", headers={"Authorization": f"Bearer {unrestricted}"})
        b = _client().get("/catalog/export", headers={"Authorization": f"Bearer {restricted}"})
        assert a.content == b.content

    def test_narrow_scope_still_gets_out_of_scope_columns(self):
        """A caller scoped to ONE column still receives columns it cannot query.

        The export is trusted-runtime bootstrap data, not the model-facing,
        scope-filtered getTableSchema overlay.
        """
        narrow = make_jwt(column_scope=["dbpcm_warehouse.employee.EmployeeCode"])
        catalog = _client().get(
            "/catalog/export", headers={"Authorization": f"Bearer {narrow}"}
        ).json()["catalog"]
        employee_cols = catalog["dbpcm_warehouse.employee"]["columns"]
        assert "ClientCode" in employee_cols
        assert len(employee_cols) > 1
        # A table the caller has no scope on at all is still fully present.
        assert "dbpcm_warehouse.payroll" in catalog


# ===========================================================================
# 3. Auth required (default-deny on the MCP app)
# ===========================================================================

class TestExportAuthOverMcp:

    def test_missing_token_returns_401(self):
        resp = _client().get("/catalog/export")
        assert resp.status_code == 401
        assert resp.json()["code"] == "MISSING_AUTH"

    def test_malformed_token_returns_401(self):
        resp = _client().get("/catalog/export", headers={"Authorization": "Bearer not-a-jwt"})
        assert resp.status_code == 401

    def test_bad_signature_returns_401(self):
        token = make_jwt(signing_key=WRONG_PRIVATE_PEM)
        resp = _client().get("/catalog/export", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401


# ===========================================================================
# 4. The agent's real request headers are accepted
# ===========================================================================

class TestAgentHeadersAccepted:

    def test_authorization_plus_x_session_id_accepted(self):
        """The agent sends Bearer JWT + X-Session-Id; the route must accept both.

        The catalog route is session-independent, but the middleware still runs
        the X-Session-Id ↔ sid_hash binding when a session id is present, so we
        bind the token to the same session the header carries.
        """
        session_id = "sess-agent-1"
        token = make_jwt(session_id=session_id)
        resp = _client().get(
            "/catalog/export",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Session-Id": session_id,
            },
        )
        assert resp.status_code == 200
        assert set(resp.json().keys()) == {"catalog_sha", "catalog"}


# ===========================================================================
# 5. Guard — exportCatalog must NOT be a model-facing MCP tool
# ===========================================================================

class TestNotAnMcpTool:

    def test_export_catalog_absent_from_mcp_tool_catalogue(self):
        """The full unscoped catalog must never be reachable by the agent LLM.

        Enumerate every registered @mcp.tool and assert the route's names do not
        appear. The route rides as a custom HTTP route, never as a tool.
        """
        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        assert "exportCatalog" not in tool_names
        assert "catalog_export_route" not in tool_names
        assert "export_catalog" not in tool_names
        # Sanity: the legitimate model-facing tools ARE present.
        assert "getTableSchema" in tool_names
