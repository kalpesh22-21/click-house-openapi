"""Tests for employee row-level-access provisioning (app/employee_access.py).

Covers, without any live ClickHouse / Redis / HTTP:
  - _parse_codes response-shape leniency + dedup
  - the per-request hook gating (disabled / no-principal / Redis-fresh / CH-fresh)
  - fail-closed-loud on a COLD JTI vs serve-stale on a STALE JTI
  - missing DBA table → EMPLOYEE_ACCESS_NOT_PROVISIONED
  - _fetch_employee_codes forwards the caller's JWT as Bearer
  - _write_pull writes the sentinel row + one row per code in a single insert
  - Redis helpers: hit/miss, best-effort error handling, circuit breaker
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

import app.employee_access as ea
from app.config import Settings
from app.principal import Principal, current_principal


def _settings(**overrides) -> Settings:
    """Build a feature-enabled Settings (required CLICKHOUSE_* come from conftest env)."""
    base = dict(employee_access_enabled=True, eeaccess_base_url="https://api.test/")
    base.update(overrides)
    return Settings(**base)


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _freshness_client(count: int, remaining: int) -> MagicMock:
    """A get_client() stand-in whose freshness query returns (count, remaining)."""
    client = MagicMock()
    client.query.return_value = MagicMock(result_rows=[[count, remaining]])
    return client


@pytest.fixture(autouse=True)
def _reset_redis_state():
    """Isolate the module-level Redis client cache + circuit breaker per test."""
    ea._redis_clients.clear()
    ea._redis_down_until = 0.0
    yield
    ea._redis_clients.clear()
    ea._redis_down_until = 0.0


# ---------------------------------------------------------------------------
# _parse_codes
# ---------------------------------------------------------------------------


class TestParseCodes:
    def test_bare_list_dedup_and_drop_empties(self):
        assert ea._parse_codes(["E1", "E2", "E2", "", None, "E3"]) == ["E1", "E2", "E3"]

    @pytest.mark.parametrize("key", ["employeeCodes", "employee_codes", "codes", "data", "result"])
    def test_dict_envelopes(self, key):
        assert ea._parse_codes({key: ["A", "B"]}) == ["A", "B"]

    def test_non_list_rejected(self):
        with pytest.raises(ValueError):
            ea._parse_codes({"unexpected": 1})

    def test_codes_coerced_to_str(self):
        assert ea._parse_codes([1, 2, 2]) == ["1", "2"]


# ---------------------------------------------------------------------------
# ensure_employee_access_fresh — gating
# ---------------------------------------------------------------------------


class TestHookGating:
    def test_disabled_is_noop(self):
        with patch.object(ea, "get_client") as gc:
            ea.ensure_employee_access_fresh(Settings())  # feature off by default
            gc.assert_not_called()

    def test_no_principal_is_noop(self):
        assert current_principal.get() is None
        with patch.object(ea, "get_client") as gc:
            ea.ensure_employee_access_fresh(_settings())
            gc.assert_not_called()

    def test_missing_jti_claim_fails_closed(self):
        tok = current_principal.set(Principal(subject="s", claims={}, token="t"))
        try:
            with pytest.raises(ea.EmployeeAccessError) as exc:
                ea.ensure_employee_access_fresh(_settings())
            assert exc.value.code == "EMPLOYEE_ACCESS_NO_JTI"
        finally:
            current_principal.reset(tok)

    def test_redis_fresh_short_circuits(self):
        """A Redis hit returns before any ClickHouse read or refresh."""
        p = Principal(subject="s", claims={"jti": "J"}, token="t")
        tok = current_principal.set(p)
        try:
            with patch.object(ea, "_redis_is_fresh", return_value=True), patch.object(
                ea, "get_client"
            ) as gc, patch.object(ea, "_refresh") as refresh:
                ea.ensure_employee_access_fresh(_settings(redis_url="redis://x/0"))
                gc.assert_not_called()
                refresh.assert_not_called()
        finally:
            current_principal.reset(tok)

    def test_ch_fresh_backfills_capped_ttl_and_skips_refresh(self):
        """Redis miss → CH says fresh (remaining < N) → backfill min(remaining, N), no refresh."""
        p = Principal(subject="s", claims={"jti": "J"}, token="t")
        tok = current_principal.set(p)
        s = _settings()  # N defaults to 600
        try:
            with patch.object(ea, "_redis_is_fresh", return_value=False), patch.object(
                ea, "get_client", return_value=_freshness_client(5, 120)
            ), patch.object(ea, "_redis_mark_fresh") as mark, patch.object(
                ea, "_refresh"
            ) as refresh:
                ea.ensure_employee_access_fresh(s)
                refresh.assert_not_called()
                # remaining=120 < N=600 → TTL capped at the actual remaining freshness.
                mark.assert_called_once_with("J", s, 120)
        finally:
            current_principal.reset(tok)

    def test_stale_triggers_refresh_not_cold(self):
        p = Principal(subject="s", claims={"jti": "J"}, token="t")
        tok = current_principal.set(p)
        try:
            with patch.object(ea, "_redis_is_fresh", return_value=False), patch.object(
                ea, "get_client", return_value=_freshness_client(5, -10)
            ), patch.object(ea, "_refresh") as refresh:
                ea.ensure_employee_access_fresh(_settings())
                # has_rows True, remaining<0 → refresh with is_cold=False
                refresh.assert_called_once()
                assert refresh.call_args.kwargs["is_cold"] is False
        finally:
            current_principal.reset(tok)

    def test_cold_triggers_refresh_cold(self):
        p = Principal(subject="s", claims={"jti": "J"}, token="t")
        tok = current_principal.set(p)
        try:
            with patch.object(ea, "_redis_is_fresh", return_value=False), patch.object(
                ea, "get_client", return_value=_freshness_client(0, 0)
            ), patch.object(ea, "_refresh") as refresh:
                ea.ensure_employee_access_fresh(_settings())
                refresh.assert_called_once()
                assert refresh.call_args.kwargs["is_cold"] is True
        finally:
            current_principal.reset(tok)


# ---------------------------------------------------------------------------
# Freshness read — missing table fails closed
# ---------------------------------------------------------------------------


class TestFreshnessProvisioning:
    def test_missing_table_not_provisioned(self):
        client = MagicMock()
        client.query.side_effect = DatabaseError(
            "Code: 60. DB::Exception: Table security.user_employee_access doesn't exist"
        )
        with patch.object(ea, "get_client", return_value=client):
            with pytest.raises(ea.EmployeeAccessError) as exc:
                ea._freshness("J", _settings())
        assert exc.value.code == "EMPLOYEE_ACCESS_NOT_PROVISIONED"

    def test_other_ch_error_is_unavailable(self):
        client = MagicMock()
        client.query.side_effect = DatabaseError("Code: 999. some transient failure")
        with patch.object(ea, "get_client", return_value=client):
            with pytest.raises(ea.EmployeeAccessError) as exc:
                ea._freshness("J", _settings())
        assert exc.value.code == "EMPLOYEE_ACCESS_UNAVAILABLE"

    def test_cold_vs_fresh_parsing(self):
        with patch.object(ea, "get_client", return_value=_freshness_client(0, 0)):
            assert ea._freshness("J", _settings()) == (False, 0)
        with patch.object(ea, "get_client", return_value=_freshness_client(7, 250)):
            assert ea._freshness("J", _settings()) == (True, 250)


# ---------------------------------------------------------------------------
# _fetch_employee_codes — forwards the caller's JWT
# ---------------------------------------------------------------------------


class TestFetch:
    def test_forwards_bearer_jwt_and_system_path(self):
        p = Principal(subject="s", claims={"jti": "J", "system": "cl"}, token="THE.JWT")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["auth"] = req.headers.get("Authorization")
            return _FakeResp(b'["E1","E2"]')

        with patch("urllib.request.urlopen", fake_urlopen):
            codes = ea._fetch_employee_codes(p, _settings())
        assert captured["url"] == "https://api.test/cl/eeaccess"
        assert captured["auth"] == "Bearer THE.JWT"
        assert codes == ["E1", "E2"]

    def test_no_token_rejected(self):
        p = Principal(subject="s", claims={"jti": "J", "system": "cl"}, token="")
        with pytest.raises(ea.EmployeeAccessError) as exc:
            ea._fetch_employee_codes(p, _settings())
        assert exc.value.code == "EMPLOYEE_ACCESS_NO_TOKEN"

    def test_missing_base_url_rejected(self):
        # eeaccess_base_url empty (bypass the enabled-requires-url validator by
        # keeping the feature flag off on this Settings).
        s = Settings(eeaccess_base_url="")
        p = Principal(subject="s", claims={"jti": "J"}, token="t")
        with pytest.raises(ea.EmployeeAccessError) as exc:
            ea._fetch_employee_codes(p, s)
        assert exc.value.code == "EMPLOYEE_ACCESS_MISCONFIGURED"


# ---------------------------------------------------------------------------
# _write_pull — sentinel + atomic single insert
# ---------------------------------------------------------------------------


class TestWritePull:
    def test_codes_written_with_sentinel_single_pull_id(self):
        client = MagicMock()
        with patch.object(ea, "build_security_client", return_value=client):
            ea._write_pull("J", ["E1", "E2"], _settings())
        assert client.insert.call_count == 1
        kwargs = client.insert.call_args.kwargs
        assert kwargs["column_names"] == ["JTI", "EmployeeCode", "pull_id"]
        data = kwargs["data"]
        # sentinel first, then one row per code
        assert data[0][0] == "J" and data[0][1] == ea._SENTINEL_CODE
        assert [r[1] for r in data] == ["", "E1", "E2"]
        pull_ids = {r[2] for r in data}
        assert len(pull_ids) == 1  # one atomic pull_id across the whole insert
        client.close.assert_called_once()

    def test_empty_codes_writes_sentinel_only(self):
        client = MagicMock()
        with patch.object(ea, "build_security_client", return_value=client):
            ea._write_pull("J", [], _settings())
        data = client.insert.call_args.kwargs["data"]
        assert data == [[d[0], d[1], d[2]] for d in data]  # shape sanity
        assert len(data) == 1 and data[0][1] == ea._SENTINEL_CODE


# ---------------------------------------------------------------------------
# _refresh — cold fails loud, stale serves existing set
# ---------------------------------------------------------------------------


class TestRefreshFailureModes:
    def _principal(self):
        return Principal(subject="s", claims={"jti": "J", "system": "cl"}, token="t")

    def test_cold_fetch_failure_fails_closed(self):
        import urllib.error

        with patch.object(
            ea, "_fetch_employee_codes", side_effect=urllib.error.URLError("down")
        ):
            with pytest.raises(ea.EmployeeAccessError):
                ea._refresh("J", self._principal(), _settings(), is_cold=True)

    def test_stale_fetch_failure_serves_existing_and_backs_off(self):
        import urllib.error

        with patch.object(
            ea, "_fetch_employee_codes", side_effect=urllib.error.URLError("down")
        ), patch.object(ea, "_write_pull") as wp, patch.object(
            ea, "_redis_mark_fresh"
        ) as mark:
            ea._refresh("J", self._principal(), _settings(), is_cold=False)  # no raise
            wp.assert_not_called()
            # backoff marker set so a down API is not re-hit every request
            assert mark.call_args.args[0] == "J"
            assert mark.call_args.args[2] == _settings().employee_access_refresh_backoff_seconds

    def test_success_marks_fresh_for_full_window(self):
        s = _settings()
        with patch.object(ea, "_fetch_employee_codes", return_value=["E1"]), patch.object(
            ea, "_write_pull"
        ), patch.object(ea, "_redis_mark_fresh") as mark:
            ea._refresh("J", self._principal(), s, is_cold=True)
            mark.assert_called_once_with("J", s, s.employee_access_refresh_seconds)


# ---------------------------------------------------------------------------
# Redis helpers — best-effort + circuit breaker
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def exists(self, k):
        return 1 if k in self.store else 0

    def set(self, k, v, ex=None):
        self.store[k] = (v, ex)


class _BrokenRedis:
    def exists(self, k):
        raise RuntimeError("connection refused")

    def set(self, k, v, ex=None):
        raise RuntimeError("connection refused")


class TestRedis:
    def test_disabled_when_url_empty(self):
        assert ea._get_redis(Settings()) is None
        assert ea._redis_is_fresh("J", Settings()) is False

    def test_hit_and_miss(self):
        s = _settings(redis_url="redis://fake/0")
        ea._redis_clients[s.redis_url.strip()] = _FakeRedis()
        assert ea._redis_is_fresh("J", s) is False
        ea._redis_mark_fresh("J", s, 300)
        assert ea._redis_is_fresh("J", s) is True

    def test_error_trips_breaker_and_falls_back(self):
        s = _settings(redis_url="redis://fake/0")
        ea._redis_clients[s.redis_url.strip()] = _BrokenRedis()
        # error → False (fall back to CH) and the breaker opens
        assert ea._redis_is_fresh("J", s) is False
        # circuit open → _get_redis short-circuits to None during cooldown
        assert ea._get_redis(s) is None

    def test_mark_fresh_swallows_errors(self):
        s = _settings(redis_url="redis://fake/0")
        ea._redis_clients[s.redis_url.strip()] = _BrokenRedis()
        ea._redis_mark_fresh("J", s, 300)  # must not raise
