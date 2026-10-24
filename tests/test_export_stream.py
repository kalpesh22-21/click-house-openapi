"""Tests for the answer-snapshot export ClickHouse path.

Covers:
  - export_settings(): the caps bundle derives EXCLUSIVELY from EXPORT_* fields
    (never the interactive path's MAX_* overrides), carries
    result_overflow_mode='throw' (exports fail loud, never silently truncate),
    leaves max_rows_to_read unchanged, and passes readonly / final /
    select_sequential_consistency through exactly as the interactive path does.
  - execute_query_rows_stream(): the tenant/CHProxy transport is IDENTICAL to
    execute_query — RLS rls_* binds + merged caps in ONE SQL-body SETTINGS
    clause on the default CHProxy path, caps-only clause with no principal,
    full readonly_settings-style settings= merge on the legacy direct path, and
    literal '%' escaping when client-side binds ride along.

ClickHouse is never contacted: a stub client captures exactly what the helper
hands to clickhouse-connect's streaming machinery.
"""

from __future__ import annotations

import contextlib
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.clickhouse_client import (
    execute_query_rows_stream,
    export_settings,
    readonly_settings,
)
from app.config import get_settings
from app.principal import Principal, current_principal

# Hermetic env pin (same discipline as tests/test_tenant_settings.py): shields
# the transport assertions from a developer's repo-root .env.
_EXPORT_ENV = {
    "EMPLOYEE_ACCESS_ENABLED": "false",
    "CLICKHOUSE_RLS_VIA_SQL_SETTINGS": "true",
    "CLICKHOUSE_READONLY": "2",
    "CLICKHOUSE_SELECT_FINAL": "false",
    "SCRATCH_CLUSTER": "",
    # Interactive-path caps pinned to values that DIFFER from the export
    # defaults — if any interactive value leaks into the export bundle the
    # difference is observable.
    "MAX_EXECUTION_TIME": "30",
    "MAX_RESULT_ROWS": "10000",
    "MAX_RESULT_BYTES": "4194304",
    "MAX_ROWS_TO_READ": "100000000",
    "EXPORT_MAX_EXECUTION_TIME": "120",
    "EXPORT_TOTAL_MAX_ROWS": "1000000",
    "EXPORT_TOTAL_MAX_BYTES": "104857600",
    "CLICKHOUSE_TENANT_SETTINGS": (
        '{"paycom_client_code": "clientcode", '
        '"paycom_proc_center": "proc_center", '
        '"paycom_authenticated_user": "jti"}'
    ),
}


@contextlib.contextmanager
def _pinned_settings(**overrides: str):
    with patch.dict(os.environ, {**_EXPORT_ENV, **overrides}):
        get_settings.cache_clear()
        try:
            yield get_settings()
        finally:
            get_settings.cache_clear()


def _principal() -> Principal:
    return Principal(
        subject="sub-1",
        claims={"clientcode": "CLIENT_A", "proc_center": "PC01", "jti": "TESTJTI001"},
    )


class _FakeType:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeStream:
    """Stand-in for StreamContext: context manager + row iterator."""

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def __iter__(self):
        return iter(self._rows)


class _FakeQueryResult:
    """Stand-in for the streaming QueryResult returned by the stub client."""

    def __init__(self, columns, type_names, rows):
        self.column_names = tuple(columns)
        self.column_types = tuple(_FakeType(n) for n in type_names)
        self._rows = rows

    @property
    def rows_stream(self):
        stream = _FakeStream(self._rows)
        stream.source = self
        return stream


def _run_stream(sql: str = "SELECT 1", *, caps: dict, rows=None, with_principal=True):
    """Run execute_query_rows_stream against a stub client.

    Returns (query, parameters, settings, columns, type_names, iterated_rows) —
    exactly what the helper handed to clickhouse-connect's query_rows_stream, plus
    what the wrapper exposes/yields.
    """
    mock_client = MagicMock()
    result = _FakeQueryResult(["x"], ["UInt8"], rows if rows is not None else [[1]])
    mock_client.query_rows_stream.return_value = result.rows_stream

    token = current_principal.set(_principal()) if with_principal else None
    try:
        with patch("app.clickhouse_client.get_client", return_value=mock_client):
            stream = execute_query_rows_stream(sql, settings=get_settings(), caps=caps)
            with stream as row_iter:
                iterated = [list(r) for r in row_iter]
    finally:
        if token is not None:
            current_principal.reset(token)

    call = mock_client.query_rows_stream.call_args
    lcls = call.kwargs
    return (
        lcls["query"],
        lcls["parameters"],
        lcls["settings"],
        stream.column_names,
        stream.column_type_names,
        iterated,
    )


# ---------------------------------------------------------------------------
# export_settings — the EXPORT_*-exclusive caps bundle
# ---------------------------------------------------------------------------

class TestExportSettingsBundle:

    def test_values_come_from_export_fields_only(self):
        with _pinned_settings() as s:
            caps = export_settings(s)
        assert caps == {
            "readonly": 2,
            "max_execution_time": 120,          # EXPORT_MAX_EXECUTION_TIME, not MAX_EXECUTION_TIME
            "max_result_rows": 1_000_001,       # EXPORT_TOTAL_MAX_ROWS + 1 (local budget wins)
            "max_result_bytes": 104_857_600,    # EXPORT_TOTAL_MAX_BYTES
            "result_overflow_mode": "throw",    # fail loud — never silently truncate
            "max_rows_to_read": 100_000_000,    # unchanged from the interactive path
            "prefer_column_name_to_alias": 0,
        }

    def test_result_overflow_mode_is_throw_even_when_interactive_is_break(self):
        with _pinned_settings() as s:
            assert readonly_settings(s)["result_overflow_mode"] == "throw"
            assert export_settings(s)["result_overflow_mode"] == "throw"

    def test_interactive_max_overrides_do_not_leak(self):
        # Operator retunes the interactive path; the export bundle must not move.
        with _pinned_settings(
            MAX_EXECUTION_TIME="55", MAX_RESULT_ROWS="42", MAX_RESULT_BYTES="999"
        ) as s:
            caps = export_settings(s)
        assert caps["max_execution_time"] == 120
        assert caps["max_result_rows"] == 1_000_001
        assert caps["max_result_bytes"] == 104_857_600

    def test_export_fields_move_the_bundle(self):
        with _pinned_settings(
            EXPORT_MAX_EXECUTION_TIME="45",
            EXPORT_TOTAL_MAX_ROWS="500",
            EXPORT_TOTAL_MAX_BYTES="2048",
        ) as s:
            caps = export_settings(s)
        assert caps["max_execution_time"] == 45
        assert caps["max_result_rows"] == 501
        assert caps["max_result_bytes"] == 2048

    def test_readonly_final_consistency_passthrough(self):
        with _pinned_settings(CLICKHOUSE_SELECT_FINAL="true", SCRATCH_CLUSTER="prod") as s:
            caps = export_settings(s)
        assert caps["readonly"] == 2
        assert caps["final"] == 1
        assert caps["select_sequential_consistency"] == 1
        with _pinned_settings() as s:
            caps = export_settings(s)
        assert "final" not in caps
        assert "select_sequential_consistency" not in caps

    def test_native_types_preserved(self):
        with _pinned_settings() as s:
            caps = export_settings(s)
        assert isinstance(caps["max_execution_time"], int)
        assert isinstance(caps["max_result_rows"], int)
        assert isinstance(caps["max_result_bytes"], int)
        assert isinstance(caps["result_overflow_mode"], str)


# ---------------------------------------------------------------------------
# execute_query_rows_stream — transport parity with execute_query
# ---------------------------------------------------------------------------

class TestRowStreamTransport:

    def test_rls_transport_preserves_caps_on_settings_channel(self):
        with _pinned_settings() as s:
            caps = export_settings(s)
            query, params, settings_kwarg, cols, types, rows = _run_stream(caps=caps)
        assert query.count("SETTINGS") == 1
        assert "max_execution_time" not in query
        assert params["rls_paycom_client_code"] == "CLIENT_A"
        assert params["rls_paycom_proc_center"] == "PC01"
        assert params["rls_paycom_authenticated_user"] == "TESTJTI001"
        assert settings_kwarg == caps
        assert cols == ["x"] and types == ["UInt8"] and rows == [[1]]

    def test_no_principal_still_enforces_caps(self):
        with _pinned_settings() as s:
            caps = export_settings(s)
            query, params, settings_kwarg, *_ = _run_stream(caps=caps, with_principal=False)
        assert "SETTINGS" not in query
        assert settings_kwarg == caps

    def test_legacy_transport_merges_everything_on_settings_channel(self):
        # CLICKHOUSE_RLS_VIA_SQL_SETTINGS=false: no clause at all; tenant values
        # first, caps LAST so a tenant key can never loosen the caps.
        with _pinned_settings(CLICKHOUSE_RLS_VIA_SQL_SETTINGS="false") as s:
            caps = export_settings(s)
            query, params, settings_kwarg, *_ = _run_stream(caps=caps)

        assert "SETTINGS" not in query
        assert params is None
        assert settings_kwarg == {
            "paycom_client_code": "CLIENT_A",
            "paycom_proc_center": "PC01",
            "paycom_authenticated_user": "TESTJTI001",
            **caps,
        }

    def test_literal_percent_escaped_when_binds_ride_along(self):
        with _pinned_settings() as s:
            caps = export_settings(s)
            query, _params, *_ = _run_stream(
                "SELECT count() FROM t WHERE a LIKE '654%'", caps=caps
            )
        # The wildcard '%' doubles for clickhouse-connect's printf pass; the
        # real %(name)s placeholders are preserved.
        assert "LIKE '654%%'" in query
        assert "%(rls_paycom_client_code)s" in query

    def test_caller_parameters_preserved_without_mutation(self):
        with _pinned_settings() as s:
            caps = export_settings(s)
            original = {"dbs": ["warehouse"]}
            mock_client = MagicMock()
            mock_client.query_rows_stream.return_value = _FakeQueryResult(["x"], ["UInt8"], []).rows_stream
            token = current_principal.set(_principal())
            try:
                with patch("app.clickhouse_client.get_client", return_value=mock_client):
                    stream = execute_query_rows_stream(
                        "SELECT 1", settings=get_settings(), parameters=original, caps=caps
                    )
                    with stream:
                        pass
            finally:
                current_principal.reset(token)

        assert original == {"dbs": ["warehouse"]}  # copied, never mutated
        params = mock_client.query_rows_stream.call_args.kwargs["parameters"]
        assert params["dbs"] == ["warehouse"]
        assert params["rls_paycom_client_code"] == "CLIENT_A"

    def test_query_error_at_call_maps_to_400(self):
        from clickhouse_connect.driver.exceptions import DatabaseError

        mock_client = MagicMock()
        mock_client.query_rows_stream.side_effect = DatabaseError("syntax error at host db:8123")
        with _pinned_settings() as s:
            caps = export_settings(s)
            token = current_principal.set(_principal())
            try:
                with patch("app.clickhouse_client.get_client", return_value=mock_client):
                    with pytest.raises(HTTPException) as exc_info:
                        execute_query_rows_stream("SELECT 1", settings=get_settings(), caps=caps)
            finally:
                current_principal.reset(token)
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == "CLICKHOUSE_QUERY_ERROR"

    def test_mid_iteration_failure_maps_to_400(self):
        from clickhouse_connect.driver.exceptions import DatabaseError

        def _exploding():
            yield [1]
            raise DatabaseError("size of result is larger than allowed")

        class _ExplodingStream(_FakeStream):
            def __iter__(self):
                return _exploding()

        class _ExplodingResult(_FakeQueryResult):
            @property
            def rows_stream(self):
                stream = _ExplodingStream([])
                stream.source = self
                return stream

        mock_client = MagicMock()
        mock_client.query_rows_stream.return_value = _ExplodingResult(["x"], ["UInt8"], []).rows_stream
        with _pinned_settings() as s:
            caps = export_settings(s)
            token = current_principal.set(_principal())
            try:
                with patch("app.clickhouse_client.get_client", return_value=mock_client):
                    stream = execute_query_rows_stream("SELECT 1", settings=get_settings(), caps=caps)
                    with pytest.raises(HTTPException) as exc_info:
                        with stream as row_iter:
                            list(row_iter)
            finally:
                current_principal.reset(token)
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == "CLICKHOUSE_QUERY_ERROR"
