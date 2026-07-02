"""QA adversarial suite for the X-Session-Id ↔ sid_hash binding (auth-hardening
Slice 1) — the MCP JWTAuthMiddleware enforcement point.

This file ADDS to (never modifies) the developer/reviewer coverage in
``test_mcp.py::TestSessionBinding`` and ``test_session_binding.py``.  It exists
to prove the *negative* space QA owns:

  * the hijack matrix, exhaustively (every header × token-binding combination);
  * encoding / forgery edge cases (whitespace, case, url-encoding, padded vs
    unpadded claim, empty / whitespace-only / literal "None" headers);
  * the constant-time compare is structurally the compare path (not ``==``);
  * rejection ORDERING — a mismatched binding is rejected 403 BEFORE any tool
    runs and BEFORE the D64 scratch extractor touches scratch;
  * the ``require_sid_binding`` flag semantics + its fail-closed default (True);
  * one genuine BYPASS QA found (omit the header) captured as a strict xfail
    regression guard (see ``TestOmitHeaderBypass``).

Harness: reuses ``tests.jwt_helpers.make_jwt`` (mints tokens with a ``sid_hash``
claim via ``session_id=...``) and drives the real ``JWTAuthMiddleware`` with a
Starlette TestClient.  The autouse ``_patch_jwks`` conftest fixture makes
``validate_token`` run offline.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json as _json

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from unittest.mock import patch

from app.config import Settings, get_settings
from app.mcp_server import JWTAuthMiddleware
from tests.jwt_helpers import make_jwt

# ---------------------------------------------------------------------------
# Harness — a middleware wrapper around a tiny inner app that records whether
# the downstream app (i.e. a "tool") was ever reached and what session id it saw.
# ---------------------------------------------------------------------------

_VALID_UUID_A = "11111111-1111-4111-8111-111111111111"
_VALID_UUID_B = "22222222-2222-4222-8222-222222222222"


def _capturing_app(captured: dict):
    async def endpoint(request):
        from app.principal import get_current_session_id

        captured["reached"] = True
        captured["session_id"] = get_current_session_id()
        return PlainTextResponse("inner-ran")

    return Starlette(routes=[Route("/mcp", endpoint)])


def _wrap(captured: dict, *, settings=None):
    return JWTAuthMiddleware(
        _capturing_app(captured), settings=settings or get_settings()
    )


def _client(captured: dict, *, settings=None) -> TestClient:
    return TestClient(_wrap(captured, settings=settings), raise_server_exceptions=False)


def _get(client: TestClient, token: str, session_header):
    headers = {"Authorization": f"Bearer {token}"}
    if session_header is not None:
        headers["X-Session-Id"] = session_header
    return client.get("/mcp", headers=headers)


# ===========================================================================
# 1. The hijack matrix, exhaustively
# ===========================================================================


class TestHijackMatrix:
    """Every (token binding) × (X-Session-Id header) combination, pinned."""

    def test_bound_A_header_A_ok(self):
        cap: dict = {}
        resp = _get(_client(cap), make_jwt(session_id=_VALID_UUID_A), _VALID_UUID_A)
        assert resp.status_code == 200
        assert cap["reached"] is True
        assert cap["session_id"] == _VALID_UUID_A

    def test_bound_A_header_B_rejected_403(self):
        """The classic hijack: token bound to A, caller presents a valid uuid B."""
        cap: dict = {}
        resp = _get(_client(cap), make_jwt(session_id=_VALID_UUID_A), _VALID_UUID_B)
        assert resp.status_code == 403
        assert resp.json()["code"] == "SESSION_BINDING_MISMATCH"
        # The tool must never have run — rejection precedes context binding.
        assert cap.get("reached") is not True

    def test_bound_A_no_header_allowed_but_session_context_is_none(self):
        """A bound token with NO header: binding is not enforced (header absent),
        the request proceeds, and the session context is NEVER established
        (current_session_id is None — the request cannot claim to *be* session A).

        This pins the design's stance that binding fires only when the header is
        present.  The downstream consequence (a None session context reaching the
        D64 scratch gate) is exercised as a bypass in TestOmitHeaderBypass."""
        cap: dict = {}
        resp = _get(_client(cap), make_jwt(session_id=_VALID_UUID_A), None)
        assert resp.status_code == 200
        assert cap["reached"] is True
        assert cap["session_id"] is None

    def test_sessionless_token_header_present_rejected_403(self):
        """A token with NO sid_hash claim + any X-Session-Id → 403 (fail-closed)."""
        cap: dict = {}
        resp = _get(_client(cap), make_jwt(), _VALID_UUID_A)
        assert resp.status_code == 403
        assert resp.json()["code"] == "SESSION_BINDING_MISMATCH"
        assert cap.get("reached") is not True

    def test_sessionless_token_no_header_ok(self):
        """A session-less token + no header → allowed (backward-compat REST/stdio)."""
        cap: dict = {}
        resp = _get(_client(cap), make_jwt(), None)
        assert resp.status_code == 200
        assert cap["reached"] is True
        assert cap["session_id"] is None


# ===========================================================================
# 2. Encoding / forgery edge cases
# ===========================================================================


class TestEncodingEdgeCases:
    """Pin the EXACT wire encoding — no accidental normalization opens a
    near-match, and the padded/unpadded distinction is honored."""

    def test_different_case_header_rejected(self):
        """sha256 is case-sensitive: 'SESS-A' != 'sess-a' → 403."""
        cap: dict = {}
        resp = _get(_client(cap), make_jwt(session_id="sess-a"), "SESS-A")
        assert resp.status_code == 403
        assert resp.json()["code"] == "SESSION_BINDING_MISMATCH"

    def test_url_encoded_header_not_decoded_rejected(self):
        """A percent-encoded header is compared literally (no URL-decoding), so
        '%73ess-A' hashes differently from 'sess-A' → 403 (no smuggling via
        transport-layer encoding)."""
        cap: dict = {}
        resp = _get(_client(cap), make_jwt(session_id="sess-A"), "%73ess-A")
        assert resp.status_code == 403
        assert resp.json()["code"] == "SESSION_BINDING_MISMATCH"

    def test_padded_claim_does_not_match_unpadded_compute(self):
        """The contract is UNPADDED base64url.  A token whose sid_hash claim was
        (wrongly) padded with '=' must NOT match the middleware's unpadded
        recompute — the exact encoding is pinned, a differently-encoded hash of
        the *same* session id is rejected."""
        session_id = "sess-P"
        padded = base64.urlsafe_b64encode(
            hashlib.sha256(session_id.encode()).digest()
        ).decode("ascii")
        assert padded.endswith("=")  # sanity: this encoding really is padded
        token = make_jwt(extra_claims={"sid_hash": padded})
        cap: dict = {}
        resp = _get(_client(cap), token, session_id)
        assert resp.status_code == 403
        assert resp.json()["code"] == "SESSION_BINDING_MISMATCH"

    def test_literal_none_string_header_is_enforced(self):
        """A header carrying the literal string 'None' is an ordinary value —
        it is NOT treated as absent and is enforced against the claim → 403
        (unless the token were bound to the literal 'None')."""
        cap: dict = {}
        resp = _get(_client(cap), make_jwt(session_id=_VALID_UUID_A), "None")
        assert resp.status_code == 403
        assert resp.json()["code"] == "SESSION_BINDING_MISMATCH"

    def test_literal_null_string_header_is_enforced(self):
        cap: dict = {}
        resp = _get(_client(cap), make_jwt(session_id=_VALID_UUID_A), "null")
        assert resp.status_code == 403
        assert resp.json()["code"] == "SESSION_BINDING_MISMATCH"

    # --- Normalization edge cases: the middleware .strip()s the header value ---
    # These pin the ACTUAL behaviour.  The strip is applied to the SAME value that
    # is later bound into current_session_id, so it can never turn session B into
    # session A (a mismatched id still mismatches after stripping) — it only
    # canonicalizes surrounding whitespace on the caller's OWN id.  A uuid4
    # session id never contains whitespace, so the practical attack surface is
    # nil; documented here so the behaviour is a locked decision, not an accident.

    def test_trailing_whitespace_is_stripped_and_matches(self):
        cap: dict = {}
        resp = _get(_client(cap), make_jwt(session_id=_VALID_UUID_A), _VALID_UUID_A + " ")
        assert resp.status_code == 200  # stripped → matches
        assert cap["session_id"] == _VALID_UUID_A  # bound value is also stripped

    def test_leading_whitespace_is_stripped_and_matches(self):
        cap: dict = {}
        resp = _get(_client(cap), make_jwt(session_id=_VALID_UUID_A), " " + _VALID_UUID_A)
        assert resp.status_code == 200
        assert cap["session_id"] == _VALID_UUID_A

    def test_whitespace_does_not_bridge_two_distinct_sessions(self):
        """The strip must not let a padded B masquerade as A.  Token bound to A,
        header ' <B> ' → still B after stripping → 403 (the strip is benign)."""
        cap: dict = {}
        resp = _get(_client(cap), make_jwt(session_id=_VALID_UUID_A), " " + _VALID_UUID_B + " ")
        assert resp.status_code == 403
        assert resp.json()["code"] == "SESSION_BINDING_MISMATCH"

    def test_empty_header_treated_as_absent(self):
        """An empty X-Session-Id ('') is coerced to None (treated as absent), so
        the binding is NOT enforced and the session context is None.  Pinned: an
        empty header cannot *establish* a session, so it cannot impersonate one."""
        cap: dict = {}
        resp = _get(_client(cap), make_jwt(session_id=_VALID_UUID_A), "")
        assert resp.status_code == 200
        assert cap["session_id"] is None

    def test_whitespace_only_header_treated_as_absent(self):
        """'   ' strips to '' → coerced to None → treated as absent (as above)."""
        cap: dict = {}
        resp = _get(_client(cap), make_jwt(session_id=_VALID_UUID_A), "   ")
        assert resp.status_code == 200
        assert cap["session_id"] is None


# ===========================================================================
# 3. Constant-time compare — structural + behavioural
# ===========================================================================


class TestConstantTimeCompare:

    def test_compare_uses_hmac_compare_digest_not_equality(self):
        """The verifier MUST use hmac.compare_digest (timing-safe), not '=='."""
        from app import session_binding

        src = inspect.getsource(session_binding.sid_hash_matches)
        assert "compare_digest" in src, "sid_hash_matches must use hmac.compare_digest"
        # No naive equality on the secret hash in the compare function.
        assert "==" not in src, "sid_hash_matches must not fall back to a '==' compare"

    def test_middleware_routes_through_sid_hash_matches(self):
        """The middleware must delegate to the constant-time helper, not inline a
        raw '==' comparison of the hash."""
        from app import mcp_server

        src = inspect.getsource(mcp_server.JWTAuthMiddleware.__call__)
        assert "sid_hash_matches" in src

    def test_one_char_off_hash_is_rejected(self):
        """A claim that differs from the true hash by a single character → 403
        (behavioural companion to the structural check above)."""
        from app.session_binding import sid_hash

        true_hash = sid_hash(_VALID_UUID_A)
        # Flip the last character to something guaranteed different.
        flipped = true_hash[:-1] + ("A" if true_hash[-1] != "A" else "B")
        assert flipped != true_hash
        token = make_jwt(extra_claims={"sid_hash": flipped})
        cap: dict = {}
        resp = _get(_client(cap), token, _VALID_UUID_A)
        assert resp.status_code == 403
        assert resp.json()["code"] == "SESSION_BINDING_MISMATCH"


# ===========================================================================
# 4. require_sid_binding flag semantics + fail-closed default
# ===========================================================================


class TestFlagSemantics:

    def test_default_is_true_fail_closed(self):
        """The config default MUST be True (fail-closed deploy posture)."""
        assert Settings().require_sid_binding is True
        assert get_settings().require_sid_binding is True

    def test_flag_off_allows_mismatched_header(self):
        """With the escape hatch off, a mismatched header is allowed (transition
        mode) — the flag's semantics are locked so it cannot silently harden."""
        cap: dict = {}
        legacy = Settings(require_sid_binding=False)
        resp = _get(
            _client(cap, settings=legacy), make_jwt(session_id=_VALID_UUID_A), _VALID_UUID_B
        )
        assert resp.status_code == 200
        assert cap["reached"] is True
        # The (attacker-controlled) header value still flows to the context —
        # which is exactly why this flag is a *transition* hatch, not a keep.
        assert cap["session_id"] == _VALID_UUID_B

    def test_flag_off_allows_present_header_missing_claim(self):
        cap: dict = {}
        legacy = Settings(require_sid_binding=False)
        resp = _get(_client(cap, settings=legacy), make_jwt(), _VALID_UUID_A)
        assert resp.status_code == 200


# ===========================================================================
# 5. Rejection ordering — 403 BEFORE any tool + BEFORE the D64 scratch extractor
# ===========================================================================


def _full_mcp_app(settings=None):
    """Wrap the REAL MCP streamable-HTTP app so tools actually dispatch."""
    from app.mcp_server import mcp

    return JWTAuthMiddleware(mcp.streamable_http_app(), settings=settings or get_settings())


def _sse_result(resp) -> dict:
    data_line = next(
        line[len("data:"):].strip()
        for line in resp.text.splitlines()
        if line.startswith("data:")
    )
    return _json.loads(data_line)["result"]


class TestRejectionOrdering:
    """A mismatched binding is rejected in the middleware — before the tool
    executes and before the D64 scratch extractor touches scratch."""

    def test_mismatch_rejected_before_tool_and_before_scratch_extractor(self):
        catalog = {"analytics.employees": {"employee_id": "UInt64"}}
        # Token bound to A; caller presents B and asks for a runQuery that WOULD
        # otherwise reach the scratch extractor (references scratch.*).
        token = make_jwt(
            column_scope=["analytics.employees.employee_id"], session_id=_VALID_UUID_A
        )
        sql = "SELECT x FROM scratch.s_" + _VALID_UUID_B + "_secret"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "runQuery", "arguments": {"sql": sql}},
        }
        with patch("app.service.execute_query") as mock_execute:
            with patch("app.service.get_catalog_schema", return_value=catalog):
                with TestClient(_full_mcp_app(), raise_server_exceptions=False) as client:
                    resp = client.post(
                        "/mcp",
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/json, text/event-stream",
                            "X-Session-Id": _VALID_UUID_B,
                        },
                    )
        # The binding rejection is an HTTP 403 at the middleware — NOT a tool
        # error inside a 200 SSE envelope (which is what a D64 rejection would be).
        assert resp.status_code == 403
        assert resp.json()["code"] == "SESSION_BINDING_MISMATCH"
        # No tool ran → no scratch access → execute_query never called.
        mock_execute.assert_not_called()


# ===========================================================================
# 6. BYPASS (QA finding) — omitting X-Session-Id defeats scratch protection
# ===========================================================================
#
# SEVERITY: HIGH.  The sid_hash binding fires ONLY when X-Session-Id is present
# (`if session_id_value is not None`).  When the header is omitted (or empty),
# current_session_id becomes None, and the D64 scratch owner-check
# (_validate_scratch_name) SKIPS validation for session_id=None — so a caller
# holding ANY valid token can read ANOTHER session's scratch table by simply
# NOT sending the header.  No forgery required; omission suffices.
#
# The interaction defeats Slice 1's stated goal (§6.1: "userA cannot ... read
# userB's scratch") even though the binding's literal when-present contract is
# not violated.  Note also that mcp_server.py's SESSION_ID_HEADER docstring
# claims a None session fails closed at "the catalogue-lookup step" — but scratch
# tables are intentionally NOT catalogued and are NOT catalogue-checked, so that
# claim does not hold for scratch references.
#
# CLOSED (D92, fix option b): `_validate_scratch_name(session_id=None)` now
# fail-closes (raises SCRATCH_SESSION_VIOLATION) on any `scratch.*` reference,
# in every SQL position (find_all(exp.Table)), instead of skipping the owner
# check — so a bound token cannot read another session's scratch by omitting
# the header. Option (a) was rejected: it would forbid the legitimate no-header
# no-session mode (a bound token running a non-scratch query stays 200).
# These tests assert the SECURE expectation and now PASS (un-xfailed) — they are
# the standing regression guard.
# ===========================================================================


class TestOmitHeaderBypass:

    def _attack(self, session_header):
        catalog = {"analytics.employees": {"employee_id": "UInt64"}}
        # Attacker holds a VALID token bound to their OWN session A.
        token = make_jwt(
            column_scope=["analytics.employees.employee_id"], session_id=_VALID_UUID_A
        )
        # ...and targets the VICTIM's (session B) scratch table.
        victim_scratch = "scratch.s_" + _VALID_UUID_B + "_payroll"
        sql = f"SELECT secret_col FROM {victim_scratch}"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "runQuery", "arguments": {"sql": sql}},
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        if session_header is not None:
            headers["X-Session-Id"] = session_header
        with patch("app.service.execute_query") as mock_execute:
            mock_execute.return_value = (["secret_col"], [["VICTIM-PII"]])
            with patch("app.service.get_catalog_schema", return_value=catalog):
                with TestClient(_full_mcp_app(), raise_server_exceptions=False) as client:
                    resp = client.post("/mcp", json=payload, headers=headers)
        return resp, mock_execute

    def test_no_header_must_not_read_cross_session_scratch(self):
        resp, mock_execute = self._attack(session_header=None)
        # SECURE expectation: the victim's scratch must NOT be read.
        mock_execute.assert_not_called()

    def test_empty_header_must_not_read_cross_session_scratch(self):
        resp, mock_execute = self._attack(session_header="")
        mock_execute.assert_not_called()
