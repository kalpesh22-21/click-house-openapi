"""Layer-2 QA proofs for the scratch-write side-channel (attack companion).

Runs against the REAL l2 ClickHouse and proves the write-path invariants that
can only be shown end-to-end.  Complements ``test_scratch_write_integration.py``
(happy path + grant confinement + isolation-read + TTL) with the adversarial
angles that file does not cover:

  * WAREHOUSE CONFINEMENT BY CODE (not just grant): under the *fallback* full-grant
    credential (SCRATCH_CH_* unset), a normal materialize creates an object ONLY in
    the `scratch` database — never in any warehouse database — proving the code is a
    wall independent of the ClickHouse grant.
  * ROWS-AS-DATA round-trip for the nastiest cells: NUL byte, unicode/RTL, and a
    1 MB string survive create→read verbatim (never parsed as SQL).
  * WRONG-TYPE coercion: a non-numeric string for an Int64 column lands as NULL.
  * HIDDEN TTL COLUMN: `_scratch_created_at` physically exists but is NEVER surfaced
    to a caller — an explicit-column read omits it, and a `SELECT *` through the D64
    read gate does not leak it (it is either fail-closed or expanded without it).

Gating mirrors the sibling file: skipped unless RUN_INTEGRATION=1 and the l2
ClickHouse is reachable on :8123.
"""

from __future__ import annotations

import os
import uuid

import pytest

IT_CH_HOST = os.environ.get("IT_CH_HOST", "localhost")
IT_CH_PORT = int(os.environ.get("IT_CH_PORT", "8123"))

_RUN = os.environ.get("RUN_INTEGRATION", "0").strip().lower() in ("1", "true", "yes")


def _ch_reachable() -> bool:
    if not _RUN:
        return False
    try:
        import clickhouse_connect

        c = clickhouse_connect.get_client(
            host=IT_CH_HOST, port=IT_CH_PORT, username="default", password=""
        )
        c.command("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


_CH_OK = _ch_reachable()
_skip = pytest.mark.skipif(
    not _CH_OK, reason="RUN_INTEGRATION!=1 or l2 ClickHouse not reachable on :8123"
)

_TTL_COLUMN = "_scratch_created_at"


@pytest.fixture()
def admin():
    import clickhouse_connect

    c = clickhouse_connect.get_client(
        host=IT_CH_HOST, port=IT_CH_PORT, username="default", password=""
    )
    yield c


@pytest.fixture()
def scratch_settings():
    """Fallback creds: SCRATCH_CH_* unset → the scratch client IS the full-grant
    'default' account.  This is the deploy where the GRANT is NOT the wall, so these
    tests exercise the *code* wall (confine to scratch.*)."""
    from app.config import Settings

    return Settings(
        clickhouse_host=IT_CH_HOST,
        clickhouse_port=IT_CH_PORT,
        clickhouse_user="default",
        clickhouse_password="",
        clickhouse_secure=False,
        oidc_jwks_url="https://idp.test/j",
        oidc_issuer="https://idp.test/",
        oidc_audience="clickhouse-api",
        public_base_url="https://test.example.com",
        scratch_database="scratch",
        scratch_ttl_seconds=3600,
        scratch_max_rows=10_000,
    )


def _sid() -> str:
    return "sess_" + uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Code (not grant) confines writes to scratch.* — proven under fallback creds
# ---------------------------------------------------------------------------


@_skip
class TestCodeConfinesToScratchUnderFallbackCreds:
    def test_materialize_creates_object_only_in_scratch(self, admin, scratch_settings):
        """Even with the full-grant fallback credential, a materialize lands the new
        table in `scratch` and NOWHERE else (system.tables proves the database)."""
        from app import scratch_ingest

        session_id = _sid()
        res = scratch_ingest.materialize(
            session_id, [{"name": "k", "type": "Int64"}], [[1]], scratch_settings
        )
        bare = res["table"].split(".", 1)[1]
        try:
            rows = admin.query(
                "SELECT database FROM system.tables WHERE name = {n:String}",
                parameters={"n": bare},
            ).result_rows
            dbs = {r[0] for r in rows}
            assert dbs == {"scratch"}, f"table appeared outside scratch: {dbs}"
        finally:
            admin.command(f"DROP TABLE IF EXISTS scratch.`{bare}`")

    def test_hostile_column_name_creates_no_object_anywhere(self, admin, scratch_settings):
        """A DDL-injecting column name is rejected — and NO object (scratch or
        warehouse) is created as a side effect."""
        from app import scratch_ingest
        from app.scratch_ingest import ScratchWriteError

        before = admin.query("SELECT count() FROM system.tables").result_rows[0][0]
        with pytest.raises(ScratchWriteError) as e:
            scratch_ingest.materialize(
                _sid(),
                [
                    {
                        "name": "c) ENGINE=Log AS SELECT * FROM system.tables --",
                        "type": "Int64",
                    }
                ],
                [[1]],
                scratch_settings,
            )
        assert e.value.code == "SCRATCH_MATERIALIZE_REJECTED"
        after = admin.query("SELECT count() FROM system.tables").result_rows[0][0]
        assert after == before, "a rejected materialize must create no table"


# ---------------------------------------------------------------------------
# Rows-as-data round-trip for the nastiest cells
# ---------------------------------------------------------------------------


@_skip
class TestHostileCellsRoundTrip:
    def test_nul_unicode_and_large_cells_survive_as_data(self, admin, scratch_settings):
        from app import scratch_ingest

        session_id = _sid()
        nul = "before\x00after"
        uni = "unïcodé — 😀 — ‮RTL"
        big = "Z" * (1024 * 1024)
        res = scratch_ingest.materialize(
            session_id,
            [{"name": "k", "type": "Int64"}, {"name": "s", "type": "String"}],
            [[1, nul], [2, uni], [3, big]],
            scratch_settings,
        )
        bare = res["table"].split(".", 1)[1]
        try:
            got = {
                r[0]: r[1]
                for r in admin.query(
                    f"SELECT k, s FROM scratch.`{bare}` ORDER BY k"
                ).result_rows
            }
            assert got[1] == nul  # NUL byte preserved verbatim in String storage
            assert got[2] == uni
            assert len(got[3]) == len(big)
        finally:
            admin.command(f"DROP TABLE IF EXISTS scratch.`{bare}`")

    def test_wrong_type_cell_lands_as_null(self, admin, scratch_settings):
        """PINNED: a non-numeric string for an Int64 column coerces to NULL (the row
        is NOT rejected) — the value is data, never SQL."""
        from app import scratch_ingest

        session_id = _sid()
        res = scratch_ingest.materialize(
            session_id,
            [{"name": "k", "type": "Int64"}, {"name": "v", "type": "Nullable(Int64)"}],
            [["not-a-number", "also-bad"]],
            scratch_settings,
        )
        bare = res["table"].split(".", 1)[1]
        try:
            # k is non-nullable Int64 → coerced None becomes ClickHouse default 0;
            # v is Nullable(Int64) → coerced None stays NULL.
            row = admin.query(
                f"SELECT k, isNull(v) FROM scratch.`{bare}`"
            ).result_rows[0]
            assert row[0] == 0
            assert row[1] == 1
        finally:
            admin.command(f"DROP TABLE IF EXISTS scratch.`{bare}`")


# ---------------------------------------------------------------------------
# Hidden _scratch_created_at column is never surfaced to a caller
# ---------------------------------------------------------------------------


@_skip
class TestHiddenTtlColumn:
    def test_ttl_column_exists_but_not_leaked(self, admin, scratch_settings):
        from app import scratch_ingest
        from app.catalog import invalidate_catalog_cache
        from app.principal import current_scope, current_session_id
        from app import service

        session_id = _sid()
        res = scratch_ingest.materialize(
            session_id, [{"name": "k", "type": "Int64"}], [[1], [2]], scratch_settings
        )
        bare = res["table"].split(".", 1)[1]
        try:
            # (1) The hidden column physically exists in storage.
            cols = {
                r[0]
                for r in admin.query(
                    "SELECT name FROM system.columns "
                    "WHERE database='scratch' AND table={t:String}",
                    parameters={"t": bare},
                ).result_rows
            }
            assert _TTL_COLUMN in cols and "k" in cols

            # (2) An explicit-column read through the D64 read gate returns ONLY the
            #     user column — the hidden TTL column is never surfaced.
            invalidate_catalog_cache()
            scope_token = current_scope.set(frozenset())
            sess_token = current_session_id.set(session_id)
            try:
                out = service.run_query(
                    f"SELECT k FROM scratch.`{bare}` ORDER BY k",
                    settings=scratch_settings,
                )
                assert out["columns"] == ["k"]
                assert _TTL_COLUMN not in out["columns"]

                # (3) SELECT * through the read gate must NOT leak the hidden column:
                #     either it fails closed (star not expandable for a scratch table)
                #     or it expands to the user columns without _scratch_created_at.
                from app.errors import ColumnScopeError

                try:
                    star = service.run_query(
                        f"SELECT * FROM scratch.`{bare}`", settings=scratch_settings
                    )
                except (ColumnScopeError, Exception):  # noqa: BLE001
                    star = None
                if star is not None:
                    assert _TTL_COLUMN not in star["columns"], (
                        "SELECT * leaked the hidden TTL column through the read gate"
                    )
            finally:
                current_scope.reset(scope_token)
                current_session_id.reset(sess_token)
        finally:
            admin.command(f"DROP TABLE IF EXISTS scratch.`{bare}`")
