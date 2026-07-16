"""Unit tests for app/session_binding.py — the X-Session-Id ↔ sid_hash primitive.

Locks in the exact wire encoding (base64url(sha256(session_id)), unpadded) that
both the token service (minter) and the MCP middleware (verifier) rely on, and
that the constant-time compare accepts only the exact hash.
"""

from __future__ import annotations

import base64
import hashlib

from app.session_binding import sid_hash, sid_hash_matches


def test_sid_hash_is_unpadded_base64url_sha256():
    session_id = "sess-abc-123"
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(session_id.encode("utf-8")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert sid_hash(session_id) == expected
    # Unpadded: no '=' padding characters.
    assert "=" not in sid_hash(session_id)


def test_sid_hash_is_deterministic():
    assert sid_hash("s1") == sid_hash("s1")


def test_sid_hash_differs_per_session():
    assert sid_hash("session-A") != sid_hash("session-B")


def test_matches_accepts_correct_hash():
    session_id = "some-uuid4-value"
    assert sid_hash_matches(session_id, sid_hash(session_id)) is True


def test_matches_rejects_wrong_hash():
    assert sid_hash_matches("session-A", sid_hash("session-B")) is False


def test_matches_rejects_empty_and_garbage():
    assert sid_hash_matches("session-A", "") is False
    assert sid_hash_matches("session-A", "not-a-real-hash") is False
