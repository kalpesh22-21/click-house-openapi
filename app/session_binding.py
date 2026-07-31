"""X-Session-Id ↔ JWT binding primitive (auth-hardening Slice 1).

The ``X-Session-Id`` header travels unsigned alongside the JWT.  Because the
scratch-table session isolation gate (D64) trusts that header's value, an
attacker holding one valid JWT could set ``X-Session-Id`` to *another* user's
session id and read their scratch/PII.  To close that gap the external token
issuer stamps a ``sid_hash`` claim inside the (already-signed) JWT at mint time,
and the MCP middleware recomputes the hash of the presented header and compares
it against the claim.

This module is the single source of truth for the wire encoding so the external
minter and the verifier (``app.mcp_server``) can never drift:

    sid_hash = base64url(sha256(session_id.utf8))  # unpadded

No keying is needed: ``session_id`` is a uuid4 (~122 bits), so a plain hash is
not brute-forceable, and the JWT signature already binds the claim to ``sub``
(an attacker cannot move the claim onto another token without the private key).
This module has no config dependency so any caller can import it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac


def sid_hash(session_id: str) -> str:
    """Return ``base64url(sha256(session_id))`` (unpadded) — the ``sid_hash`` claim."""
    digest = hashlib.sha256(session_id.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def sid_hash_matches(session_id: str, claim: str) -> bool:
    """Constant-time compare ``sid_hash(session_id)`` against the token's claim.

    ``hmac.compare_digest`` avoids leaking, via timing, how many leading
    characters of the expected hash an attacker has guessed.
    """
    return hmac.compare_digest(sid_hash(session_id), claim)
