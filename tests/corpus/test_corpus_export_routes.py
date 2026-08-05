"""Tests for GET /blueprints/export and /knowledge/export on the MCP HTTP app.

Mirrors tests/test_catalog_export.py: the agent reaches this service via its
``mcp_url`` — the MCP HTTP app (``app/mcp_server.py``), NOT the REST app — so these
endpoints are registered as ``@mcp.custom_route`` handlers and are driven here
through the SAME ``JWTAuthMiddleware``-wrapped streamable-HTTP app the runtime hits.

Coverage:
  1. 200 + payload shape for both endpoints via the MCP app; source=mcp +
     verified=true stamped on every exported entry.
  2. SECURITY — scope-independence: two DIFFERENTLY column-scoped JWTs get
     byte-for-byte identical bodies (the route must ignore current_scope).
  3. The agent's real header pair (Authorization + X-Session-Id) is accepted.
  4. Auth required (default-deny): 401 without / with a malformed / bad-signature token.
  5. The routes are NOT model-facing @mcp.tool entries.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
from app.corpus import loader
from app.mcp_server import JWTAuthMiddleware, mcp
from tests.jwt_helpers import WRONG_PRIVATE_PEM, make_jwt


@pytest.fixture(autouse=True)
def _reset_corpus_cache():
    loader.invalidate_corpus_cache()
    yield
    loader.invalidate_corpus_cache()


def _authed_app():
    """The real MCP streamable-HTTP app behind JWTAuthMiddleware (agent-facing path)."""
    return JWTAuthMiddleware(mcp.streamable_http_app(), settings=get_settings())


def _client() -> TestClient:
    # Custom routes are served without entering the MCP lifespan (same as the
    # catalog-export / scratch-route tests) — no `with` block needed.
    return TestClient(_authed_app(), raise_server_exceptions=False)


AUTH = {"Authorization": f"Bearer {make_jwt()}"}


# ===========================================================================
# 1. Payload shape — served by the MCP HTTP app
# ===========================================================================

class TestBlueprintsExportPayload:
    def test_serves_200(self):
        resp = _client().get("/blueprints/export", headers=AUTH)
        assert resp.status_code == 200, resp.text

    def test_top_level_shape(self):
        body = _client().get("/blueprints/export", headers=AUTH).json()
        assert set(body.keys()) == {"blueprints_sha", "blueprints"}
        assert isinstance(body["blueprints_sha"], str) and body["blueprints_sha"]
        assert isinstance(body["blueprints"], dict)

    def test_all_blueprints_present(self):
        blueprints = _client().get("/blueprints/export", headers=AUTH).json()["blueprints"]
        assert set(blueprints.keys()) == set(loader.get_blueprints().keys())
        assert "bp-overtime-by-department" in blueprints

    def test_sha_matches_loader_sidecar(self):
        body = _client().get("/blueprints/export", headers=AUTH).json()
        assert body["blueprints_sha"] == loader.get_blueprints_sha()

    def test_source_and_verified_on_every_entry(self):
        blueprints = _client().get("/blueprints/export", headers=AUTH).json()["blueprints"]
        assert blueprints
        for entry in blueprints.values():
            assert entry["source"] == "mcp"
            assert entry["verified"] is True


class TestKnowledgeExportPayload:
    def test_serves_200(self):
        resp = _client().get("/knowledge/export", headers=AUTH)
        assert resp.status_code == 200, resp.text

    def test_top_level_shape(self):
        body = _client().get("/knowledge/export", headers=AUTH).json()
        assert set(body.keys()) == {"knowledge_sha", "knowledge"}
        assert isinstance(body["knowledge_sha"], str) and body["knowledge_sha"]
        assert isinstance(body["knowledge"], dict)

    def test_all_knowledge_present(self):
        knowledge = _client().get("/knowledge/export", headers=AUTH).json()["knowledge"]
        assert set(knowledge.keys()) == set(loader.get_knowledge().keys())
        assert "kn-overtime-multiplier" in knowledge

    def test_sha_matches_loader_sidecar(self):
        body = _client().get("/knowledge/export", headers=AUTH).json()
        assert body["knowledge_sha"] == loader.get_knowledge_sha()

    def test_source_and_verified_on_every_entry(self):
        knowledge = _client().get("/knowledge/export", headers=AUTH).json()["knowledge"]
        assert knowledge
        for entry in knowledge.values():
            assert entry["source"] == "mcp"
            assert entry["verified"] is True


# ===========================================================================
# 2. SECURITY — scope-independence (the key property, mirrored from catalog)
# ===========================================================================

class TestScopeIndependence:
    """Two principals with DIFFERENT column scopes get byte-identical bodies.

    JWTAuthMiddleware binds current_scope from each JWT, so this proves the corpus
    routes ignore that bound scope entirely — they are trusted-runtime bootstrap
    data, never the model-facing, scope-filtered path.
    """

    @pytest.mark.parametrize("path", ["/blueprints/export", "/knowledge/export"])
    def test_identical_body_across_different_column_scopes(self, path):
        narrow = make_jwt(column_scope=["dbpcm_warehouse.employee.employee_code"])
        other = make_jwt(
            column_scope=[
                "dbpcm_warehouse.payroll.gross_pay",
                "dbpcm_warehouse.candidate_education.institute_name",
            ]
        )
        resp_narrow = _client().get(path, headers={"Authorization": f"Bearer {narrow}"})
        resp_other = _client().get(path, headers={"Authorization": f"Bearer {other}"})
        assert resp_narrow.status_code == 200
        assert resp_other.status_code == 200
        # Byte-for-byte identical — no per-caller scoping whatsoever.
        assert resp_narrow.content == resp_other.content

    @pytest.mark.parametrize("path", ["/blueprints/export", "/knowledge/export"])
    def test_empty_scope_and_scoped_caller_agree(self, path):
        unrestricted = make_jwt()  # empty column_scope = allow-all
        restricted = make_jwt(column_scope=["dbpcm_warehouse.employee.employee_code"])
        a = _client().get(path, headers={"Authorization": f"Bearer {unrestricted}"})
        b = _client().get(path, headers={"Authorization": f"Bearer {restricted}"})
        assert a.content == b.content


# ===========================================================================
# 3. The agent's real request headers are accepted
# ===========================================================================

class TestAgentHeadersAccepted:
    """The agent sends Bearer JWT + X-Session-Id; the routes must accept both.

    The corpus routes are session-independent, but the middleware still runs the
    X-Session-Id <-> sid_hash binding when a session id is present, so we bind the
    token to the same session the header carries.
    """

    @pytest.mark.parametrize(
        "path,keys",
        [
            ("/blueprints/export", {"blueprints_sha", "blueprints"}),
            ("/knowledge/export", {"knowledge_sha", "knowledge"}),
        ],
    )
    def test_authorization_plus_x_session_id_accepted(self, path, keys):
        session_id = "sess-agent-1"
        token = make_jwt(session_id=session_id)
        resp = _client().get(
            path,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Session-Id": session_id,
            },
        )
        assert resp.status_code == 200
        assert set(resp.json().keys()) == keys


# ===========================================================================
# 4. Auth required (default-deny on the MCP app)
# ===========================================================================

class TestCorpusExportAuth:
    @pytest.mark.parametrize("path", ["/blueprints/export", "/knowledge/export"])
    def test_missing_token_returns_401(self, path):
        resp = _client().get(path)
        assert resp.status_code == 401
        assert resp.json()["code"] == "MISSING_AUTH"

    @pytest.mark.parametrize("path", ["/blueprints/export", "/knowledge/export"])
    def test_malformed_token_returns_401(self, path):
        resp = _client().get(path, headers={"Authorization": "Bearer not-a-jwt"})
        assert resp.status_code == 401

    @pytest.mark.parametrize("path", ["/blueprints/export", "/knowledge/export"])
    def test_bad_signature_returns_401(self, path):
        token = make_jwt(signing_key=WRONG_PRIVATE_PEM)
        resp = _client().get(path, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401


# ===========================================================================
# 5. Guard — corpus exports must NOT be model-facing MCP tools
# ===========================================================================

class TestNotAnMcpTool:
    def test_corpus_routes_absent_from_mcp_tool_catalogue(self):
        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        assert "blueprints_export_route" not in tool_names
        assert "knowledge_export_route" not in tool_names
        assert "exportBlueprints" not in tool_names
        assert "exportKnowledge" not in tool_names
        # Sanity: the legitimate model-facing tools ARE present.
        assert "getTableSchema" in tool_names
