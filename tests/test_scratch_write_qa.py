"""QA adversarial suite for the scratch-write side-channel (table-intermediate Slice 1).

This is the *attack* companion to ``tests/test_scratch_ingest.py``.  Where that
file proves the happy-path invariants, this file drives the highest-stakes
failure modes for a WRITE path into the same ClickHouse the warehouse lives on:

  * WAREHOUSE-WRITE (the catastrophe): every body-supplied name/DDL-injection
    attempt must ALWAYS produce a ``scratch.s_<bound-sid>_*`` object and NEVER a
    warehouse identifier — including when SCRATCH_CH_* is unset (grant absent) so
    the *code* is the only wall.
  * CROSS-SESSION drop/overwrite: the ownership prefix check must bind to the
    caller's D92-bound session, never the request body.
  * ROWS-AS-DATA: hostile cells (SQL, backslash, NUL, unicode, 1 MB) must ride
    ``client.insert(data=...)`` and never appear in any ``command()`` SQL text.
  * AUTH / D92 binding: unauthenticated, no-session, and header↔sid_hash mismatch
    must be rejected before any write.
  * IDENTIFIER FAIL-CLOSED: a real-runtime uuid4 (hyphenated) or metachar session
    id must reject SCRATCH_MATERIALIZE_REJECTED, never be mangled into a name.
  * LIMITS: over-cap rows must fail closed BEFORE any DDL is executed.

No live ClickHouse: a fake client records every ``command()``/``insert()`` so we
can prove the SQL text structurally.  Endpoint tests drive the real custom route
behind ``JWTAuthMiddleware`` (so the D92 session binding runs) with the CH client
faked or made to explode-on-connect (to prove fail-closed reject-before-connect).
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

from app import scratch_ingest  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.scratch_ingest import (  # noqa: E402
    ScratchTooLargeError,
    ScratchWriteError,
    build_scratch_create_sql,
    drop as scratch_drop,
    materialize as scratch_materialize,
    scratch_table_name,
)

# A warehouse identifier that MUST NEVER appear in any SQL the scratch path emits.
WAREHOUSE = "dbpcm_warehouse"


# ---------------------------------------------------------------------------
# Fake CH client — records every command()/insert(); explodes if asked to.
# ---------------------------------------------------------------------------


class FakeCHClient:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.inserts: list[dict] = []
        self.closed = False

    def command(self, sql: str) -> None:
        self.commands.append(sql)

    def insert(self, table, data, column_names, database, settings=None) -> None:  # noqa: ANN001
        self.inserts.append(
            {
                "table": table,
                "data": data,
                "column_names": column_names,
                "database": database,
                "settings": settings,
            }
        )

    def close(self) -> None:
        self.closed = True


class ExplodingClientFactory:
    """A build_scratch_client stand-in that fails if a connection is ever attempted.

    Used to prove a request is rejected *before* any ClickHouse connection — i.e.
    validation fail-closed, not a caught connection error.
    """

    def __init__(self) -> None:
        self.called = False

    def __call__(self, settings):  # noqa: ANN001
        self.called = True
        raise AssertionError(
            "build_scratch_client was called — request should have been rejected "
            "BEFORE any ClickHouse connection (fail-closed)."
        )


@pytest.fixture()
def fake_client(monkeypatch):
    fc = FakeCHClient()
    monkeypatch.setattr(scratch_ingest, "build_scratch_client", lambda settings: fc)
    return fc


@pytest.fixture()
def no_connect(monkeypatch):
    """Patch build_scratch_client to explode if called — proves reject-before-connect."""
    factory = ExplodingClientFactory()
    monkeypatch.setattr(scratch_ingest, "build_scratch_client", factory)
    return factory


@pytest.fixture()
def settings():
    return get_settings()


def _all_sql(fc: FakeCHClient) -> str:
    return "\n".join(fc.commands)


# ===========================================================================
# A. WAREHOUSE-WRITE ATTEMPTS — the catastrophe
# ===========================================================================


class TestWarehouseWriteRefused:
    def test_body_table_session_database_fields_are_ignored(self, fake_client, settings):
        """table/session_id/database in the body must NOT influence the object name.

        The name derives ONLY from the bound session_id argument; materialize()
        never even receives the body's table/database fields.
        """
        res = scratch_ingest.materialize(
            "sessowner",
            [{"name": "k", "type": "Int64"}],
            [[1]],
            settings,
        )
        assert res["table"].startswith("scratch.s_sessowner_bp_")
        sql = _all_sql(fake_client)
        # Nothing the attacker could name appears anywhere in the emitted SQL.
        assert WAREHOUSE not in sql
        assert "victim" not in sql
        create_tbl = [c for c in fake_client.commands if c.upper().startswith("CREATE TABLE")]
        assert create_tbl and "`scratch`.`s_sessowner_bp_" in create_tbl[0]
        # Every command targets the scratch schema; no other database is named.
        for c in fake_client.commands:
            assert "`scratch`" in c or c.upper().startswith("CREATE DATABASE")

    @pytest.mark.parametrize(
        "bad_name",
        [
            "col) ENGINE=Log AS SELECT * FROM dbpcm_warehouse.employee --",
            "col`) ENGINE=Log--",
            "..",
            "a.b",
            "a`b",
            "a b",
            "k; DROP TABLE dbpcm_warehouse.employee",
            "*",
        ],
    )
    def test_ddl_injection_via_column_name_rejected_before_connect(
        self, no_connect, settings, bad_name
    ):
        """A DDL-injecting column name is rejected by validate_identifier — and the
        rejection happens BEFORE any ClickHouse client is built (no_connect explodes
        if a connection is attempted)."""
        with pytest.raises(ScratchWriteError) as e:
            scratch_ingest.materialize(
                "sessowner",
                [{"name": bad_name, "type": "Int64"}],
                [[1]],
                settings,
            )
        assert e.value.code == "SCRATCH_MATERIALIZE_REJECTED"
        assert no_connect.called is False

    @pytest.mark.parametrize(
        "bad_type",
        [
            "Int64) ENGINE=Log AS SELECT * FROM dbpcm_warehouse.employee --",
            "Int64; DROP TABLE dbpcm_warehouse.employee",
            "String) --",
            "Enum8('a'=1)",
            "Array(String)",
        ],
    )
    def test_ddl_injection_via_type_rejected_before_connect(
        self, no_connect, settings, bad_type
    ):
        with pytest.raises(ScratchWriteError) as e:
            scratch_ingest.materialize(
                "sessowner",
                [{"name": "k", "type": bad_type}],
                [[1]],
                settings,
            )
        assert e.value.code == "SCRATCH_MATERIALIZE_REJECTED"
        assert no_connect.called is False

    def test_create_sql_always_scratch_qualified_even_with_warehouse_creds(self):
        """With SCRATCH_CH_* UNSET the client falls back to the warehouse creds, so
        the GRANT is no longer the wall — prove the CODE still refuses to name any
        object outside `scratch.*`.

        build_scratch_create_sql is *only* ever called with settings.scratch_database
        (see materialize), so a caller cannot steer it at a warehouse db; and the db
        it does receive is re-validated + backtick-qualified.  A warehouse-looking db
        passed directly is still emitted only if it is a *bare identifier* — it can
        never carry a cross-database ``AS SELECT`` because identifiers are validated.
        """
        # Fallback settings: no SCRATCH_CH_* set → scratch_client_params() inherits
        # the warehouse connection, but scratch_database is still "scratch".
        s = Settings(
            clickhouse_host="localhost",
            clickhouse_port=9000,
            clickhouse_user="default",
            clickhouse_password="pw",
            clickhouse_secure=False,
            oidc_jwks_url="https://idp.test/j",
            oidc_issuer="https://idp.test/",
            oidc_audience="clickhouse-api",
            public_base_url="https://test.example.com",
        )
        params = s.scratch_client_params()
        # Fallback confirmed: the scratch credential IS the warehouse credential.
        assert params["user"] == "default" and params["password"] == "pw"
        # Yet the schema the code writes to is still exactly "scratch".
        assert s.scratch_database == "scratch"
        ddl = build_scratch_create_sql(
            s.scratch_database, "s_sessx_bp_abc", [{"name": "k", "type": "Int64"}], 3600
        )
        assert ddl.startswith("CREATE TABLE `scratch`.`s_sessx_bp_abc`")
        assert WAREHOUSE not in ddl

    def test_materialize_with_fallback_creds_still_targets_scratch(self, fake_client):
        """End-to-end (fake client) under fallback creds: every command is scratch."""
        s = Settings(
            clickhouse_host="localhost",
            clickhouse_port=9000,
            clickhouse_user="default",
            clickhouse_password="pw",
            clickhouse_secure=False,
            oidc_jwks_url="https://idp.test/j",
            oidc_issuer="https://idp.test/",
            oidc_audience="clickhouse-api",
            public_base_url="https://test.example.com",
        )
        scratch_ingest.materialize(
            "sessx", [{"name": "k", "type": "Int64"}], [[1]], s
        )
        for c in fake_client.commands:
            if c.upper().startswith("CREATE DATABASE"):
                assert "`scratch`" in c
            else:
                assert "`scratch`.`s_sessx_bp_" in c
        assert fake_client.inserts[0]["database"] == "scratch"


# ===========================================================================
# B. CROSS-SESSION DROP / OVERWRITE
# ===========================================================================


class TestCrossSession:
    def test_drop_prefix_check_binds_to_session_arg_not_body(self, fake_client, settings):
        """drop() authorizes against the session_id ARGUMENT (route-bound), while the
        table name is the only body-controlled value — a foreign-prefixed name is 403
        and NO DROP reaches ClickHouse."""
        with pytest.raises(ScratchWriteError) as e:
            scratch_drop("sess_b", "scratch.s_sess_a_bp_x", settings)
        assert e.value.code == "SCRATCH_SESSION_VIOLATION"
        assert e.value.status_code == 403
        assert fake_client.commands == []

    def test_drop_prefix_confusion_rejected(self, fake_client, settings):
        """Session 'sess_a' must not drop 's_sess_ab_*' (prefix confusion) — the
        trailing underscore in the expected prefix prevents it."""
        with pytest.raises(ScratchWriteError) as e:
            scratch_drop("sess_a", "scratch.s_sess_ab_bp_x", settings)
        assert e.value.code == "SCRATCH_SESSION_VIOLATION"
        assert fake_client.commands == []

    def test_drop_own_prefix_but_injection_rejected(self, fake_client, settings):
        """A drop target that starts with the own-session prefix but smuggles DDL
        after it is refused fail-closed BEFORE any command().

        FIX 1: the drop authorizer now requires an EXACT ``s_<sid>_bp_<32hex>``
        structure (the only shape materialize could have produced), so an injection
        payload never matches and is rejected SCRATCH_SESSION_VIOLATION (403) —
        an earlier, stronger fail-closed than the previous validate_identifier
        catch (which returned SCRATCH_MATERIALIZE_REJECTED).  The load-bearing
        guarantee is unchanged: nothing reaches ClickHouse."""
        with pytest.raises(ScratchWriteError) as e:
            scratch_drop(
                "sess_b",
                "scratch.s_sess_b_bp_x`; DROP TABLE dbpcm_warehouse.employee --",
                settings,
            )
        assert e.value.code == "SCRATCH_SESSION_VIOLATION"
        assert e.value.status_code == 403
        assert fake_client.commands == []

    def test_drop_missing_or_nonstring_table_rejected(self, fake_client, settings):
        for bad in (None, "", 123, ["scratch.s_sess_b_bp_x"]):
            with pytest.raises(ScratchWriteError) as e:
                scratch_drop("sess_b", bad, settings)
            assert e.value.code == "SCRATCH_MATERIALIZE_REJECTED"
        assert fake_client.commands == []

    def test_write_name_is_header_derived_cannot_forge_other_session(self):
        """Session B can only ever produce an s_<B>_ name — there is no code path
        that yields an s_<A>_ name for a caller bound to B (isolation invariant #2)."""
        b = scratch_table_name("sessb")
        assert b.startswith("s_sessb_bp_")
        assert "sessa" not in b


# ===========================================================================
# C. ROWS-AS-DATA — hostile cells ride insert(data=), never command() SQL
# ===========================================================================


class TestRowsAsData:
    def test_hostile_cells_roundtrip_as_data_only(self, fake_client, settings):
        one_mb = "A" * (1024 * 1024)
        cells = [
            "'; DROP TABLE dbpcm_warehouse.employee; --",
            "back\\slash",
            "nul\x00byte",
            "unïcodé — 😀 — ‮RTL",
            one_mb,
        ]
        cols = [{"name": f"c{i}", "type": "String"} for i in range(len(cells))]
        res = scratch_ingest.materialize("sessx", cols, [cells], settings)
        assert res["row_count"] == 1
        # Exactly one native insert; every hostile cell verbatim in data=, not SQL.
        assert len(fake_client.inserts) == 1
        assert fake_client.inserts[0]["data"] == [cells]
        sql = _all_sql(fake_client)
        for cell in cells:
            assert cell not in sql
        assert "DROP TABLE" not in sql.upper()
        assert WAREHOUSE not in sql
        # No command carries an INSERT — rows never became SQL text (invariant #4).
        assert not any("INSERT" in c.upper() for c in fake_client.commands)

    @pytest.mark.parametrize("row", [[1], [1, 2, 3], []])
    def test_wrong_arity_row_rejected_no_ddl(self, no_connect, settings, row):
        """Fewer OR more cells than columns → reject BEFORE any connection/DDL."""
        with pytest.raises(ScratchWriteError) as e:
            scratch_ingest.materialize(
                "sessx",
                [{"name": "a", "type": "Int64"}, {"name": "b", "type": "Int64"}],
                [row],
                settings,
            )
        assert e.value.code == "SCRATCH_MATERIALIZE_REJECTED"
        assert no_connect.called is False

    def test_wrong_type_cell_coerces_to_null_not_reject(self, fake_client, settings):
        """PINNED BEHAVIOR: a non-numeric string for an Int64 column is silently
        coerced to NULL (None), NOT rejected.  This is a data-integrity quirk, not a
        security hole (the value never reaches SQL), but callers must be aware the
        write does not fail on a type-mismatched cell."""
        scratch_ingest.materialize(
            "sessx", [{"name": "k", "type": "Int64"}], [["not-a-number"]], settings
        )
        assert fake_client.inserts[0]["data"] == [[None]]

    def test_non_string_cell_passes_through_untouched(self, fake_client, settings):
        """int/float/bool/None cells bypass coerce() and go straight to insert data=."""
        scratch_ingest.materialize(
            "sessx",
            [
                {"name": "i", "type": "Int64"},
                {"name": "f", "type": "Float64"},
                {"name": "b", "type": "Bool"},
                {"name": "n", "type": "Nullable(Int64)"},
            ],
            [[7, 1.5, True, None]],
            settings,
        )
        assert fake_client.inserts[0]["data"] == [[7, 1.5, True, None]]

    def test_empty_rows_list_creates_table_zero_rows(self, fake_client, settings):
        res = scratch_ingest.materialize(
            "sessx", [{"name": "k", "type": "Int64"}], [], settings
        )
        assert res["row_count"] == 0
        # The table is still created (a downstream JOIN can reference an empty table).
        assert any(c.upper().startswith("CREATE TABLE") for c in fake_client.commands)
        assert fake_client.inserts[0]["data"] == []

    def test_duplicate_column_rejected_before_connect(self, no_connect, settings):
        with pytest.raises(ScratchWriteError) as e:
            scratch_ingest.materialize(
                "sessx",
                [{"name": "k", "type": "Int64"}, {"name": "k", "type": "String"}],
                [[1, "x"]],
                settings,
            )
        assert e.value.code == "SCRATCH_MATERIALIZE_REJECTED"
        assert no_connect.called is False


# ===========================================================================
# D. LIMITS — over-cap fails closed BEFORE any DDL
# ===========================================================================


class TestLimits:
    def test_over_cap_rows_fail_closed_before_connect(self, no_connect):
        capped = get_settings().model_copy(update={"scratch_max_rows": 2})
        with pytest.raises(ScratchTooLargeError) as e:
            scratch_ingest.materialize(
                "sessx",
                [{"name": "k", "type": "Int64"}],
                [[1], [2], [3]],
                capped,
            )
        assert e.value.code == "SCRATCH_TOO_LARGE"
        assert e.value.status_code == 413
        assert no_connect.called is False

    def test_exactly_at_cap_allowed(self, fake_client):
        capped = get_settings().model_copy(update={"scratch_max_rows": 3})
        res = scratch_ingest.materialize(
            "sessx", [{"name": "k", "type": "Int64"}], [[1], [2], [3]], capped
        )
        assert res["row_count"] == 3


# ===========================================================================
# E. IDENTIFIER FAIL-CLOSED — real-runtime session-id shapes
# ===========================================================================


class TestIdentifierFailClosed:
    def test_uuid4_hyphenated_session_id_fails_closed(self, no_connect, settings):
        """The real runtime session id is a uuid4 like
        '11111111-2222-3333-4444-555555555555'.  Its hyphens make an UNSAFE scratch
        identifier, so materialize must reject SCRATCH_MATERIALIZE_REJECTED rather
        than silently mangle it into a bad/foreign name — and BEFORE any connection.
        """
        sid = "11111111-2222-3333-4444-555555555555"
        with pytest.raises(ScratchWriteError) as e:
            scratch_ingest.materialize(
                sid, [{"name": "k", "type": "Int64"}], [[1]], settings
            )
        assert e.value.code == "SCRATCH_MATERIALIZE_REJECTED"
        assert no_connect.called is False

    @pytest.mark.parametrize(
        "sid",
        ["a.b", "a b", "a`b", "a; DROP", "s_x_'; --", "évil"],
    )
    def test_metachar_session_id_fails_closed(self, no_connect, settings, sid):
        with pytest.raises(ScratchWriteError) as e:
            scratch_ingest.materialize(
                sid, [{"name": "k", "type": "Int64"}], [[1]], settings
            )
        assert e.value.code in ("SCRATCH_MATERIALIZE_REJECTED", "SCRATCH_SESSION_MISSING")
        assert no_connect.called is False

    def test_leading_digit_session_id_is_accepted_not_a_bypass(self, fake_client, settings):
        """PINNED: a session id with a leading digit (e.g. '1leading') is ACCEPTED —
        the derived name 's_1leading_bp_..' is a valid identifier and stays scoped to
        that session's own prefix (the read gate uses the same s_<sid>_ prefix), so
        it is NOT a cross-session bypass.  Documented so the contract is explicit."""
        res = scratch_ingest.materialize(
            "1leading", [{"name": "k", "type": "Int64"}], [[1]], settings
        )
        assert res["table"].startswith("scratch.s_1leading_bp_")


# ===========================================================================
# Endpoint-level — real custom route behind JWTAuthMiddleware (D92 binding)
# ===========================================================================

from starlette.testclient import TestClient  # noqa: E402

from app.mcp_server import JWTAuthMiddleware, mcp  # noqa: E402
from tests.jwt_helpers import make_jwt  # noqa: E402


def _authed_app():
    # require_sid_binding now defaults to False (the external minter does not
    # stamp sid_hash).  The D92 mismatch tests below assert the binding rejects a
    # forged X-Session-Id, so the harness enables it explicitly; the header-less
    # tests are unaffected either way.
    settings = get_settings().model_copy(update={"require_sid_binding": True})
    return JWTAuthMiddleware(mcp.streamable_http_app(), settings=settings)


def _hdrs(session_id: str | None):
    h = {"Authorization": f"Bearer {make_jwt(session_id=session_id)}"}
    if session_id is not None:
        h["X-Session-Id"] = session_id
    return h


class TestAuthAndBindingRoutes:
    def test_materialize_unauthenticated_401(self):
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post("/scratch/v1/materialize", json={})
        assert resp.status_code == 401

    def test_drop_unauthenticated_401(self):
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post("/scratch/v1/drop", json={"table": "scratch.s_x_bp_y"})
        assert resp.status_code == 401

    def test_materialize_valid_jwt_no_session_header_rejected(self):
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post(
            "/scratch/v1/materialize",
            headers={"Authorization": f"Bearer {make_jwt()}"},
            json={"columns": [{"name": "k", "type": "Int64"}], "rows": [[1]]},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "SCRATCH_SESSION_MISSING"

    def test_drop_valid_jwt_no_session_header_rejected(self):
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post(
            "/scratch/v1/drop",
            headers={"Authorization": f"Bearer {make_jwt()}"},
            json={"table": "scratch.s_x_bp_y"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "SCRATCH_SESSION_MISSING"

    def test_materialize_header_sid_mismatch_403_d92(self, no_connect):
        """JWT bound to sess_a but X-Session-Id=sess_b → 403 SESSION_BINDING_MISMATCH
        at the middleware, BEFORE the route/service runs (no connection attempted)."""
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post(
            "/scratch/v1/materialize",
            headers={
                "Authorization": f"Bearer {make_jwt(session_id='sess_a')}",
                "X-Session-Id": "sess_b",
            },
            json={"columns": [{"name": "k", "type": "Int64"}], "rows": [[1]]},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "SESSION_BINDING_MISMATCH"
        assert no_connect.called is False

    def test_drop_header_sid_mismatch_403_d92(self):
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post(
            "/scratch/v1/drop",
            headers={
                "Authorization": f"Bearer {make_jwt(session_id='sess_a')}",
                "X-Session-Id": "sess_b",
            },
            json={"table": "scratch.s_sess_b_bp_x"},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "SESSION_BINDING_MISMATCH"

    def test_sessionless_token_with_header_cannot_write_scratch_403(self, no_connect):
        """A token WITHOUT a sid_hash claim cannot present an X-Session-Id: the D92
        binding rejects it 403.  So a session-less token can NEVER materialize scratch
        (it also can't without the header — see next test)."""
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post(
            "/scratch/v1/materialize",
            headers={
                "Authorization": f"Bearer {make_jwt()}",  # no session_id → no sid_hash
                "X-Session-Id": "sessx",
            },
            json={"columns": [{"name": "k", "type": "Int64"}], "rows": [[1]]},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "SESSION_BINDING_MISMATCH"
        assert no_connect.called is False

    def test_sessionless_token_no_header_cannot_write_scratch(self):
        """...and without the header the same token is SCRATCH_SESSION_MISSING — so
        there is NO way for a session-less token to write scratch."""
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post(
            "/scratch/v1/materialize",
            headers={"Authorization": f"Bearer {make_jwt()}"},
            json={"columns": [{"name": "k", "type": "Int64"}], "rows": [[1]]},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "SCRATCH_SESSION_MISSING"


class TestRoutesNotTools:
    def test_scratch_routes_absent_and_exactly_six_read_tools(self):
        import asyncio

        tools = asyncio.run(mcp.list_tools())
        names = {t.name for t in tools}
        assert not any("scratch" in n.lower() for n in names)
        assert not any("materialize" in n.lower() or "drop" in n.lower() for n in names)
        assert names == {
            "listDatabases",
            "listTables",
            "getTableSchema",
            "sampleRows",
            "runQuery",
            "explainQuery",
        }


class TestMaterializeRouteWarehouseWrite:
    def test_body_table_session_database_ignored_route(self, monkeypatch):
        """Full route: a body that tries to name a warehouse/foreign table, its own
        session_id, AND a database field — all IGNORED.  The created object is
        scratch.s_<bound-sid>_* and the warehouse name never reaches the DDL."""
        fc = FakeCHClient()
        monkeypatch.setattr(scratch_ingest, "build_scratch_client", lambda settings: fc)
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post(
            "/scratch/v1/materialize",
            headers=_hdrs("sessowner"),
            json={
                "table": f"{WAREHOUSE}.employee",
                "database": WAREHOUSE,
                "session_id": "sessvictim",
                "columns": [{"name": "k", "type": "Int64"}],
                "rows": [[1]],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["table"].startswith("scratch.s_sessowner_bp_")
        sql = "\n".join(fc.commands)
        assert WAREHOUSE not in sql
        assert "victim" not in sql
        create = [c for c in fc.commands if c.upper().startswith("CREATE TABLE")]
        assert create and "`scratch`.`s_sessowner_bp_" in create[0]

    def test_ddl_injection_column_name_rejected_route(self, no_connect):
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post(
            "/scratch/v1/materialize",
            headers=_hdrs("sessowner"),
            json={
                "columns": [
                    {
                        "name": "col) ENGINE=Log AS SELECT * FROM dbpcm_warehouse.employee --",
                        "type": "Int64",
                    }
                ],
                "rows": [[1]],
            },
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "SCRATCH_MATERIALIZE_REJECTED"
        assert no_connect.called is False

    def test_uuid4_session_id_fails_closed_route(self, no_connect):
        """Real-runtime uuid4 (hyphenated) session id: the D92 binding PASSES (sid_hash
        of the header matches the claim), then materialize fail-closes on the unsafe
        identifier — 400 SCRATCH_MATERIALIZE_REJECTED, no connection attempted."""
        sid = "11111111-2222-3333-4444-555555555555"
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post(
            "/scratch/v1/materialize",
            headers=_hdrs(sid),
            json={"columns": [{"name": "k", "type": "Int64"}], "rows": [[1]]},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "SCRATCH_MATERIALIZE_REJECTED"
        assert no_connect.called is False

    def test_over_cap_rows_route_413_no_connect(self, no_connect, monkeypatch):
        """Over-cap at the route returns 413 SCRATCH_TOO_LARGE with NO connection."""
        monkeypatch.setenv("SCRATCH_MAX_ROWS", "1")
        get_settings.cache_clear()
        try:
            client = TestClient(_authed_app(), raise_server_exceptions=False)
            resp = client.post(
                "/scratch/v1/materialize",
                headers=_hdrs("sessowner"),
                json={"columns": [{"name": "k", "type": "Int64"}], "rows": [[1], [2]]},
            )
        finally:
            get_settings.cache_clear()
        assert resp.status_code == 413
        assert resp.json()["code"] == "SCRATCH_TOO_LARGE"
        assert no_connect.called is False


class TestDropRouteCrossSession:
    def test_cross_session_drop_403_binds_to_header(self, no_connect):
        """Bound to sessowner, body names sessvictim's table → 403, no connection."""
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post(
            "/scratch/v1/drop",
            headers=_hdrs("sessowner"),
            json={"table": "scratch.s_sessvictim_bp_x"},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "SCRATCH_SESSION_VIOLATION"
        assert no_connect.called is False

    def test_own_prefix_injection_drop_rejected_route(self, no_connect):
        # FIX 1: the exact-structure drop gate refuses an own-prefix-with-injection
        # target as SCRATCH_SESSION_VIOLATION (403) before any CH connection — an
        # earlier fail-closed than the prior validate_identifier 400.  Still no
        # connection is opened.
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post(
            "/scratch/v1/drop",
            headers=_hdrs("sessowner"),
            json={"table": "scratch.s_sessowner_bp_x`; DROP TABLE dbpcm_warehouse.employee --"},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "SCRATCH_SESSION_VIOLATION"
        assert no_connect.called is False
