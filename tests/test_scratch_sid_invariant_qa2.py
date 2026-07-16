"""QA2 — the underscore-free-sid invariant at the SCRATCH WRITE boundary.

The D64 read gate (``app/sqlparse/provenance._validate_scratch_name``) extracts a
scratch table's owning session by ``partition('_')`` — i.e. it TRUSTS that a session
id contains no ``_``. The companion read-gate suite
(``tests/sqlparse/test_provenance_d64_extraction_qa2.py``) proves that trust is
load-bearing: an underscore-containing owner sid causes an ownership INVERSION
(a different session reads the victim's table).

This file interrogates whether that invariant is ENFORCED at the write boundary —
``app/scratch_ingest.scratch_table_name`` / ``materialize`` — which is the point
that actually mints the ``s_<sid>_bp_<hex>`` name. Finding: it is NOT. The name is
run through ``validate_identifier`` (``^[A-Za-z_][A-Za-z0-9_]*$``) which PERMITS
``_``, so an underscore-containing session id is accepted and produces a table the
read gate mis-attributes. The invariant is enforced ONLY as a discipline at the UI
BFF (``ui/server.py`` mints ``s`` + ``uuid4().hex``), not structurally at either
security boundary.

The passing tests here document the current (unsafe) acceptance; the strict xfail
pins the DESIRED fix (reject an underscore-containing sid at materialize, so the
write side enforces the invariant the read gate depends on). No live ClickHouse:
``scratch_table_name`` is a pure function.
"""

from __future__ import annotations

import os

import pytest

_BASE_ENV = {
    "CLICKHOUSE_HOST": "localhost",
    "CLICKHOUSE_PORT": "9000",
    "CLICKHOUSE_USER": "default",
    "CLICKHOUSE_PASSWORD": "test-password",
    "CLICKHOUSE_DATABASE": "default",
    "CLICKHOUSE_SECURE": "false",
    "OIDC_JWKS_URL": "https://idp.test/.well-known/jwks.json",
    "OIDC_ISSUER": "https://idp.test/",
    "OIDC_AUDIENCE": "clickhouse-api",
    "JWT_ALGORITHMS": "RS256",
    "ALLOWED_DATABASES": "*",
    "APP_PORT": "8000",
    "LOG_LEVEL": "INFO",
    "PUBLIC_BASE_URL": "https://test.example.com",
}
for _k, _v in _BASE_ENV.items():
    os.environ.setdefault(_k, _v)

from app.scratch_ingest import ScratchWriteError, scratch_table_name  # noqa: E402
from app.sqlparse import ScratchSessionError, extract_column_provenance  # noqa: E402

_CATALOG: dict[str, dict[str, str]] = {
    "dbpcm_warehouse.employee": {"EmployeeCode": "String", "Department": "Nullable(String)"}
}


def _read_allowed(full_or_bare_table: str, session_id: str) -> bool:
    """True iff the D64 read gate accepts *table* for *session_id*."""
    bare = full_or_bare_table.split(".", 1)[1] if "." in full_or_bare_table else full_or_bare_table
    sql = (
        f"SELECT s.EmployeeCode, e.Department "
        f"FROM scratch.{bare} AS s "
        f"JOIN dbpcm_warehouse.employee AS e ON s.EmployeeCode = e.EmployeeCode"
    )
    try:
        extract_column_provenance(sql, _CATALOG, session_id=session_id)
        return True
    except ScratchSessionError:
        return False


# ---------------------------------------------------------------------------
# Baseline: safe sids behave correctly at the write boundary.
# ---------------------------------------------------------------------------


def test_underscore_free_hex_sid_accepted_and_read_gate_agrees() -> None:
    """A demo-format underscore-free sid mints a name only its OWN session reads."""
    sid = "s" + "a" * 32
    name = scratch_table_name(sid)
    assert name.startswith(f"s_{sid}_bp_")
    assert _read_allowed(name, sid) is True
    # A different underscore-free sid cannot read it.
    assert _read_allowed(name, "s" + "b" * 32) is False


def test_hyphenated_uuid_sid_rejected_at_write() -> None:
    """A raw uuid4 (hyphens) is not a safe SQL identifier → materialize rejects it
    (the runtime must strip hyphens to hex, not pass them through)."""
    import uuid

    with pytest.raises(ScratchWriteError):
        scratch_table_name(str(uuid.uuid4()))


def test_empty_sid_rejected_at_write() -> None:
    with pytest.raises(ScratchWriteError):
        scratch_table_name("")


# ---------------------------------------------------------------------------
# The fix (HIGH): the write boundary now REJECTS an underscore-containing sid, so
# the invariant the read gate depends on holds STRUCTURALLY (a scratch table can
# only exist for an underscore-free sid). These tests document the closed gap.
# ---------------------------------------------------------------------------


def test_underscore_sid_is_now_rejected_at_write_boundary() -> None:
    """FIXED: ``scratch_table_name('a_b')`` now raises ``ScratchWriteError`` —
    ``validate_identifier`` permits ``_`` but ``scratch_table_name`` additionally
    rejects any ``_``-containing sid, so the read gate never faces the ambiguous
    ``s_a_b_bp_<hex>`` name it would mis-attribute to session ``a``."""
    with pytest.raises(ScratchWriteError) as exc:
        scratch_table_name("a_b")
    assert exc.value.code == "SCRATCH_MATERIALIZE_REJECTED"


def test_underscore_sid_bypass_is_closed_at_the_write_boundary() -> None:
    """END-TO-END PIN (HIGH, now CLOSED): the cross-session bypass is prevented at
    its SOURCE — the underscore-owner table can never be minted, so the read gate
    is never handed an ambiguous name. There is no ``s_a_b_bp_<hex>`` for the
    unrelated session ``a`` to read, because ``scratch_table_name('a_b')`` refuses
    to create it."""
    with pytest.raises(ScratchWriteError):
        scratch_table_name("a_b")


# ---------------------------------------------------------------------------
# The fix, pinned STRICT (HIGH, un-xfailed): the write boundary rejects an
# underscore-containing session id, so the read gate's exact-extraction never
# silently depends on a discipline only the UI enforces.
# ---------------------------------------------------------------------------


def test_underscore_sid_SHOULD_be_rejected_at_write_boundary() -> None:
    with pytest.raises(ScratchWriteError):
        scratch_table_name("a_b")
