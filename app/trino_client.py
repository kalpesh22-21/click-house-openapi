"""Experimental read-only Trino adapter for the existing logical SQL interface.

The MCP continues to validate ClickHouse-dialect logical SQL. This adapter
transpiles only after validation and resolves physical sources using the YAML
catalog. It does NOT install database row policies or employee reporting views.
"""

from __future__ import annotations

import json
import re
from contextlib import suppress
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.optimizer.qualify_tables import qualify_tables
from sqlglot.optimizer.scope import traverse_scope

from app.config import Settings, get_settings
from app.errors import (
    ClickHouseQueryError,
    ClickHouseUnavailableError,
    QueryValidationError,
)
from app.principal import get_current_principal
from app.semantic_catalog.loader import DEFAULT_DATABASE, get_semantic_catalog
from app.shard_routing import resolve_mysql_shard, validate_identifier


def _close(cursor, conn) -> None:
    # Cleanup must not replace the original query error. Cursor.close cancels
    # unfinished queries, including requests stopped by local result limits.
    for resource in (cursor, conn):
        if resource is not None:
            with suppress(Exception):
                resource.close()


def request_identity(settings: Settings) -> tuple[str, str, str]:
    """Values must come from verified claims, never SQL or client headers."""
    principal = get_current_principal()
    if principal is None:
        raise QueryValidationError(
            "Trino requires an authenticated principal.", "TRINO_IDENTITY_REQUIRED"
        )
    values = []
    for claim in (
        settings.trino_proc_center_claim,
        settings.trino_client_code_claim,
        settings.trino_username_claim,
    ):
        value = principal.claims.get(claim)
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 128
            or any(c in value for c in "|\r\n\x00")
            or not value.isprintable()
        ):
            raise QueryValidationError(
                "Missing or invalid Trino identity claim.", "TRINO_IDENTITY_REQUIRED"
            )
        values.append(value)
    return values[0], values[1], values[2]


def rewrite_query(sql: str, settings: Settings) -> str:
    """Route base relations, preserving CTE names, aliases and logical columns.

    Physical catalog names and scratch relations are intentionally inaccessible
    to caller SQL. SELECT/UNION and plain EXPLAIN SELECT are the initial surface.
    """
    proc_center, client_code, _ = request_identity(settings)
    explain = bool(re.match(r"^\s*EXPLAIN\s+", sql, re.IGNORECASE))
    inner = (
        re.sub(r"^\s*EXPLAIN\s+", "", sql, count=1, flags=re.IGNORECASE)
        if explain
        else sql
    )
    try:
        statements = sqlglot.parse(
            inner, read="clickhouse", error_level=ErrorLevel.RAISE
        )
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise QueryValidationError(
                "Trino supports SELECT/WITH and plain EXPLAIN SELECT only.",
                "TRINO_UNSUPPORTED_SQL",
            )
        tree = qualify_tables(statements[0], db=DEFAULT_DATABASE, dialect="clickhouse")
        catalog = get_semantic_catalog()
        allowed = settings.allowed_databases_list()
        shard = None
        routed = set()
        for scope in traverse_scope(tree):
            for _, source in scope.selected_sources.values():
                if not isinstance(source, exp.Table):
                    continue  # CTE or derived relation; its own scope is visited.
                if source.catalog or not isinstance(source.this, exp.Identifier):
                    raise QueryValidationError(
                        "Physical catalogs and table functions are not accepted.",
                        "TRINO_UNSUPPORTED_SQL",
                    )
                key = f"{source.db}.{source.name}"
                if key not in catalog or (
                    allowed is not None and source.db not in allowed
                ):
                    raise QueryValidationError(
                        "Trino accepts only allowed logical catalog tables; scratch is not supported.",
                        "TABLE_NOT_ALLOWED",
                    )
                entry = catalog[key]
                if entry.get("source", "clickhouse") == "mysql":
                    if shard is None:
                        shard = resolve_mysql_shard(proc_center, client_code, settings)
                    target_catalog = settings.trino_mysql_view_catalog or shard
                    target_schema = (
                        shard
                        if settings.trino_mysql_view_catalog
                        else settings.trino_mysql_schema
                    )
                else:
                    target_catalog = settings.trino_clickhouse_catalog
                    target_schema = source.db
                source.set(
                    "catalog",
                    exp.to_identifier(validate_identifier(target_catalog), quoted=True),
                )
                source.set(
                    "db",
                    exp.to_identifier(validate_identifier(target_schema), quoted=True),
                )
                routed.add(id(source))
        # Catch table-function/other relation forms that scope traversal omitted.
        for table in tree.find_all(exp.Table):
            if id(table) not in routed and (
                table.db or table.catalog or not isinstance(table.this, exp.Identifier)
            ):
                raise QueryValidationError(
                    "Unsupported Trino relation.", "TRINO_UNSUPPORTED_SQL"
                )
        for count in tree.find_all(exp.Count):
            if count.this is None:
                count.set("this", exp.Star())
        rendered = tree.sql(dialect="trino", unsupported_level=ErrorLevel.RAISE)
        return f"EXPLAIN {rendered}" if explain else rendered
    except SqlglotError as exc:
        raise QueryValidationError(
            "SQL cannot be safely translated to Trino.", "TRINO_UNSUPPORTED_SQL"
        ) from exc


def _connect(settings: Settings, user: str):
    # Lazy import: existing ClickHouse-only installations keep working.
    from trino.auth import BasicAuthentication
    from trino.dbapi import connect

    if settings.trino_password and settings.trino_http_scheme != "https":
        raise QueryValidationError(
            "Trino password authentication requires HTTPS.", "TRINO_TLS_REQUIRED"
        )
    auth = (
        BasicAuthentication(settings.trino_user, settings.trino_password)
        if settings.trino_password
        else None
    )
    return connect(
        host=settings.trino_host,
        port=settings.trino_port,
        http_scheme=settings.trino_http_scheme,
        user=user,
        auth=auth,
        source="clickhouse-api-mcp",
        request_timeout=settings.trino_request_timeout,
        max_attempts=1,
        session_properties={"query_max_run_time": f"{settings.max_execution_time}s"},
    )


def execute_query(
    sql: str, settings: Settings | None = None, parameters: dict[str, Any] | None = None
) -> tuple[list[str], list[list[Any]]]:
    settings = settings or get_settings()
    if not settings.trino_security_ready:
        raise QueryValidationError(
            "Provision Trino tenant/employee policies or secured views before setting TRINO_SECURITY_READY=true. "
            "ClickHouse JWT settings are not forwarded by this adapter.",
            "TRINO_SECURITY_NOT_CONFIGURED",
        )
    if parameters:
        raise QueryValidationError(
            "Internal ClickHouse parameter syntax cannot execute in Trino.",
            "TRINO_UNSUPPORTED_SQL",
        )
    identity = request_identity(settings)
    physical_sql = rewrite_query(sql, settings)
    conn = cursor = None
    try:
        conn = _connect(settings, "|".join(identity))
        cursor = conn.cursor()
        cursor.execute(physical_sql)
        rows = []
        size = 0
        while batch := cursor.fetchmany(
            min(1000, settings.max_result_rows + 1 - len(rows))
        ):
            rows.extend([list(row) for row in batch])
            size += len(
                json.dumps(batch, default=str, ensure_ascii=False).encode("utf-8")
            )
            if len(rows) > settings.max_result_rows or size > settings.max_result_bytes:
                raise QueryValidationError(
                    "Trino result exceeded the configured result limit.",
                    "TRINO_RESULT_LIMIT",
                )
        return [col[0] for col in cursor.description], rows
    except QueryValidationError:
        raise
    except Exception as exc:
        # Do not echo connector errors: they can contain SQL, identities and credentials.
        from trino.exceptions import TrinoUserError

        if isinstance(exc, TrinoUserError):
            raise ClickHouseQueryError(
                "Trino rejected the query. Check the logical schema and supported SQL.",
                "TRINO_QUERY_ERROR",
            ) from exc
        raise ClickHouseUnavailableError(
            "Trino query execution failed.", "TRINO_UNAVAILABLE"
        ) from exc
    finally:
        _close(cursor, conn)


def ping() -> bool:
    """Connectivity only; data access is independently gated by security_ready."""
    settings = get_settings()
    conn = cursor = None
    try:
        conn = _connect(settings, settings.trino_user)
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        return cursor.fetchone() == [1]
    except Exception:  # noqa: BLE001 — connectivity probes must return a boolean
        return False
    finally:
        _close(cursor, conn)
