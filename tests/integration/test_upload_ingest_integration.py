"""Layer-2 integration test for the /scratch/v1/upload route (UI Slice 4).

Exercises the authoritative front-door path end-to-end against a REAL l2
ClickHouse: multipart file + mapping → parse → rename → scratch_ingest.materialize
→ a session-scoped ``scratch.s_<sid>_bp_<uuid>`` table with the mapping-renamed
canonical join keys.  Mirrors the gating of
tests/integration/test_scratch_write_integration.py (RUN_INTEGRATION=1 + a live
ClickHouse reachable on :8123); a plain ``pytest tests/`` run skips it.
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


@pytest.fixture()
def admin():
    import clickhouse_connect

    c = clickhouse_connect.get_client(
        host=IT_CH_HOST, port=IT_CH_PORT, username="default", password=""
    )
    yield c


@pytest.fixture()
def scratch_settings():
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
    # Identifier-safe AND underscore-free (Slice-2 contract for the read gate).
    return "s" + uuid.uuid4().hex


@_skip
class TestUploadMaterializeLive:
    def test_csv_upload_with_mapping_materializes_renamed_columns(
        self, admin, scratch_settings
    ):
        from app.principal import current_session_id
        from app import upload_ingest
        from app.scratch_ingest import materialize as scratch_materialize

        session_id = _sid()
        csv_bytes = b"Emp Code,Dept,Note\nE1,D1,hi\nE2,D2,yo\n"
        columns, rows = upload_ingest.parse_upload(
            csv_bytes, "people.csv", scratch_settings.scratch_max_rows
        )
        columns, rows = upload_ingest.apply_mapping(
            columns, rows, {"emp_code": "EmployeeCode", "dept": "DepartmentCode"}
        )
        # The route derives session_id from the D92 binding; here we call the same
        # service function the route calls, with the bound session set.
        token = current_session_id.set(session_id)
        try:
            res = scratch_materialize(session_id, columns, rows, scratch_settings)
        finally:
            current_session_id.reset(token)

        table = res["table"]
        bare = table.split(".", 1)[1]
        try:
            assert res["row_count"] == 2
            assert bare.startswith(f"s_{session_id}_bp_")
            # The renamed canonical keys are the real column names in ClickHouse.
            names = {
                r[0]
                for r in admin.query(
                    "SELECT name FROM system.columns "
                    "WHERE database='scratch' AND table={t:String}",
                    parameters={"t": bare},
                ).result_rows
            }
            assert {"employee_code", "department_code", "note"} <= names
            data = admin.query(
                f"SELECT employee_code, department_code, note FROM scratch.`{bare}` "
                "ORDER BY employee_code"
            ).result_rows
            assert data == [("E1", "D1", "hi"), ("E2", "D2", "yo")]
        finally:
            admin.command(f"DROP TABLE IF EXISTS scratch.`{bare}`")
