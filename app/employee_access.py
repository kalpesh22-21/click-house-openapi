"""Employee row-level-security (RLS) access provisioning.

This module populates ``security.user_employee_access`` — the table a ClickHouse
row policy on ``dbpcm_warehouse.employee`` reads to filter each tenant's rows to
only the employees that tenant's JWT (identified by its stable composite ``JTI``)
is permitted to see.  It is the WRITE half of the RLS feature; the row policy
itself (the READ half) is a DBA-managed object (see the canonical DDL below).

Design (settled with the maintainer):

  * **Storage** — ``ReplacingMergeTree(pull_id) ORDER BY (JTI, EmployeeCode)``.
    Each *pull* of a JTI's access set is written with a fresh, monotonically
    increasing ``pull_id``.  The row policy reads ONLY the latest pull:

        EmployeeCode IN (
            SELECT EmployeeCode FROM security.user_employee_access
            WHERE JTI = getSetting('SQL_TENANT')
              AND pull_id = (SELECT max(pull_id) FROM security.user_employee_access
                             WHERE JTI = getSetting('SQL_TENANT'))
        )

    The ``pull_id = max(pull_id)`` gate is what makes **revocation** correct:
    ReplacingMergeTree alone cannot express "this employee is gone" (a revoked
    code simply stops getting new rows and its old row lingers until TTL), so
    without the gate the policy would keep returning revoked codes — a silent
    over-grant.  The gate excludes any code not present in the newest pull, and
    it is correct BEFORE any background merge, so ``FINAL`` is never needed.
    ReplacingMergeTree then compacts the table to one row per (JTI, EmployeeCode).

  * **Atomic swap** — the whole pull is written in a SINGLE ``client.insert`` so
    that, on the single-node deployment, ``max(pull_id)`` advances in one atomic
    step.  A partial (multi-block) insert would briefly expose a half-loaded set.

  * **Sentinel row** — every pull also writes ``(JTI, '', pull_id)``.  ``''`` can
    never equal a real ``EmployeeCode``, so it never grants anything, but it
    guarantees ``max(pull_id)`` advances even when the access set is EMPTY (all
    revoked).  Without it, an empty pull would leave the previous pull as the
    "latest" and fail to revoke-to-empty.

  * **Staleness-gated refresh (Redis in front, ClickHouse authoritative)** — on
    every data query we first check a best-effort Redis marker ``eeaccess:fresh:
    {JTI}``; if present the set is fresh and we do NOTHING (no CH read, no pull).
    On a miss — or whenever Redis is disabled, cooling-down after an error, or
    unreachable — we fall back to the cheap CH read ``SELECT count(), N - age``
    keyed by JTI (JTI leads the sort key → fast range scan) and, if fresh,
    backfill the Redis marker with the REMAINING freshness so revocation latency
    stays bounded by N.  Only a stale-or-cold JTI triggers a pull from
    ``/cl/eeaccess`` and a single insert; on success we mark Redis fresh for N.
    Correctness never depends on Redis — it is purely a latency/load shedder, and
    the shared marker also throttles the cross-pod refresh herd.

  * **Fail-closed-loud** — a COLD JTI (never pulled) whose ``/cl/eeaccess`` fetch
    fails raises :class:`EmployeeAccessError` (HTTP 502 / MCP unreachable) rather
    than letting the query run against an empty set and silently return
    "you can see no employees".  A STALE-but-present JTI whose refresh fails
    serves the existing set and logs (availability over freshness).

  * **Least privilege** — a DISTINCT, server-side-only ClickHouse credential
    (``SECURITY_CH_*``, mirroring the scratch writer) GRANTed only **INSERT +
    SELECT** on ``security.user_employee_access`` handles ALL app access to the
    security database: BOTH the per-request freshness read AND the pull insert go
    through it (a cached, thread-safe client), so the ordinary ``mcp_user`` query
    connection never touches ``security.*`` at the app layer.  The table and the
    row policy are **DBA-owned** objects (see the canonical DDL below); the app
    issues NO DDL — refresh is append-only and eviction is the table's own TTL, so
    no CREATE/DELETE/ALTER grant is needed.  (ClickHouse still evaluates the row
    policy's subquery in the *querying* user's context, so ``mcp_user`` separately
    needs SELECT on this table for the policy itself — that grant is for the DB
    engine, not for any query this app issues as ``mcp_user``.)

  * **Provisioning** — if the feature is enabled but the DBA table is absent, the
    freshness read fails closed loud (``EMPLOYEE_ACCESS_NOT_PROVISIONED``) rather
    than silently returning zero rows — a clear signal the migration was not run.

Canonical DBA objects (create once, out of band — the row policy references
``mcp_user`` and the warehouse and cannot be created by the writer credential):

    CREATE DATABASE IF NOT EXISTS security;

    CREATE TABLE IF NOT EXISTS security.user_employee_access
    (
        JTI          String,
        EmployeeCode String,
        pull_id      UInt64,
        UpdatedAt    DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(pull_id)
    ORDER BY (JTI, EmployeeCode)
    TTL UpdatedAt + INTERVAL 1 DAY;   -- housekeeping only; MUST exceed N (600s)

    CREATE ROW POLICY employee_rls ON dbpcm_warehouse.employee
    USING
            ClientCode  = getSetting('SQL_CLIENTCODE')
        AND ProcCenter  = getSetting('SQL_PROCCENTER')
        AND EmployeeCode IN (
            SELECT EmployeeCode FROM security.user_employee_access
            WHERE JTI = getSetting('SQL_TENANT')
              AND pull_id = (SELECT max(pull_id) FROM security.user_employee_access
                             WHERE JTI = getSetting('SQL_TENANT'))
        )
    TO mcp_user;

``SQL_TENANT`` / ``SQL_CLIENTCODE`` / ``SQL_PROCCENTER`` are injected per request
via ``clickhouse_tenant_settings`` (config), so the map must contain e.g.
``{"SQL_tenant":"jti","SQL_CLIENTCODE":"clientcode","SQL_PROCCENTER":"proc_center"}``.
The LLM cannot override them — ``security.py`` blocks any user ``SETTINGS`` clause.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import quote

import clickhouse_connect
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError

from app.ingest_primitives import validate_identifier
from app.clickhouse_client import readonly_settings
from app.config import Settings, get_settings
from app.errors import ClickHouseUnavailableError
from app.principal import Principal, get_current_principal

logger = logging.getLogger(__name__)

# The bare table name under the configured security database.
_TABLE = "user_employee_access"

# The sentinel EmployeeCode written on every pull so max(pull_id) advances even
# for an empty access set.  Empty string can never equal a real employee code, so
# it grants nothing while keeping the pull pointer well-defined.
_SENTINEL_CODE = ""


class EmployeeAccessError(ClickHouseUnavailableError):
    """The caller's employee-access set could not be established — fail closed.

    Subclasses :class:`ClickHouseUnavailableError` so both transports map it
    without new wiring: HTTP 502 in the REST layer and a generic "temporarily
    unreachable" ToolError in the MCP layer.  Carries its own stable ``code``.
    """

    def __init__(
        self, message: str, code: str = "EMPLOYEE_ACCESS_UNAVAILABLE"
    ) -> None:
        super().__init__(message, code)


# ---------------------------------------------------------------------------
# Privileged, security-only writer client (mirrors build_scratch_client)
# ---------------------------------------------------------------------------


# Process-wide cached security client.  ALL security.* access — both the
# per-request freshness read and the rare pull insert — goes through this
# credential, so the ordinary mcp_user client never touches the security database
# at the app layer.  Cached (not one-off) because the freshness read is on the hot
# path; a fresh TLS handshake per request would defeat "the cheap read".
_security_client: Optional[Client] = None


def _build_security_client(settings: Settings) -> Client:
    """Construct the security-credential ClickHouse client (server config, not request)."""
    params = settings.security_client_params()
    host = str(params["host"])
    port = int(params["port"])
    user = str(params["user"])
    password = str(params["password"])
    secure = bool(params["secure"])
    logger.info(
        "Building security ClickHouse client: host=%s port=%s secure=%s user=%s db=%s",
        host,
        port,
        secure,
        user,
        settings.security_database,
    )
    # Connect at the server level (no default database) so every statement is
    # explicitly `security`-qualified.  autogenerate_session_id=False is
    # load-bearing for thread safety on the shared singleton (same reason as the
    # main client in app/clickhouse_client._build_client): with a session_id the
    # driver forbids concurrent queries and raises on the second one.
    return clickhouse_connect.get_client(
        host=host,
        port=port,
        username=user,
        password=password,
        secure=secure,
        verify=secure,
        autogenerate_session_id=False,
    )


def _get_security_client(settings: Settings) -> Client:
    """Return the process-wide cached security client (INSERT + SELECT on security.*)."""
    global _security_client
    if _security_client is None:
        _security_client = _build_security_client(settings)
    return _security_client


# ---------------------------------------------------------------------------
# Table reference (the table itself is a DBA-owned object — the app issues no DDL)
# ---------------------------------------------------------------------------


def _qualified_table(settings: Settings) -> str:
    """Return the backtick-quoted `db`.`table`, with the db identifier validated."""
    db = validate_identifier(settings.security_database)
    return f"`{db}`.`{_TABLE}`"


# ---------------------------------------------------------------------------
# Redis freshness cache (best-effort; ClickHouse is the authoritative fallback)
# ---------------------------------------------------------------------------

# One client per Redis URL, built lazily. redis-py clients are thread-safe (they
# own a connection pool), so a single instance serves both transports.
_redis_clients: dict[str, Any] = {}

# Circuit breaker: after a Redis error, skip Redis entirely until this wall-clock
# time so a down Redis does not add its socket timeout to EVERY request — traffic
# goes straight to the ClickHouse fallback during the cooldown.
_REDIS_CB_COOLDOWN_SECONDS = 5.0
_redis_down_until = 0.0


def _trip_redis_breaker() -> None:
    """Open the circuit for a short cooldown after a Redis failure."""
    global _redis_down_until
    _redis_down_until = time.time() + _REDIS_CB_COOLDOWN_SECONDS


def _get_redis(settings: Settings) -> Any:
    """Return a cached Redis client, or None when disabled / cooling-down / unavailable."""
    url = settings.redis_url.strip()
    if not url:
        return None  # cache disabled → ClickHouse read only
    if time.time() < _redis_down_until:
        return None  # circuit open → use the ClickHouse fallback
    client = _redis_clients.get(url)
    if client is None:
        try:
            import redis  # lazy: only imported when REDIS_URL is configured
        except ImportError as exc:
            logger.warning("REDIS_URL is set but the 'redis' package is not installed: %s", exc)
            _trip_redis_breaker()
            return None
        # from_url builds a lazy pool (no connection yet); the small timeouts bound
        # how long a down Redis can stall a request before we fall back.
        client = redis.Redis.from_url(
            url,
            socket_timeout=settings.redis_socket_timeout_seconds,
            socket_connect_timeout=settings.redis_socket_timeout_seconds,
            decode_responses=True,
        )
        _redis_clients[url] = client
    return client


def _redis_key(jti: str, settings: Settings) -> str:
    return f"{settings.employee_access_cache_prefix}{jti}"


def _redis_is_fresh(jti: str, settings: Settings) -> bool:
    """True only when a freshness marker for *jti* is present; any error → False."""
    client = _get_redis(settings)
    if client is None:
        return False
    try:
        return client.exists(_redis_key(jti, settings)) >= 1
    except Exception as exc:  # noqa: BLE001 — Redis is best-effort; degrade to ClickHouse.
        logger.warning("Redis freshness read failed (falling back to ClickHouse): %s", exc)
        _trip_redis_breaker()
        return False


def _redis_mark_fresh(jti: str, settings: Settings, ttl_seconds: int) -> None:
    """Best-effort set of the freshness marker with a TTL. Errors are swallowed."""
    client = _get_redis(settings)
    if client is None:
        return
    try:
        client.set(_redis_key(jti, settings), "1", ex=max(1, int(ttl_seconds)))
    except Exception as exc:  # noqa: BLE001 — Redis is best-effort.
        logger.warning("Redis freshness write failed (ignored): %s", exc)
        _trip_redis_breaker()


# ---------------------------------------------------------------------------
# Staleness read (authoritative fallback, via the shared mcp_user client)
# ---------------------------------------------------------------------------


def _freshness(jti: str, settings: Settings) -> tuple[bool, int]:
    """Return ``(has_rows, remaining_seconds)`` for *jti* using the ClickHouse clock.

    ``remaining_seconds = N - age`` is computed in SQL (``dateDiff`` against
    ``now()``) so there is no Python/ClickHouse timezone skew — it compares
    against the same clock that stamped ``UpdatedAt``.  ``remaining > 0`` means
    fresh.  ``count() == 0`` (table present, no rows for this JTI) is a genuine
    COLD JTI.  A MISSING table means the DBA migration was not applied — that
    fails closed loud (the app owns no DDL to bootstrap it).
    """
    n = int(settings.employee_access_refresh_seconds)
    sql = (
        "SELECT count() AS c, "
        f"toInt64({{n:UInt32}}) - dateDiff('second', max(UpdatedAt), now()) AS remaining "
        f"FROM {_qualified_table(settings)} WHERE JTI = {{jti:String}}"
    )
    try:
        result = _get_security_client(settings).query(
            sql,
            parameters={"jti": jti, "n": n},
            settings=readonly_settings(settings),
        )
    except ClickHouseError as exc:
        text = str(exc).lower()
        # Unknown table (code 60 / "doesn't exist") → the DBA-owned table + policy
        # were never provisioned. Fail closed loud with an actionable code rather
        # than silently returning zero rows.
        if "doesn't exist" in text or "unknown table" in text or "code: 60" in text:
            raise EmployeeAccessError(
                "The employee-access table is not provisioned — apply the DBA "
                "migration (table + row policy) before enabling employee-access.",
                code="EMPLOYEE_ACCESS_NOT_PROVISIONED",
            ) from exc
        # Any other ClickHouse error on the freshness read is an infra problem.
        raise EmployeeAccessError(
            "Could not read the employee-access table to check freshness."
        ) from exc
    rows = result.result_rows
    if not rows:
        return (False, 0)
    count = int(rows[0][0])
    if count <= 0:
        return (False, 0)
    remaining = int(rows[0][1]) if rows[0][1] is not None else 0
    return (True, remaining)


# ---------------------------------------------------------------------------
# External access API (/cl/eeaccess)
# ---------------------------------------------------------------------------


def _parse_codes(payload: Any) -> list[str]:
    """Extract a de-duplicated list of employee-code strings from the response.

    Accepts either a bare JSON array of codes or an object carrying the list
    under a conventional key.  Non-string / empty entries are dropped.  The
    ``/cl/eeaccess`` contract is "codes only, per-JTI"; this stays lenient about
    the exact envelope so a shape tweak on the API side does not require a code
    change here.
    """
    if isinstance(payload, dict):
        for key in ("employeeCodes", "employee_codes", "codes", "data", "result"):
            if key in payload:
                payload = payload[key]
                break
    if not isinstance(payload, list):
        raise ValueError("eeaccess response is not a JSON list of employee codes")
    seen: set[str] = set()
    codes: list[str] = []
    for item in payload:
        code = str(item).strip() if item is not None else ""
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _fetch_employee_codes(principal: Principal, settings: Settings) -> list[str]:
    """GET the employee codes the caller may access from ``/{system}/eeaccess``.

    Auth: forwards the caller's OWN JWT (the token this request was validated
    from) as a ``Bearer`` credential, so ``/cl/eeaccess`` authorizes exactly as
    the user and derives the JTI/client/proc/sub from the token itself — no
    identifying query params are needed.  The path segment is the ``system`` claim
    (e.g. ``cl``).  Response shape is handled leniently by ``_parse_codes``.
    """
    base = settings.eeaccess_base_url.strip().rstrip("/")
    if not base:
        raise EmployeeAccessError(
            "EEACCESS_BASE_URL is not configured but employee-access is enabled.",
            code="EMPLOYEE_ACCESS_MISCONFIGURED",
        )
    token = principal.token.strip()
    if not token:
        # HTTP transports always carry the raw token; its absence means we cannot
        # authenticate the fetch as the user — fail (cold handling decides loud/quiet).
        raise EmployeeAccessError(
            "No bearer token available to authorize the employee-access fetch.",
            code="EMPLOYEE_ACCESS_NO_TOKEN",
        )
    system = str(principal.claims.get("system") or "cl").strip() or "cl"
    url = f"{base}/{quote(system, safe='')}/eeaccess"

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=settings.eeaccess_timeout_seconds) as resp:
        raw = resp.read()
    payload = json.loads(raw.decode("utf-8"))
    return _parse_codes(payload)


# ---------------------------------------------------------------------------
# Refresh (rare: only when stale or cold)
# ---------------------------------------------------------------------------


def _write_pull(jti: str, codes: list[str], settings: Settings) -> None:
    """Write one atomic pull: a sentinel row + one row per code, all with a new pull_id."""
    # Wall-clock milliseconds: monotonic enough to order pulls for a single JTI.
    # A herd double-pull just yields two pull_ids; max() picks one and the loser's
    # rows age out via TTL (harmless — both reflect near-simultaneous API truth).
    pull_id = int(time.time() * 1000)

    # Sentinel FIRST so an empty access set still advances max(pull_id).
    data: list[list[Any]] = [[jti, _SENTINEL_CODE, pull_id]]
    data.extend([jti, code, pull_id] for code in codes)

    # ONE insert = one atomic pointer advance on single-node ClickHouse.
    # UpdatedAt is omitted so ClickHouse fills it with DEFAULT now() (the
    # server-clock value the freshness read compares against).  The cached client
    # is shared, so it is not closed here.
    _get_security_client(settings).insert(
        table=_TABLE,
        database=settings.security_database,
        data=data,
        column_names=["JTI", "EmployeeCode", "pull_id"],
    )
    logger.info(
        "employee-access pull written: jti=%s codes=%d pull_id=%d",
        jti,
        len(codes),
        pull_id,
    )


def _refresh(jti: str, principal: Principal, settings: Settings, is_cold: bool) -> None:
    """Fetch the current codes and write a new pull; fail-closed-loud only when cold."""
    try:
        codes = _fetch_employee_codes(principal, settings)
    except EmployeeAccessError:
        raise
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError) as exc:
        if is_cold:
            # No prior set to fall back on — refusing to run a query against an
            # unpopulated access table (which would silently return zero rows).
            logger.error("employee-access COLD fetch failed jti=%s: %s", jti, exc)
            raise EmployeeAccessError(
                "Could not establish your employee-access list (the access service is "
                "unavailable). No employee data can be returned until access is provisioned."
            ) from exc
        # Stale-but-present: prefer availability, serve the existing (stale) set,
        # and back off so a down /cl/eeaccess is not re-hit on every request.
        logger.warning(
            "employee-access refresh failed jti=%s; serving existing set: %s", jti, exc
        )
        _redis_mark_fresh(jti, settings, settings.employee_access_refresh_backoff_seconds)
        return

    try:
        _write_pull(jti, codes, settings)
    except ClickHouseError as exc:
        if is_cold:
            logger.error("employee-access COLD write failed jti=%s: %s", jti, exc)
            raise EmployeeAccessError(
                "Could not persist your employee-access list. No employee data can be "
                "returned until access is provisioned."
            ) from exc
        logger.warning(
            "employee-access refresh write failed jti=%s; serving existing set: %s", jti, exc
        )
        _redis_mark_fresh(jti, settings, settings.employee_access_refresh_backoff_seconds)
        return

    # Success: the new pull is authoritative for the full refresh window.
    _redis_mark_fresh(jti, settings, settings.employee_access_refresh_seconds)


# ---------------------------------------------------------------------------
# Public hook — called at the top of each data-read path
# ---------------------------------------------------------------------------


def ensure_employee_access_fresh(settings: Optional[Settings] = None) -> None:
    """Ensure the current caller's employee-access set is fresh before a data read.

    No-op when the feature is disabled or there is no authenticated principal
    (internal schema/health queries and the stdio/local-trust transport, which
    are not tenant-scoped).  Otherwise: check the Redis marker; on a miss fall
    back to the authoritative ClickHouse freshness read; only a stale-or-cold JTI
    pulls ``/cl/eeaccess`` and writes one pull.

    Raises:
        EmployeeAccessError: when a COLD JTI cannot be provisioned (fail closed).
    """
    if settings is None:
        settings = get_settings()
    if not settings.employee_access_enabled:
        return
    principal = get_current_principal()
    if principal is None:
        return

    jti = str(principal.claims.get(settings.employee_access_jti_claim) or "").strip()
    if not jti:
        # Defensive: auth already fails closed on the mapped tenant claim, but the
        # JTI is the row-policy key — never run tenant-scoped without it.
        raise EmployeeAccessError(
            "Authenticated token is missing the JTI claim required for row-level access.",
            code="EMPLOYEE_ACCESS_NO_JTI",
        )

    # 1. Redis fast path (best-effort): a present marker means fresh — done.
    if _redis_is_fresh(jti, settings):
        return

    # 2. Authoritative fallback — the cheap ClickHouse staleness read. Also the
    #    path taken whenever Redis is disabled, cooling-down, or a cache miss.
    has_rows, remaining = _freshness(jti, settings)
    if has_rows and remaining > 0:
        # Backfill the cache, capping the TTL at the ACTUAL remaining freshness so
        # revocation latency stays bounded by N even across a Redis backfill.
        _redis_mark_fresh(
            jti, settings, min(remaining, settings.employee_access_refresh_seconds)
        )
        return

    # 3. Stale or cold → pull and write (fail-closed-loud only when cold).
    _refresh(jti, principal, settings, is_cold=not has_rows)
