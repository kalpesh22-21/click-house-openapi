"""Layer-1 tests for the scratch-write side-channel (table-intermediate Slice 1).

Pure unit tests — NO live ClickHouse.  A fake ClickHouse client records every
`.command(sql)` and `.insert(...)` so we can prove the load-bearing security
invariants structurally:

  #2  the written table name is derived from the bound session_id, never the body;
  #4  rows are native-inserted (data=), never string-interpolated into SQL;
  #5  the write route is NOT an MCP tool (absent from list_tools);
  #6  an oversized result fails closed (SCRATCH_TOO_LARGE);
  scratch-only-schema: every DDL targets the `scratch` database;
  identifier/type whitelist: hostile column names/types are rejected.

Endpoint-level tests drive the real custom route behind JWTAuthMiddleware (so the
D92 session binding applies) with the scratch service faked out.
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
from app.config import get_settings  # noqa: E402
from app.scratch_ingest import (  # noqa: E402
    ScratchTooLargeError,
    ScratchWriteError,
    _bare_scratch_name,
    _validate_columns,
    build_scratch_create_sql,
    scratch_table_name,
)


# ---------------------------------------------------------------------------
# Fake ClickHouse client — records DDL commands and native inserts
# ---------------------------------------------------------------------------


class FakeCHClient:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.inserts: list[dict] = []
        self.closed = False

    def command(self, sql: str) -> None:
        self.commands.append(sql)

    def insert(self, table, data, column_names, database) -> None:  # noqa: ANN001
        self.inserts.append(
            {
                "table": table,
                "data": data,
                "column_names": column_names,
                "database": database,
            }
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def fake_client(monkeypatch):
    fc = FakeCHClient()
    monkeypatch.setattr(scratch_ingest, "build_scratch_client", lambda settings: fc)
    return fc


@pytest.fixture()
def settings():
    return get_settings()


# ---------------------------------------------------------------------------
# Naming — derived from session_id, never the body
# ---------------------------------------------------------------------------


class TestScratchTableName:
    def test_name_derives_from_session_prefix(self):
        name = scratch_table_name("sess_abc123")
        assert name.startswith("s_sess_abc123_bp_")
        # read-gate prefix contract (D64): startswith s_<session_id>_ and longer
        assert name.startswith("s_sess_abc123_") and len(name) > len("s_sess_abc123_")

    def test_two_names_in_one_session_are_distinct(self):
        a = scratch_table_name("sess_abc123")
        b = scratch_table_name("sess_abc123")
        assert a != b

    def test_empty_session_rejected(self):
        with pytest.raises(ScratchWriteError) as e:
            scratch_table_name("")
        assert e.value.code == "SCRATCH_SESSION_MISSING"

    def test_hostile_session_id_rejected(self):
        # A session_id with SQL/identifier-hostile chars cannot form a safe name.
        for bad in ["a; DROP", "a-b", "a`b", "a.b", "a b"]:
            with pytest.raises(ScratchWriteError) as e:
                scratch_table_name(bad)
            assert e.value.code == "SCRATCH_MATERIALIZE_REJECTED"


# ---------------------------------------------------------------------------
# Column identifier / type whitelist
# ---------------------------------------------------------------------------


class TestColumnValidation:
    def test_valid_columns_pass(self):
        cols = _validate_columns(
            [{"name": "k", "type": "Int64"}, {"name": "note", "type": "Nullable(String)"}]
        )
        assert cols == [
            {"name": "k", "type": "Int64"},
            {"name": "note", "type": "Nullable(String)"},
        ]

    @pytest.mark.parametrize(
        "bad_name",
        ["k; DROP TABLE x", "1bad", "has space", "a`b", "a.b", "évil", ""],
    )
    def test_hostile_column_name_rejected(self, bad_name):
        with pytest.raises(ScratchWriteError) as e:
            _validate_columns([{"name": bad_name, "type": "Int64"}])
        assert e.value.code == "SCRATCH_MATERIALIZE_REJECTED"

    @pytest.mark.parametrize(
        "bad_type",
        ["Int64; DROP", "Enum8('a'=1)", "FixedString(4)", "Array(Int64)", "String)", ""],
    )
    def test_disallowed_type_rejected(self, bad_type):
        with pytest.raises(ScratchWriteError) as e:
            _validate_columns([{"name": "k", "type": bad_type}])
        assert e.value.code == "SCRATCH_MATERIALIZE_REJECTED"

    def test_reserved_ttl_column_name_rejected(self):
        with pytest.raises(ScratchWriteError) as e:
            _validate_columns([{"name": "_scratch_created_at", "type": "Int64"}])
        assert e.value.code == "SCRATCH_MATERIALIZE_REJECTED"

    def test_duplicate_column_rejected(self):
        with pytest.raises(ScratchWriteError):
            _validate_columns(
                [{"name": "k", "type": "Int64"}, {"name": "k", "type": "String"}]
            )

    def test_empty_columns_rejected(self):
        with pytest.raises(ScratchWriteError):
            _validate_columns([])


# ---------------------------------------------------------------------------
# DDL always targets the scratch schema
# ---------------------------------------------------------------------------


class TestScratchDDL:
    def test_ddl_targets_scratch_and_has_ttl(self):
        ddl = build_scratch_create_sql(
            "scratch", "s_sess_x_bp_abc", [{"name": "k", "type": "Int64"}], 3600
        )
        assert "`scratch`.`s_sess_x_bp_abc`" in ddl
        assert "ENGINE = MergeTree" in ddl
        assert "TTL `_scratch_created_at` + INTERVAL 3600 SECOND" in ddl

    def test_ddl_rejects_non_scratch_by_identifier(self):
        # A hostile database string is rejected by validate_identifier.
        with pytest.raises(ValueError):
            build_scratch_create_sql(
                "warehouse; DROP", "t", [{"name": "k", "type": "Int64"}], 3600
            )


# ---------------------------------------------------------------------------
# materialize() — native insert, session-derived name, scratch-only schema
# ---------------------------------------------------------------------------


class TestMaterialize:
    def test_native_insert_rows_are_data_not_sql(self, fake_client, settings):
        hostile = "a'; DROP TABLE analytics.employees; --"
        res = scratch_ingest.materialize(
            "sess_abc123",
            [{"name": "k", "type": "Int64"}, {"name": "note", "type": "String"}],
            [[1, hostile], [2, "b"]],
            settings,
        )
        # name derived from session, scratch-qualified
        assert res["table"].startswith("scratch.s_sess_abc123_bp_")
        assert res["row_count"] == 2

        # exactly one native insert; rows passed as data=, hostile cell verbatim
        assert len(fake_client.inserts) == 1
        ins = fake_client.inserts[0]
        assert ins["database"] == "scratch"
        assert ins["column_names"] == ["k", "note"]
        assert ins["data"] == [[1, hostile], [2, "b"]]

        # NO command carries the row data — rows never became SQL text (invariant #4)
        for sql in fake_client.commands:
            assert "INSERT" not in sql.upper()
            assert hostile not in sql

        # every DDL command targets the scratch schema only
        create_db = [c for c in fake_client.commands if c.upper().startswith("CREATE DATABASE")]
        create_tbl = [c for c in fake_client.commands if c.upper().startswith("CREATE TABLE")]
        assert create_db and "scratch" in create_db[0]
        assert create_tbl and "`scratch`.`s_sess_abc123_bp_" in create_tbl[0]
        assert fake_client.closed is True

    def test_body_cannot_override_table_name(self, fake_client, settings):
        # There is no table/session field in the contract; even if a caller smuggles
        # extra keys in a column dict they cannot influence the derived name.
        res = scratch_ingest.materialize(
            "sess_owner",
            [{"name": "k", "type": "Int64", "table": "scratch.s_victim_bp_x"}],
            [[1]],
            settings,
        )
        assert res["table"].startswith("scratch.s_sess_owner_bp_")
        assert "victim" not in res["table"]

    def test_oversized_result_fails_closed(self, fake_client, settings):
        capped = settings.model_copy(update={"scratch_max_rows": 3})
        with pytest.raises(ScratchTooLargeError) as e:
            scratch_ingest.materialize(
                "sess_abc123",
                [{"name": "k", "type": "Int64"}],
                [[i] for i in range(4)],
                capped,
            )
        assert e.value.code == "SCRATCH_TOO_LARGE"
        assert e.value.status_code == 413
        # nothing was created — fail closed BEFORE any DDL
        assert fake_client.commands == []
        assert fake_client.inserts == []

    def test_ragged_row_rejected(self, fake_client, settings):
        with pytest.raises(ScratchWriteError) as e:
            scratch_ingest.materialize(
                "sess_abc123",
                [{"name": "k", "type": "Int64"}, {"name": "v", "type": "Int64"}],
                [[1]],  # too short
                settings,
            )
        assert e.value.code == "SCRATCH_MATERIALIZE_REJECTED"

    def test_string_cell_coerced_by_type(self, fake_client, settings):
        # A numeric string for an Int64 column is coerced to int native data.
        scratch_ingest.materialize(
            "sess_abc123",
            [{"name": "k", "type": "Int64"}],
            [["42"]],
            settings,
        )
        assert fake_client.inserts[0]["data"] == [[42]]


# ---------------------------------------------------------------------------
# drop() — own-session only
# ---------------------------------------------------------------------------


_HEX = "0123456789abcdef0123456789abcdef"  # a valid 32-char bp_ suffix body


class TestDrop:
    def test_bare_name_own_session(self):
        name = f"s_sess_me_bp_{_HEX}"
        assert _bare_scratch_name(f"scratch.{name}", "sess_me") == name
        assert _bare_scratch_name(name, "sess_me") == name

    def test_cross_session_drop_rejected(self):
        with pytest.raises(ScratchWriteError) as e:
            _bare_scratch_name(f"scratch.s_sess_other_bp_{_HEX}", "sess_me")
        assert e.value.code == "SCRATCH_SESSION_VIOLATION"
        assert e.value.status_code == 403

    def test_underscore_boundary_prefix_cannot_drop_neighbor(self):
        """FIX 1: session `a` must NOT be able to drop `a_b`'s table.

        A loose ``startswith("s_a_")`` prefix would let session `a` match
        ``s_a_b_bp_<hex>`` (owned by session ``a_b``).  The exact-structure match
        anchors the whole name, so the neighbor's table is refused 403.
        """
        neighbor = f"s_a_b_bp_{_HEX}"  # owned by session "a_b"
        with pytest.raises(ScratchWriteError) as e:
            _bare_scratch_name(f"scratch.{neighbor}", "a")  # bound to session "a"
        assert e.value.code == "SCRATCH_SESSION_VIOLATION"
        assert e.value.status_code == 403
        # ...and the true owner ("a_b") CAN drop it.
        assert _bare_scratch_name(f"scratch.{neighbor}", "a_b") == neighbor

    def test_malformed_suffix_rejected(self):
        # own prefix but not a bp_<32hex> shape materialize could have produced
        for bad in ["s_sess_me_bp_abc", "s_sess_me_onboarding", f"s_sess_me_bp_{_HEX}x"]:
            with pytest.raises(ScratchWriteError) as e:
                _bare_scratch_name(bad, "sess_me")
            assert e.value.code == "SCRATCH_SESSION_VIOLATION"

    def test_drop_targets_scratch_schema(self, fake_client, settings):
        name = f"s_sess_me_bp_{_HEX}"
        res = scratch_ingest.drop("sess_me", f"scratch.{name}", settings)
        assert res == {"dropped": True}
        assert len(fake_client.commands) == 1
        assert f"`scratch`.`{name}`" in fake_client.commands[0]
        assert fake_client.commands[0].upper().startswith("DROP TABLE IF EXISTS")

    def test_drop_cross_session_never_touches_ch(self, fake_client, settings):
        with pytest.raises(ScratchWriteError):
            scratch_ingest.drop("sess_me", f"scratch.s_sess_other_bp_{_HEX}", settings)
        assert fake_client.commands == []


# ---------------------------------------------------------------------------
# Endpoint-level — real route behind JWTAuthMiddleware (D92 session binding)
# ---------------------------------------------------------------------------

from starlette.testclient import TestClient  # noqa: E402

from app.mcp_server import JWTAuthMiddleware, mcp  # noqa: E402
from tests.jwt_helpers import make_jwt  # noqa: E402


def _authed_app():
    return JWTAuthMiddleware(mcp.streamable_http_app(), settings=get_settings())


class TestScratchRoutesNotTools:
    def test_scratch_absent_from_list_tools(self):
        import asyncio

        tools = asyncio.run(mcp.list_tools())
        names = {t.name for t in tools}
        assert not any("scratch" in n.lower() for n in names)
        assert names == {
            "listDatabases",
            "listTables",
            "getTableSchema",
            "sampleRows",
            "runQuery",
            "explainQuery",
        }


class TestMaterializeRoute:
    def test_missing_session_rejected(self):
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        # Valid JWT, but NO X-Session-Id header → current_session_id is None.
        resp = client.post(
            "/scratch/v1/materialize",
            headers={"Authorization": f"Bearer {make_jwt()}"},
            json={"columns": [{"name": "k", "type": "Int64"}], "rows": [[1]]},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "SCRATCH_SESSION_MISSING"

    def test_unauthenticated_rejected(self):
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post("/scratch/v1/materialize", json={})
        assert resp.status_code == 401

    def test_session_derived_from_header_not_body(self, monkeypatch):
        # Patch the STABLE dependency (build_scratch_client) rather than the route's
        # module global: test_mcp.py re-imports app.mcp_server, so a string-path
        # patch on that module can miss the route's actual closure.  The real
        # materialize() runs here against a fake client, proving the table name is
        # derived from the bound header session — not the body.
        fc = FakeCHClient()
        monkeypatch.setattr(scratch_ingest, "build_scratch_client", lambda settings: fc)
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post(
            "/scratch/v1/materialize",
            headers={
                "Authorization": f"Bearer {make_jwt(session_id='sess_owner')}",
                "X-Session-Id": "sess_owner",
            },
            json={
                # A body attempt to name a foreign table/session is IGNORED.
                "table": "scratch.s_victim_bp_x",
                "session_id": "sess_victim",
                "columns": [{"name": "k", "type": "Int64"}],
                "rows": [[1]],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["table"].startswith("scratch.s_sess_owner_bp_")
        assert "victim" not in resp.json()["table"]
        # the DDL executed against the fake client targets the owner's namespace
        create = [c for c in fc.commands if c.upper().startswith("CREATE TABLE")]
        assert create and "`scratch`.`s_sess_owner_bp_" in create[0]
        assert "victim" not in create[0]

    def test_service_error_maps_to_code(self):
        # An un-whitelisted column type is rejected by validation BEFORE any CH
        # client is built, so no patching is needed — the real reject path runs.
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post(
            "/scratch/v1/materialize",
            headers={
                "Authorization": f"Bearer {make_jwt(session_id='sess_owner')}",
                "X-Session-Id": "sess_owner",
            },
            json={"columns": [{"name": "k", "type": "Array(Int64)"}], "rows": [[1]]},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "SCRATCH_MATERIALIZE_REJECTED"


class TestDropRoute:
    def test_cross_session_drop_403(self, monkeypatch):
        client = TestClient(_authed_app(), raise_server_exceptions=False)
        resp = client.post(
            "/scratch/v1/drop",
            headers={
                "Authorization": f"Bearer {make_jwt(session_id='sess_owner')}",
                "X-Session-Id": "sess_owner",
            },
            json={"table": "scratch.s_sess_victim_bp_x"},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "SCRATCH_SESSION_VIOLATION"
