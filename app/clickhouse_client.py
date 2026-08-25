"""ClickHouse client wrapper.

Creates a single, connection-pooled clickhouse-connect client from application
config and exposes a thin execute() helper that:
  - Applies mandatory read-only session settings on every query.
  - Carries the per-tenant RLS identity settings in the SQL body's SETTINGS
    clause (as typed param_* binds) so they survive CHProxy, which strips the
    HTTP settings= channel clickhouse-connect would otherwise use.
  - Converts ClickHouse errors into clean HTTP 400 / 502 responses so that
    the LLM caller receives actionable error messages without stack traces.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any

import clickhouse_connect
from clickhouse_connect import common as cc_common
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError, DatabaseError
from fastapi import HTTPException

from app.config import Settings, get_settings
from app.principal import get_current_principal

logger = logging.getLogger(__name__)

# LOAD-BEARING for per-tenant isolation: clickhouse-connect validates every
# setting name against the server's known settings (system.settings) and, by
# default, REFUSES to transmit any it doesn't recognize — raising "Setting X is
# unknown or readonly" client-side, before the request is even sent.  Our
# per-tenant CUSTOM setting (e.g. paycom_client_code) is NOT in system.settings, so the
# default behaviour silently breaks row isolation.  'send' delegates validation
# to the server, so custom settings under the configured
# <custom_settings_prefixes> are passed through and applied.  This is a
# process-global clickhouse-connect setting; set once at import.
cc_common.set_setting("invalid_setting_action", "send")

# Pattern that matches host:port style strings in error messages, used to
# redact infrastructure details before forwarding errors to callers.
_HOST_PORT_RE = re.compile(r"\b[\w\-.]+:\d{2,5}\b")

# A plain ClickHouse identifier (setting name / bind-parameter name): a letter or
# underscore followed by letters, digits, or underscores.  Used to re-assert that
# a server-configured tenant-setting key is safe to inline into a SETTINGS clause.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _redact_connection_details(msg: str, settings: Settings) -> str:
    """Remove host/port/username from *msg* before returning it to the caller.

    We explicitly replace the configured host, port, and username with
    placeholders, then apply a broad host:port regex as a final sweep.
    """
    msg = msg.replace(settings.clickhouse_host, "<host>")
    msg = msg.replace(str(settings.clickhouse_port), "<port>")
    msg = msg.replace(settings.clickhouse_user, "<user>")
    # Passwords must never appear in exception strings, but replace defensively.
    if settings.clickhouse_password:
        msg = msg.replace(settings.clickhouse_password, "<redacted>")
    # Broad sweep: any remaining host:port pattern
    msg = _HOST_PORT_RE.sub("<host>:<port>", msg)
    return msg


def _build_client(settings: Settings) -> Client:
    """Construct a fresh clickhouse-connect Client from *settings* (no caching)."""
    logger.info(
        "Creating ClickHouse client: host=%s port=%s secure=%s user=%s",
        settings.clickhouse_host,
        settings.clickhouse_port,
        settings.clickhouse_secure,
        settings.clickhouse_user,
    )

    return clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_database,
        secure=settings.clickhouse_secure,
        # Verify TLS certificates by default; can be relaxed with a custom CA
        # by setting verify=False only in development environments.
        verify=settings.clickhouse_secure,
        # THREAD SAFETY (load-bearing): clickhouse-connect's sync client defaults
        # autogenerate_session_id=True, stamping the SAME session_id on every
        # query from this process-wide singleton. ClickHouse forbids concurrent
        # queries in one session, so the driver raises ProgrammingError ("Attempt
        # to execute concurrent queries within the same session") the moment two
        # requests share the client — which is the normal case here, since our
        # sync FastAPI routes and the MCP HTTP transport run in a threadpool.
        # Disabling session_id makes the query path stateless: with no session_id
        # the driver skips its _active_session guard entirely and concurrent
        # queries ride the thread-safe urllib3 connection pool independently.
        # This read-only API uses no session-scoped features, so we lose nothing.
        autogenerate_session_id=False,
    )


@lru_cache(maxsize=1)
def _cached_client() -> Client:
    """Process-wide singleton Client built from the cached settings.

    The cache key is the (empty) argument list, NOT the Settings object —
    pydantic Settings instances are unhashable, so lru_cache must never receive
    one as an argument.
    """
    return _build_client(get_settings())


def get_client(settings: Settings | None = None) -> Client:
    """Return the clickhouse-connect Client.

    Production code calls ``get_client()`` with no arguments and receives the
    process-wide cached singleton (clickhouse-connect pools connections
    internally).  Tests may pass an explicit *settings* to build a one-off,
    uncached client — this path deliberately bypasses the lru_cache so an
    unhashable Settings object is never used as a cache key.
    """
    if settings is not None:
        return _build_client(settings)
    return _cached_client()


def readonly_settings(settings: Settings) -> dict[str, Any]:
    """Build the ClickHouse session settings applied to every read-only query.

    This is the single source of truth for read-only session settings.  Every
    query path — user-submitted queries AND internal schema-discovery queries —
    must use this function so that all safety caps (readonly, execution time,
    result size) are consistently applied.

    NOTE: ``readonly`` is configurable (1 by default).  readonly=1 works fine
    with the per-tenant custom setting — the earlier blocker was clickhouse-connect
    rejecting the unknown setting client-side, fixed by invalid_setting_action=
    'send' at the top of this module, NOT a server-side readonly conflict.  The
    config knob is retained for unusual ClickHouse setups; these caps are
    authoritative regardless because we always send them ourselves.
    """
    ch_settings: dict[str, Any] = {
        "readonly": settings.clickhouse_readonly,
        "max_execution_time": settings.max_execution_time,
        "max_result_rows": settings.max_result_rows,
        "result_overflow_mode": "throw",
        "max_rows_to_read": settings.max_rows_to_read,
        # Pin ALIAS-FIRST name resolution (this is ClickHouse's default, 0).
        #
        # This is a COLUMN-SCOPE-ENFORCEMENT setting, not a performance knob. The
        # provenance extractor treats an unqualified name in WHERE / GROUP BY /
        # HAVING / ORDER BY that matches a declared SELECT-list alias as a
        # query-internal reference that reads no new column (sqlglot's alias
        # expansion, and the catalog-named case-(A) escape in
        # `sqlparse.provenance.extract_column_provenance`). That is sound ONLY
        # while ClickHouse resolves the alias before a same-named source column,
        # which is exactly what this setting governs.
        #
        # If a server default or user profile set it to 1 (source-column-first),
        # `SELECT lower(earn_code) AS status FROM accrual_events ORDER BY status`
        # would READ `accrual_events.status` while the extractor's USES set omits
        # it — an under-reported access, i.e. a scope-check bypass. Per-query
        # SETTINGS clauses are already denied by the statement gate, so sending it
        # ourselves on every read-only query closes the remaining environmental
        # hole (a server/profile-level flip we do not control).
        "prefer_column_name_to_alias": 0,
    }
    # Optional dedup-at-read for ReplacingMergeTree/CollapsingMergeTree tables.
    # Query-global: ClickHouse applies FINAL to every MergeTree-family table in
    # the query (no-op on system tables). Off by default; see config for caveats.
    if settings.clickhouse_select_final:
        ch_settings["final"] = 1
    # READ-YOUR-WRITES for the cluster scratch side-channel. When SCRATCH_CLUSTER
    # is set, materialize() writes scratch tables as ReplicatedMergeTree with a
    # quorum insert (insert_quorum='auto'); replication is otherwise ASYNCHRONOUS,
    # so a JOIN routed to a lagging replica could miss the just-written rows.
    # select_sequential_consistency=1 makes the SELECT wait until its replica has
    # applied the quorum insert, closing that window so the downstream JOIN always
    # sees the scratch rows it just materialized.
    #
    # TRADEOFF (why it is gated, not always-on): this is query-GLOBAL and applies
    # to every read, including plain warehouse queries. It is a no-op on
    # non-replicated tables, but on Replicated warehouse tables it makes the read
    # wait for replication — a latency cost we only accept in a cluster deployment
    # that has explicitly opted in via SCRATCH_CLUSTER. Single-node deployments
    # (the default) never pay it.
    if settings.scratch_cluster:
        ch_settings["select_sequential_consistency"] = 1
    return ch_settings


def tenant_settings(settings: Settings) -> dict[str, Any]:
    """Build per-tenant ClickHouse custom settings from the current principal.

    Reads the authenticated principal bound to the request context and maps each
    configured ``ch_setting -> jwt_claim`` (the CLICKHOUSE_TENANT_SETTINGS env
    object) into ``{ch_setting: claim_value}``.  ClickHouse row policies read
    these via ``getSetting('paycom_client_code')`` (and the proc-center /
    authenticated-user settings) to filter rows per tenant.

    Returns an empty dict when there is no principal (internal schema-discovery
    or health queries, or the local MCP stdio transport) — those run with the
    safety caps only and no tenant scoping.

    The claim's presence is already enforced at authentication time (auth_jwt
    fails closed on a missing tenant claim), so by the time we get here a present
    principal is guaranteed to carry every mapped claim.
    """
    principal = get_current_principal()
    if principal is None:
        return {}

    out: dict[str, Any] = {}
    for ch_key, claim_name in settings.clickhouse_tenant_settings.items():
        value = principal.claims.get(claim_name)
        if value is None:
            # Defensive: should be unreachable because auth fails closed, but we
            # never silently run a tenant-scoped query with a missing value.
            raise HTTPException(
                status_code=403,
                detail={
                    "error": f"Authenticated token is missing the '{claim_name}' claim.",
                    "code": "MISSING_TENANT_CLAIM",
                },
            )
        out[ch_key] = str(value)
    return out


def _append_tenant_settings_clause(
    sql: str,
    parameters: dict[str, Any] | None,
    tenant: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """Carry the per-tenant RLS settings in the SQL body's SETTINGS clause.

    WHY THIS EXISTS (CHProxy): clickhouse-connect transmits ``settings=`` as HTTP
    query parameters.  Our multi-node ClickHouse now sits behind CHProxy, which
    forwards the SQL body and any ``param_*`` HTTP parameters but STRIPS other
    incoming query parameters — including custom session settings.  The RLS row
    policies read the tenant identity via ``getSetting('paycom_client_code')``
    (and proc-center / authenticated-user), so those values MUST reach ClickHouse
    or every policy evaluates against an empty setting and the query returns no
    rows (or errors).  We therefore move them out of the stripped ``settings=``
    channel and into the SETTINGS clause of the SQL body, binding each value as a
    typed ``{name:String}`` query parameter — transported as a CHProxy-preserved
    ``param_*`` — rather than concatenating raw values into SQL.

    Only the CUSTOM ``paycom_*`` settings travel here: ClickHouse exempts custom
    settings (under ``<custom_settings_prefixes>``) from the readonly constraint,
    so a SETTINGS clause carrying them is accepted even when the ClickHouse user
    profile pins ``readonly=1`` (see config.py notes on CLICKHOUSE_READONLY).  The
    standard safety caps are NOT moved here — a query SETTINGS clause that changed
    a standard setting under a profile-enforced readonly=1 would be rejected — so
    they stay on the ``settings=`` channel (harmless if CHProxy strips them; they
    are meant to be enforced by the ClickHouse profile / CHProxy on that path).

    The setting VALUES originate only from the authenticated principal's JWT
    claims (see ``tenant_settings``); security.py already rejects any caller
    ``SETTINGS`` clause, so this appended clause is fully server-trusted.  The SQL
    reaching here is single-statement, comment-stripped and trailing-semicolon-free
    (user queries pass through validate_and_sanitize; internal queries are
    server-built), so a trailing SETTINGS clause is always well-formed.

    Returns the (sql, parameters) to hand to ``client.query``.  When *tenant* is
    empty (no principal — internal schema/health queries), the inputs are returned
    unchanged so those paths are entirely unaffected.
    """
    if not tenant:
        return sql, parameters

    # Copy so we never mutate a caller-owned parameters dict (e.g. a schema route
    # binding {dbs:Array(String)}); the RLS binds live alongside those.
    merged: dict[str, Any] = dict(parameters or {})
    assignments: list[str] = []
    for ch_key, value in tenant.items():
        # ch_key is server-configured and already validated to sit under the
        # custom-settings prefix, but it is inlined here as a bare setting name AND
        # reused as a bind-parameter name, so re-assert it is a plain identifier —
        # defence-in-depth against a malformed config key breaking the clause.
        if not _IDENTIFIER_RE.match(ch_key):
            raise HTTPException(
                status_code=500,
                detail={
                    "error": f"Invalid tenant setting name '{ch_key}'.",
                    "code": "INVALID_TENANT_SETTING",
                },
            )
        # Distinctive prefix so the bind name can never collide with a caller's
        # own parameter (schema routes use plain names like 'dbs' / 'table').
        param_name = f"rls_{ch_key}"
        merged[param_name] = value
        assignments.append(f"{ch_key} = {{{param_name}:String}}")

    clause = "SETTINGS " + ", ".join(assignments)
    return f"{sql}\n{clause}", merged


def execute_query(
    sql: str,
    settings: Settings | None = None,
    parameters: dict[str, Any] | None = None,
) -> tuple[list[str], list[list[Any]]]:
    """Execute *sql* with read-only settings and return (column_names, rows).

    Args:
        sql:        The SQL statement to execute.  Must have already passed
                    through security.validate_and_sanitize() for user-submitted
                    queries.  Internal schema queries may be passed directly.
        settings:   Application settings; defaults to the cached singleton.
        parameters: Optional bind parameters dict for {param:Type} placeholders
                    (used by schema routes to safely bind database/table names).

    Returns:
        A 2-tuple of (column_names, rows) where each row is a list of Python
        values.  Dates, decimals, etc. are serialised by clickhouse-connect to
        native Python types suitable for JSON serialisation.

    Raises:
        HTTPException(400): For query-level errors (syntax, unknown table, etc.)
                            so the LLM can self-correct.
        HTTPException(502): For connectivity / server-level errors.
    """
    if settings is None:
        settings = get_settings()

    # Use the cached singleton client (no-arg). Passing settings here would
    # build a fresh client on every query and (historically) crash because
    # lru_cache cannot hash a Settings object.
    client = get_client()
    # Transport for the per-tenant RLS identity settings (paycom_*), gated by
    # CLICKHOUSE_RLS_VIA_SQL_SETTINGS:
    #   ON  (default, CHProxy-safe): carry them in the SQL body's SETTINGS clause as
    #       CHProxy-preserved param_* binds — the settings= channel is stripped by
    #       CHProxy, so the identity values would never reach ClickHouse there. The
    #       safety caps stay on settings= (a standard setting changed under a
    #       profile-enforced readonly=1 would be rejected, and the caps are enforced
    #       by the ClickHouse profile / CHProxy on the proxied path).
    #   OFF (legacy, direct ClickHouse): merge them into the settings= dict alongside
    #       the safety caps — customs FIRST, caps LAST, so a misconfigured tenant
    #       mapping can never overwrite readonly / the row caps.
    # security.py already rejects any caller-supplied SETTINGS clause, so the only
    # code that sets paycom_* is this trusted path.
    tenant = tenant_settings(settings)
    if settings.clickhouse_rls_via_sql_settings:
        sql, parameters = _append_tenant_settings_clause(sql, parameters, tenant)
        ch_settings = readonly_settings(settings)
    else:
        ch_settings = {**tenant, **readonly_settings(settings)}

    try:
        result = client.query(sql, parameters=parameters, settings=ch_settings)
    except DatabaseError as exc:
        # DatabaseError covers syntax errors, unknown tables, type mismatches —
        # things the LLM can fix by rewriting the query.
        # Log the full detail server-side (safe; stays in logs, not the response).
        logger.warning(
            "ClickHouse query error host=%s port=%s db=%s: %s",
            settings.clickhouse_host,
            settings.clickhouse_port,
            settings.clickhouse_database,
            exc,
        )
        # Forward only the ClickHouse error message to the caller for
        # LLM self-correction.  The str() of a DatabaseError typically contains
        # the SQL error text without connection details, but we explicitly strip
        # any "host:port" patterns that may appear in the exception string to
        # avoid leaking infrastructure details.
        raw_msg = str(exc)
        safe_msg = _redact_connection_details(raw_msg, settings)
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"ClickHouse query error: {safe_msg}",
                "code": "CLICKHOUSE_QUERY_ERROR",
            },
        ) from exc
    except ClickHouseError as exc:
        # Broader ClickHouseError covers network / server problems.
        logger.error(
            "ClickHouse connectivity error host=%s port=%s: %s",
            settings.clickhouse_host,
            settings.clickhouse_port,
            exc,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": "ClickHouse is temporarily unavailable. Please try again later.",
                "code": "CLICKHOUSE_UNAVAILABLE",
            },
        ) from exc

    column_names: list[str] = result.column_names  # type: ignore[assignment]
    rows: list[list[Any]] = [list(row) for row in result.result_rows]
    return column_names, rows


def ping(settings: Settings | None = None) -> bool:
    """Return True if ClickHouse responds to a lightweight ping.

    Passes *settings* straight through to get_client: None (the production
    path, e.g. /health probes) resolves to the cached singleton client, while
    explicit settings build a one-off client (used by tests).  Resolving
    settings eagerly here would force every probe through _build_client,
    rebuilding the connection and re-logging on each call.

    Stale-socket resilience: the singleton client keeps a urllib3 pool of
    keep-alive sockets.  When ClickHouse (or an intermediary LB/proxy) closes an
    idle connection, the next probe reuses that dead socket and clickhouse-connect's
    ping() sees a RemoteDisconnected — it discards the broken connection and
    returns False.  A single retry therefore rides a fresh socket and succeeds,
    so a healthy-but-idle server no longer makes /health flap to "error".  Only
    the singleton path retries: an explicit *settings* builds a brand-new client
    every call (no pooled socket to go stale), so retrying there would just churn
    connections.
    """
    retry_stale = settings is None
    try:
        client = get_client(settings)
        if client.ping():
            return True
    except Exception:  # noqa: BLE001
        if not retry_stale:
            return False

    if not retry_stale:
        return False

    # Second attempt: the broken keep-alive socket has been evicted from the
    # pool, so this ping draws a fresh connection.
    try:
        return get_client().ping()
    except Exception:  # noqa: BLE001
        return False
