"""ClickHouse client wrapper.

Creates a single, connection-pooled clickhouse-connect client from application
config and exposes a thin execute() helper that:
  - Applies mandatory read-only session settings on every query.
  - Converts ClickHouse errors into clean HTTP 400 / 502 responses so that
    the LLM caller receives actionable error messages without stack traces.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError, DatabaseError
from fastapi import HTTPException

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Pattern that matches host:port style strings in error messages, used to
# redact infrastructure details before forwarding errors to callers.
_HOST_PORT_RE = re.compile(r"\b[\w\-.]+:\d{2,5}\b")


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
    """
    return {
        "readonly": 1,
        "max_execution_time": settings.max_execution_time,
        "max_result_rows": settings.max_result_rows,
        "result_overflow_mode": "throw",
        "max_rows_to_read": settings.max_rows_to_read,
    }


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
    ch_settings = readonly_settings(settings)

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
    """
    try:
        client = get_client(settings)
        return client.ping()
    except Exception:  # noqa: BLE001
        return False
