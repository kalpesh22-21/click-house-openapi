"""Layer-2 integration tests for the scratch-write side-channel (Slice 1).

These run against the REAL l2 ClickHouse (localhost:8123 by default) and prove
the hard security invariants end-to-end:

  #1  the scratch credential can write/read ONLY `scratch.*` — a warehouse
      CREATE/INSERT/SELECT with it is denied by the ClickHouse grant;
  #2/#3  isolation, enforced twice: a table materialized as session A carries the
      s_<A>_ prefix (write-side), and session B cannot READ it (the D64 read gate,
      via service.run_query, denies with SCRATCH_SESSION_VIOLATION);
  #4  a cell with SQL metacharacters round-trips create → read as DATA;
  #7  a short-TTL scratch table's rows GC away after the interval;
  plus: create → load → read-back through the READ path with the matching session
  succeeds, and two materializations in one session get distinct names.

Gating: skipped unless RUN_INTEGRATION=1 and the l2 ClickHouse is reachable (see
the local reachability probe below — the scratch tests talk to ClickHouse
directly, not through the REST /health gate the sibling suites use).

REQUIRED ENV (NIT): the two read-back tests exercise the REAL read path
(``service.run_query``), which connects via the process-wide singleton built from
the ambient ``CLICKHOUSE_*`` settings — NOT the ``scratch_settings`` fixture (that
fixture only points the scratch WRITE client).  So run these with the main
connection env pointed at the live l2 ClickHouse, e.g.::

    RUN_INTEGRATION=1 CLICKHOUSE_HOST=localhost CLICKHOUSE_PORT=8123 \\
        CLICKHOUSE_PASSWORD= CLICKHOUSE_DATABASE=default CLICKHOUSE_SECURE=false \\
        pytest tests/integration/test_scratch_write_integration.py

Without the CLICKHOUSE_* override the read-back tests fail against the unit-test
default (:9000).  The direct-write / grant / TTL tests use their own admin client
and only need IT_CH_HOST/IT_CH_PORT (default localhost:8123).
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

# NOTE: deliberately NOT marked `integration`.  The sibling integration suites
# gate on the REST /health endpoint (port 18080); these scratch tests talk to
# ClickHouse directly, so they gate on their own CH-reachability probe (_skip)
# and run whenever RUN_INTEGRATION=1 and the l2 ClickHouse is up.

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


# ---------------------------------------------------------------------------
# Fixtures: an admin client, a warehouse table, scratch settings, and a
# scratch-only ClickHouse user provisioned for the grant (invariant #1) test.
# ---------------------------------------------------------------------------


@pytest.fixture()
def admin():
    import clickhouse_connect

    c = clickhouse_connect.get_client(
        host=IT_CH_HOST, port=IT_CH_PORT, username="default", password=""
    )
    yield c


@pytest.fixture()
def scratch_settings():
    """Settings pointing the scratch client at the live default (full-grant) CH.

    Used for the create/load/read-back path.  The grant-denial test builds its own
    scratch-only user separately (invariant #1).
    """
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
    # identifier-safe session id (underscores only) so the derived table name is a
    # valid unqualified scratch identifier the read gate can parse.
    return "sess_" + uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Create → load → read-back through the READ path with the matching session
# ---------------------------------------------------------------------------


@_skip
class TestMaterializeAndReadBack:
    def test_create_load_readback_same_session_succeeds(self, admin, scratch_settings):
        from app import scratch_ingest
        from app.catalog import get_catalog_schema, invalidate_catalog_cache
        from app.principal import current_scope, current_session_id
        from app import service

        session_id = _sid()
        hostile = "x'; DROP TABLE analytics.employees; --"
        res = scratch_ingest.materialize(
            session_id,
            [{"name": "k", "type": "Int64"}, {"name": "note", "type": "String"}],
            [[1, hostile], [2, "b"], [3, "c"]],
            scratch_settings,
        )
        table = res["table"]  # scratch.s_<sid>_bp_<uuid>
        bare = table.split(".", 1)[1]
        assert res["row_count"] == 3

        try:
            # (a) direct read: the hostile cell round-tripped as DATA (invariant #4)
            rows = admin.query(
                f"SELECT k, note FROM scratch.`{bare}` ORDER BY k"
            ).result_rows
            assert rows == [(1, hostile), (2, "b"), (3, "c")]

            # (b) read-back through the READ path (service.run_query) with the
            # MATCHING bound session → the D64 read gate accepts it.
            invalidate_catalog_cache()
            scope_token = current_scope.set(frozenset())  # allow-all warehouse scope
            sess_token = current_session_id.set(session_id)
            try:
                out = service.run_query(
                    f"SELECT k, note FROM scratch.`{bare}` ORDER BY k",
                    settings=scratch_settings,
                )
            finally:
                current_scope.reset(scope_token)
                current_session_id.reset(sess_token)
            assert out["row_count"] == 3
            assert [r[0] for r in out["rows"]] == [1, 2, 3]
        finally:
            admin.command(f"DROP TABLE IF EXISTS scratch.`{bare}`")

    def test_two_materializations_distinct_names(self, admin, scratch_settings):
        from app import scratch_ingest

        session_id = _sid()
        a = scratch_ingest.materialize(
            session_id, [{"name": "k", "type": "Int64"}], [[1]], scratch_settings
        )
        b = scratch_ingest.materialize(
            session_id, [{"name": "k", "type": "Int64"}], [[2]], scratch_settings
        )
        try:
            assert a["table"] != b["table"]
            assert a["table"].startswith(f"scratch.s_{session_id}_bp_")
            assert b["table"].startswith(f"scratch.s_{session_id}_bp_")
        finally:
            for r in (a, b):
                admin.command(f"DROP TABLE IF EXISTS scratch.`{r['table'].split('.',1)[1]}`")


# ---------------------------------------------------------------------------
# Isolation — session B cannot READ session A's scratch (D64 read gate, live)
# ---------------------------------------------------------------------------


@_skip
class TestIsolationLive:
    def test_session_b_cannot_read_session_a_scratch(self, admin, scratch_settings):
        from app import scratch_ingest, service
        from app.catalog import invalidate_catalog_cache
        from app.errors import ColumnScopeError
        from app.principal import current_scope, current_session_id

        sid_a = _sid()
        sid_b = _sid()
        res = scratch_ingest.materialize(
            sid_a, [{"name": "k", "type": "Int64"}], [[7]], scratch_settings
        )
        bare = res["table"].split(".", 1)[1]
        try:
            invalidate_catalog_cache()
            # Session B (its OWN bound session) tries to read A's scratch table.
            scope_token = current_scope.set(frozenset())
            sess_token = current_session_id.set(sid_b)
            try:
                with pytest.raises(ColumnScopeError) as e:
                    service.run_query(
                        f"SELECT k FROM scratch.`{bare}`", settings=scratch_settings
                    )
                assert e.value.code == "SCRATCH_SESSION_VIOLATION"
            finally:
                current_scope.reset(scope_token)
                current_session_id.reset(sess_token)
        finally:
            admin.command(f"DROP TABLE IF EXISTS scratch.`{bare}`")

    def test_write_is_bound_a_cannot_name_b(self, admin, scratch_settings):
        """A caller acting as session A can only ever produce an s_<A>_ table.

        The name is derived from the bound session_id; there is no request path
        that lets A materialize into B's namespace (invariant #2).
        """
        from app import scratch_ingest

        sid_a = _sid()
        sid_b = _sid()
        res = scratch_ingest.materialize(
            sid_a, [{"name": "k", "type": "Int64"}], [[1]], scratch_settings
        )
        bare = res["table"].split(".", 1)[1]
        try:
            assert bare.startswith(f"s_{sid_a}_")
            assert sid_b not in bare
        finally:
            admin.command(f"DROP TABLE IF EXISTS scratch.`{bare}`")


# ---------------------------------------------------------------------------
# Invariant #1 — the scratch credential is confined to scratch.* by the grant
# ---------------------------------------------------------------------------


@_skip
class TestScratchGrantConfinement:
    def test_scratch_credential_denied_warehouse(self, admin):
        import clickhouse_connect

        user = "it_scratch_only_" + uuid.uuid4().hex[:8]
        pw = "pw_" + uuid.uuid4().hex[:8]
        wh_db = "it_wh_probe_" + uuid.uuid4().hex[:8]
        try:
            admin.command("CREATE DATABASE IF NOT EXISTS scratch")
            admin.command(f"CREATE DATABASE {wh_db}")
            admin.command(f"CREATE TABLE {wh_db}.t (a Int64) ENGINE=MergeTree ORDER BY a")
            admin.command(f"INSERT INTO {wh_db}.t VALUES (1)")
            admin.command(
                f"CREATE USER {user} IDENTIFIED WITH plaintext_password BY '{pw}'"
            )
            # scratch-only grant — NO warehouse grant of any kind
            admin.command(
                f"GRANT CREATE TABLE, INSERT, DROP TABLE, SELECT, CREATE DATABASE "
                f"ON scratch.* TO {user}"
            )

            su = clickhouse_connect.get_client(
                host=IT_CH_HOST, port=IT_CH_PORT, username=user, password=pw
            )
            # Allowed: a scratch write.
            tname = "s_grant_probe_bp_" + uuid.uuid4().hex
            su.command(
                f"CREATE TABLE scratch.`{tname}` (a Int64) ENGINE=MergeTree ORDER BY tuple()"
            )
            su.insert(table=tname, data=[[1]], column_names=["a"], database="scratch")

            # Denied: every warehouse operation is refused by the grant.
            for label, fn in [
                (
                    "CREATE",
                    lambda: su.command(
                        f"CREATE TABLE {wh_db}.evil (a Int64) ENGINE=MergeTree ORDER BY a"
                    ),
                ),
                (
                    "INSERT",
                    lambda: su.insert(
                        table="t", data=[[9]], column_names=["a"], database=wh_db
                    ),
                ),
                ("SELECT", lambda: su.query(f"SELECT * FROM {wh_db}.t")),
            ]:
                with pytest.raises(Exception) as e:  # noqa: PT011
                    fn()
                assert "ACCESS_DENIED" in str(e.value) or "Not enough privileges" in str(
                    e.value
                ), f"{label} was not denied"

            admin.command(f"DROP TABLE IF EXISTS scratch.`{tname}`")
        finally:
            admin.command(f"DROP DATABASE IF EXISTS {wh_db}")
            admin.command(f"DROP USER IF EXISTS {user}")


# ---------------------------------------------------------------------------
# Invariant #7 — a short-TTL scratch table's rows GC away after the interval
# ---------------------------------------------------------------------------


@_skip
class TestScratchTTL:
    def test_short_ttl_rows_expire(self, admin, scratch_settings):
        from app import scratch_ingest

        short = scratch_settings.model_copy(update={"scratch_ttl_seconds": 1})
        session_id = _sid()
        res = scratch_ingest.materialize(
            session_id, [{"name": "k", "type": "Int64"}], [[1], [2], [3]], short
        )
        bare = res["table"].split(".", 1)[1]
        try:
            assert (
                admin.query(f"SELECT count() FROM scratch.`{bare}`").result_rows[0][0]
                == 3
            )
            time.sleep(2)
            # Force the TTL merge (background merges are timer-driven; OPTIMIZE FINAL
            # applies the wall-clock row TTL deterministically).
            admin.command(f"OPTIMIZE TABLE scratch.`{bare}` FINAL")
            assert (
                admin.query(f"SELECT count() FROM scratch.`{bare}`").result_rows[0][0]
                == 0
            )
        finally:
            admin.command(f"DROP TABLE IF EXISTS scratch.`{bare}`")
