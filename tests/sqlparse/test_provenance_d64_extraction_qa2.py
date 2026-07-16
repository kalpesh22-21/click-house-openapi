"""QA2 — D64 read-gate EXACT-session-extraction matrix (table-intermediate Slice 2).

The tightening under test lives in ``app/sqlparse/provenance._validate_scratch_name``:
a scratch reference ``scratch.s_<sid>_<suffix>`` is accepted ONLY when the owning
session extracted as ``tbl[2:].partition('_')[0]`` equals the bound ``session_id``
EXACTLY (was: a loose ``startswith(f"s_{session_id}_")`` prefix).

This suite drives the security core of that gate + PINS the exact dependency the
whole isolation property now rests on: the exact-extraction is only sound because
session ids are minted UNDERSCORE-FREE. ``partition('_')`` splits at the FIRST
``_``, so for a table literally named ``s_a_b_...`` and a session bound to ``a``,
the extracted owner is ``a`` — a MATCH. The gate therefore does NOT, on its own,
stop session ``a`` from reading a table whose real owner is ``a_b``; it relies on
``a_b`` never being a mintable session id. See ``test_gate_RELIES_on_underscore
_free_sid_invariant`` (passing, documents the reliance) and the companion write-
side suite ``tests/test_scratch_sid_invariant_qa2.py`` (the HIGH xfail — the write
endpoint does NOT enforce underscore-free, so the invariant is unenforced where it
matters).

All tests are pure/sync — no ClickHouse, no infra.
"""

from __future__ import annotations

import pytest

from app.sqlparse import (
    ProvenanceExtractionError,
    ScratchSessionError,
    extract_column_provenance,
)

# Minimal warehouse catalog: the scratch table is JOINed to a catalogued table so
# the query is a realistic materialize-and-join consumer (Slice 2 shape).
_CATALOG: dict[str, dict[str, str]] = {
    "dbpcm_warehouse.employee": {
        "EmployeeCode": "String",
        "Department": "Nullable(String)",
    }
}

_HEX = "f" * 32  # a `bp_<32hex>` suffix stand-in; the gate does not inspect it


def _read(scratch_table: str, session_id: str | None) -> frozenset[tuple[str, str]]:
    """Extract provenance for a consumer that JOINs ``scratch.<scratch_table>``
    (aliased ``s``) to the warehouse ``employee`` under *session_id*."""
    sql = (
        f"SELECT s.EmployeeCode, e.Department "
        f"FROM scratch.{scratch_table} AS s "
        f"JOIN dbpcm_warehouse.employee AS e ON s.EmployeeCode = e.EmployeeCode"
    )
    return extract_column_provenance(sql, _CATALOG, session_id=session_id)


def _is_allowed(scratch_table: str, session_id: str | None) -> bool:
    try:
        _read(scratch_table, session_id)
        return True
    except ScratchSessionError:
        return False


# ---------------------------------------------------------------------------
# 1. The core matrix — own / different-session / malformed
# ---------------------------------------------------------------------------


def test_own_session_scratch_allowed() -> None:
    """Session ``a`` reading its OWN ``s_a_bp_<hex>`` → allowed (baseline)."""
    prov = _read(f"s_a_bp_{_HEX}", "a")
    # Scratch columns are accepted without catalog qualification (D69/OQ-4); the
    # warehouse column is still captured.
    assert ("dbpcm_warehouse.employee", "Department") in prov


def test_different_session_prefix_sibling_denied() -> None:
    """Session ``a`` reading ``s_ab_bp_<hex>`` (owner ``ab``) → DENIED.

    The former loose prefix ``startswith("s_a")`` would have been fooled by this
    (``s_ab...`` starts with ``s_a``); exact extraction yields owner ``ab`` != ``a``.
    """
    assert _is_allowed(f"s_ab_bp_{_HEX}", "a") is False


def test_longer_session_reading_shorter_owner_denied() -> None:
    """Session ``ab`` reading ``s_a_bp_<hex>`` (owner ``a``) → DENIED (symmetric)."""
    assert _is_allowed(f"s_a_bp_{_HEX}", "ab") is False


def test_realistic_hex_sid_cross_session_denied() -> None:
    """Two demo-format ``s<32hex>`` sids never read each other's scratch tables."""
    owner = "s" + "0" * 32
    attacker = "s" + "1" * 32
    assert _is_allowed(f"s_{owner}_bp_{_HEX}", owner) is True
    assert _is_allowed(f"s_{owner}_bp_{_HEX}", attacker) is False


# ---------------------------------------------------------------------------
# 2. THE reliance pin — the whole reason the write side must mint underscore-free
# ---------------------------------------------------------------------------


def test_gate_RELIES_on_underscore_free_sid_invariant() -> None:
    """PIN: session ``a`` reading a table literally named ``s_a_b_bp_<hex>`` is
    ALLOWED by the gate — because ``partition('_')`` extracts ``a`` from
    ``a_b_bp_<hex>`` and ``a == a``.

    This is the load-bearing observation for the whole isolation story: the
    exact-extraction gate does NOT close the ``_``-boundary case by itself. If a
    session whose id is ``a_b`` could ever materialize ``s_a_b_bp_<hex>``, session
    ``a`` would read it. The property that makes the gate sound is EXTERNAL: session
    ids are minted underscore-free (``s<32hex>``), so ``a_b`` is not a valid sid and
    the table cannot exist. This test locks in the dependency — if it ever starts
    DENYING (the gate was hardened to also reject an ambiguous ``_`` boundary),
    update the security note; if the write side stops enforcing underscore-free,
    this ALLOW becomes a live cross-session read (see the write-side xfail suite).
    """
    assert _is_allowed(f"s_a_b_bp_{_HEX}", "a") is True


def test_concrete_underscore_owner_ownership_inversion() -> None:
    """PIN (concrete, HIGH): with an underscore-containing owner sid ``sess_a``, the
    gate INVERTS ownership of ``s_sess_a_bp_<hex>``:

      - the TRUE owner ``sess_a`` is DENIED its OWN table (extraction yields ``sess``
        from ``sess_a_bp_<hex>``, and ``sess`` != ``sess_a``);
      - a DIFFERENT, unrelated session ``sess`` is ALLOWED to read it (``sess`` ==
        the extracted owner).

    So an underscore sid does not merely weaken isolation — it hands the victim's
    scratch table to whichever session matches the pre-first-underscore prefix. The
    scratch materialize endpoint accepts underscore-containing session ids
    (``validate_identifier`` allows ``_``; see ``test_scratch_sid_invariant_qa2``),
    so both ``sess`` and ``sess_a`` are mintable there — this is a real cross-session
    read absent the UI-only ``s<32hex>`` minting discipline. Passing PIN kept beside
    the write-side HIGH xfail that proposes the actual fix (reject ``_`` sids at
    materialize).
    """
    # A different session reading the underscore-owner's table: ALLOWED (the bypass).
    assert _is_allowed(f"s_sess_a_bp_{_HEX}", "sess") is True
    # The genuine owner reading its OWN table: DENIED (ownership inversion).
    assert _is_allowed(f"s_sess_a_bp_{_HEX}", "sess_a") is False


# ---------------------------------------------------------------------------
# 3. Malformed / edge session + table shapes — all fail-closed
# ---------------------------------------------------------------------------


def test_none_session_scratch_reference_denied() -> None:
    """A scratch reference with NO bound session (``session_id=None``) → rejected
    fail-closed (D64 auth-hardening Slice 1: the omit-the-header bypass)."""
    with pytest.raises(ScratchSessionError):
        _read(f"s_a_bp_{_HEX}", None)


def test_empty_string_session_denies_normal_table() -> None:
    """An empty (``""``) bound session cannot read a normally-owned ``s_a_bp_<hex>``
    table — extracted owner ``a`` != ``""``."""
    assert _is_allowed(f"s_a_bp_{_HEX}", "") is False


def test_empty_string_session_degenerate_empty_owner_edge() -> None:
    """EDGE PIN (now CLOSED): an empty bound session ``""`` matched against
    ``s__bp_x`` (a table whose extracted owner is the empty string) is now DENIED.

    The fail-closed guard is ``if not session_id`` (not ``is None``), so a FALSY
    bound session — ``None`` OR the empty string — fail-closes before extraction,
    rather than letting ``"" == ""`` spoof a match against ``s__bp_x``. This is
    defense-in-depth: a bound session is never empty in practice (the write side
    rejects an empty sid with SCRATCH_SESSION_MISSING), but the read gate no longer
    depends on that.
    """
    assert _is_allowed("s__bp_x", "") is False


def test_session_with_sql_metachars_denied() -> None:
    """A bound session carrying SQL metacharacters can never match a
    ``[A-Za-z0-9_]``-only scratch table name → DENIED (never a spoofed match)."""
    assert _is_allowed(f"s_a_bp_{_HEX}", "a'; DROP--") is False


def test_scratch_table_not_starting_s_underscore_denied() -> None:
    """A ``scratch.*`` table that does not start ``s_`` (no extractable owner) →
    rejected fail-closed regardless of session (a naming convention is not an
    access boundary without enforcement)."""
    with pytest.raises((ScratchSessionError, ProvenanceExtractionError)):
        _read("compensation_export", "a")


def test_scratch_table_s_prefix_but_no_suffix_denied() -> None:
    """``s_a`` (an ``s_<owner>`` with NO second ``_`` / empty suffix) → DENIED:
    the gate requires a non-empty suffix after the extracted owner."""
    assert _is_allowed("s_a", "a") is False


def test_scratch_table_single_char_s_denied() -> None:
    """A bare ``s`` scratch table (no ``s_`` prefix at all) → DENIED."""
    with pytest.raises((ScratchSessionError, ProvenanceExtractionError)):
        _read("s", "a")


def test_owner_matches_but_trailing_only_underscore_is_suffix() -> None:
    """``s_a_`` — owner ``a``, but the suffix after the second ``_`` is empty →
    DENIED (``not suffix`` guard). A trailing-underscore table is not a valid
    own-session table even for the right owner."""
    assert _is_allowed("s_a_", "a") is False
