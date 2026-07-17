"""Business logic layer — single code path shared by REST and MCP transports.

All six operations (list_databases, list_tables, get_table_schema, sample_rows,
run_query, explain_query) live here.  Neither the FastAPI routers nor the MCP
server should contain business logic — they are thin transport adapters that
call these functions and translate domain errors into transport-specific error
responses.

Key design decisions:
- No FastAPI or MCP imports.  Domain errors use app.errors, not HTTPException.
- All paths go through execute_query(), which applies readonly_settings() on
  every query, ensuring safety caps are applied regardless of transport.
- validate_and_sanitize() is called for every user-supplied SQL (run_query,
  explain_query).  Its HTTPException is caught here and re-raised as a
  transport-agnostic QueryValidationError.
- ALLOWED_DATABASES allowlist enforcement raises DatabaseNotAllowedError, not
  HTTPException, so the MCP transport can map it to a clear tool error.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import HTTPException

from app.catalog import get_catalog_schema
from app.clickhouse_client import execute_query
from app.config import Settings, get_settings
from app.employee_access import ensure_employee_access_fresh
from app.errors import (
    ClickHouseQueryError,
    ClickHouseUnavailableError,
    ColumnScopeError,
    DatabaseNotAllowedError,
    ParseFailedError,
    QueryValidationError,
    TableNotFoundError,
)
from app.principal import get_current_scope, get_current_session_id
from app.security import validate_and_sanitize
from app.semantic_catalog import (
    build_table_schema_response,
    get_catalog_sha,
    get_semantic_catalog,
)
from app.sqlparse import (
    ProvenanceExtractionError,
    ScratchSessionError,
    extract_column_provenance,
    scratch_table_belongs_to_session,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Matches a trailing LIMIT clause with an optional OFFSET clause, e.g.:
#   "LIMIT 100"  or  "LIMIT 100 OFFSET 20"
# When a caller-supplied limit overrides the query, only the LIMIT value is
# replaced; the OFFSET is preserved so paging queries are not silently broken.
# The max_result_rows setting acts as a backstop regardless of what LIMIT remains.
_LIMIT_TAIL_RE = re.compile(r"\bLIMIT\s+\d+(\s+OFFSET\s+\d+)?\s*$", re.IGNORECASE)


def _check_database_allowed(database: str, settings: Settings) -> None:
    """Raise DatabaseNotAllowedError if *database* is not in the allowlist."""
    allowed = settings.allowed_databases_list()
    if allowed is not None and database not in allowed:
        raise DatabaseNotAllowedError(
            message=(
                f"Access to database '{database}' is not permitted. "
                f"Allowed databases: {', '.join(allowed)}"
            ),
            code="DATABASE_NOT_ALLOWED",
        )


def _execute(
    sql: str,
    settings: Settings,
    parameters: dict[str, Any] | None = None,
) -> tuple[list[str], list[list[Any]]]:
    """Thin wrapper around execute_query that translates HTTPExceptions to domain errors.

    execute_query() currently raises HTTPException(400) for query errors and
    HTTPException(502) for connectivity errors.  We re-raise those as
    transport-agnostic domain errors so the MCP layer never sees HTTPException.
    """
    try:
        return execute_query(sql, settings, parameters)
    except HTTPException as exc:
        if exc.status_code == 502:
            raise ClickHouseUnavailableError(
                message=exc.detail.get("error", "ClickHouse unavailable"),
                code=exc.detail.get("code", "CLICKHOUSE_UNAVAILABLE"),
            ) from exc
        # 400 — query error
        raise ClickHouseQueryError(
            message=exc.detail.get("error", "ClickHouse query error"),
            code=exc.detail.get("code", "CLICKHOUSE_QUERY_ERROR"),
        ) from exc


def _compact_result(
    columns: list[str],
    rows: list[list[Any]],
    max_response_rows: int,
    truncated_override: bool | None = None,
) -> dict[str, Any]:
    """Return the compact {columns, rows, row_count, truncated} shape."""
    truncated = len(rows) > max_response_rows
    if truncated:
        rows = rows[:max_response_rows]
    if truncated_override is not None:
        truncated = truncated_override
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------


def list_databases(settings: Settings | None = None) -> list[dict[str, str]]:
    """Return all databases accessible through this API.

    Filters results against the ALLOWED_DATABASES allowlist so only
    permitted databases are returned.

    Returns a list of dicts, each with a single 'name' key.
    """
    if settings is None:
        settings = get_settings()

    _, rows = _execute("SELECT name FROM system.databases ORDER BY name", settings)

    allowed = settings.allowed_databases_list()
    return [
        {"name": name}
        for (name,) in rows
        if allowed is None or name in allowed
    ]


def list_tables(
    database: str,
    settings: Settings | None = None,
) -> list[dict[str, str]]:
    """Return all tables in *database*.

    Scratch-table isolation (D64): when *database* is the scratch database, the
    result is filtered to ONLY the caller's own scratch tables (those whose name
    identifies the current session as owner, via the shared
    ``scratch_table_belongs_to_session`` D64 parser). This closes the metadata
    leak where listTables('scratch') returned every session's scratch tables.
    FAIL-CLOSED: if no session is bound (session_id is None), the scratch listing
    is EMPTY — a session-less caller can never prove ownership, so nothing leaks.
    Non-scratch databases are unaffected (full list).

    Raises:
        DatabaseNotAllowedError: if *database* is not in the allowlist.
    """
    if settings is None:
        settings = get_settings()

    _check_database_allowed(database, settings)

    sql = (
        "SELECT database, name, engine "
        "FROM system.tables "
        "WHERE database = {db:String} "
        "ORDER BY name"
    )
    _, rows = _execute(sql, settings, parameters={"db": database})

    tables = [
        {"database": row[0], "name": row[1], "engine": row[2]}
        for row in rows
    ]

    if database == settings.scratch_database:
        # Session-gate the scratch database: keep only tables owned by the caller.
        # scratch_table_belongs_to_session fails closed on a None/empty session_id
        # and on any name not owned exactly by session_id, so a session-less caller
        # gets an empty list and no foreign scratch table ever appears.
        session_id = get_current_session_id()
        tables = [
            t for t in tables
            if scratch_table_belongs_to_session(t["name"], session_id)
        ]

    return tables


def get_table_schema(
    database: str,
    table: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Return the merged, scope-filtered column schema for *database*.*table* (D83/D84).

    Pipeline (mcp-overlay-design.md §1): introspect (unchanged system.columns
    query) -> merge with the Semantic Catalog overlay (case-sensitive exact
    column-name join, D70) -> scope-filter the merged result (skipped when
    `current_scope` is None [stdio/local-trust] or empty [allow-all, D80b]).

    Returns the design §1.2 shape: database, table, catalogued, description,
    grain, temporal, primary_key, join_keys, columns, measures, rules,
    ambiguities, catalog_sha. An uncatalogued table returns catalogued=False
    with all catalog-derived fields null and columns carrying introspection
    fields only (design §1.3) — still scope-filtered.

    Scratch-table isolation (D64): when *database* is the scratch database, the
    requested table must belong to the caller's session (same owning-session check
    run_query/sample_rows apply, via the shared ``scratch_table_belongs_to_session``
    D64 parser). A foreign — or session-less — scratch table's schema is NEVER
    returned; instead the SAME violation run_query raises is surfaced
    (SCRATCH_SESSION_VIOLATION), so the MCP reports it identically. Non-scratch
    databases are unaffected.

    Raises:
        DatabaseNotAllowedError: if *database* is not in the allowlist.
        ColumnScopeError:        SCRATCH_SESSION_VIOLATION if *database* is the
                                  scratch db and *table* does not belong to the
                                  current session (or no session is bound).
        TableNotFoundError:      if the table has no columns (does not exist).
    """
    if settings is None:
        settings = get_settings()

    _check_database_allowed(database, settings)

    if database == settings.scratch_database:
        # Session-gate the scratch database (D64): only the owning session may read
        # a scratch table's schema. Fails closed on a None/empty session and on any
        # foreign/malformed name — identical semantics to the run_query read gate.
        session_id = get_current_session_id()
        if not scratch_table_belongs_to_session(table, session_id):
            logger.error(
                "get_table_schema: scratch session violation code=SCRATCH_SESSION_VIOLATION"
            )
            raise ColumnScopeError(
                message=(
                    "Schema requested for a scratch table that does not belong to "
                    "this session."
                ),
                code="SCRATCH_SESSION_VIOLATION",
            )

    sql = (
        "SELECT name, type, comment "
        "FROM system.columns "
        "WHERE database = {db:String} AND table = {tbl:String} "
        "ORDER BY position"
    )
    _, rows = _execute(sql, settings, parameters={"db": database, "tbl": table})

    if not rows:
        raise TableNotFoundError(
            message=(
                f"Table '{database}.{table}' not found or has no columns. "
                "Check the database and table names with listTables."
            ),
            code="TABLE_NOT_FOUND",
        )

    introspected_columns = [
        {"name": row[0], "type": row[1], "comment": row[2] or ""}
        for row in rows
    ]

    scope = get_current_scope()
    # The introspected universe (real system.columns data for every table) is
    # only needed to resolve cross-table free-text references inside
    # rules[]/ambiguities[] when scope-filtering is actually active (FIX 1,
    # security review) — skip the extra query entirely when enforcement is a
    # no-op (scope is None or the empty/allow-all frozenset).
    introspected_schema = get_catalog_schema() if scope else None
    response = build_table_schema_response(
        database=database,
        table=table,
        introspected_columns=introspected_columns,
        catalog=get_semantic_catalog(),
        scope=scope,
        introspected_schema=introspected_schema,
    )
    response["catalog_sha"] = get_catalog_sha()
    return response


def sample_rows(
    database: str,
    table: str,
    limit: int = 5,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Return up to *limit* rows from *database*.*table*.

    *limit* is capped at 50 regardless of the caller's value.

    Returns the compact {columns, rows, row_count, truncated=False} shape.

    Column-scope enforcement (D83, matching runQuery's SELECT * treatment,
    D69/OQ-2): sampleRows is effectively `SELECT * LIMIT n`, so the exact same
    provenance-extraction + scope-allowlist check `run_query` applies is reused
    here, unmodified — the query is REJECTED (never partially projected) if any
    column it would return is outside `column_scope`. If the table can't be
    enumerated (not in the provenance catalog), extraction fails closed and the
    sample is rejected (same fail-closed rule SELECT * gets in runQuery).
    Skipped entirely when scope is None (stdio/local-trust) or empty
    (allow-all, D80b) — identical three-state model as run_query.

    Raises:
        DatabaseNotAllowedError: if *database* is not in the allowlist.
        ColumnScopeError:        if any column of the table is outside scope
                                  (COLUMN_SCOPE_VIOLATION) or the table can't be
                                  enumerated under scope enforcement.
        ParseFailedError:        if column provenance can't be extracted
                                  (PARSE_FAILED_CLOSED) — the sample is rejected,
                                  never executed.
    """
    if settings is None:
        settings = get_settings()

    _check_database_allowed(database, settings)

    # Cap at 50 regardless of caller input.
    effective_limit = max(1, min(limit, 50))

    # Database and table names are allowlist-checked above; use backtick quoting
    # to handle names with special characters.
    safe_db = database.replace("`", "``")
    safe_table = table.replace("`", "``")
    sql = f"SELECT * FROM `{safe_db}`.`{safe_table}` LIMIT {effective_limit}"

    # ---------------------------------------------------------------------------
    # Column-scope enforcement (D83) — reuses run_query's exact enforcement path.
    # ---------------------------------------------------------------------------
    scope = get_current_scope()
    session_id = get_current_session_id()

    if scope is not None:
        catalog = get_catalog_schema()

        try:
            uses = extract_column_provenance(sql, catalog, session_id=session_id)
        except ScratchSessionError as exc:
            logger.error(
                "sample_rows: scratch session violation code=SCRATCH_SESSION_VIOLATION"
            )
            raise ColumnScopeError(
                message="Table sampled is a scratch table that does not belong to this session.",
                code="SCRATCH_SESSION_VIOLATION",
            ) from exc
        except ProvenanceExtractionError as exc:
            logger.error(
                "sample_rows: provenance extraction failed code=PARSE_FAILED_CLOSED"
            )
            raise ParseFailedError(
                message=(
                    "Column provenance could not be extracted for this table "
                    "(it may not be in the catalog). Sample rejected — fail-closed."
                ),
                code="PARSE_FAILED_CLOSED",
            ) from exc

        if scope:
            forbidden = {
                f"{db_tbl}.{col}"
                for db_tbl, col in uses
                if not db_tbl.startswith("scratch.")
                and f"{db_tbl}.{col}" not in scope
            }

            if forbidden:
                logger.warning(
                    "sample_rows: column scope violation forbidden_count=%d code=COLUMN_SCOPE_VIOLATION",
                    len(forbidden),
                )
                # Column NAMES are catalog metadata (not PII / cell values, D25),
                # so it is safe to name the out-of-scope columns here — this is
                # what lets the model see WHICH columns it lacks.
                raise ColumnScopeError(
                    message=(
                        "This table has columns outside your permitted scope: "
                        f"{', '.join(sorted(forbidden))}. "
                        "sampleRows cannot partially project columns — request "
                        "access to those columns, or use runQuery with an "
                        "explicit column list within your scope."
                    ),
                    code="COLUMN_SCOPE_VIOLATION",
                )
    # ---------------------------------------------------------------------------

    # Refresh the caller's employee row-policy set if stale/cold (no-op when the
    # feature is disabled or there is no principal). Fail-closed-loud on a cold JTI.
    ensure_employee_access_fresh(settings)

    columns, rows = _execute(sql, settings)

    return _compact_result(columns, rows, settings.max_response_rows, truncated_override=False)


def run_query(
    sql: str,
    limit: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Validate, optionally limit, and execute *sql*.

    SQL is passed through validate_and_sanitize() before execution.  An
    explicit *limit* overrides the injected default (capped at max_response_rows).

    Returns the compact {columns, rows, row_count, truncated} shape.

    Raises:
        QueryValidationError:    if SQL fails the security guardrails.
        ClickHouseQueryError:    if ClickHouse returns a query-level error.
        ClickHouseUnavailableError: if ClickHouse is unreachable.
    """
    if settings is None:
        settings = get_settings()

    try:
        clean_sql = validate_and_sanitize(sql, settings.default_limit)
    except HTTPException as exc:
        raise QueryValidationError(
            message=exc.detail.get("error", "Query validation failed"),
            code=exc.detail.get("code", "QUERY_VALIDATION_ERROR"),
        ) from exc

    # ---------------------------------------------------------------------------
    # Column-scope and scratch-isolation enforcement (D57, D63, D64)
    # ---------------------------------------------------------------------------
    scope = get_current_scope()
    session_id = get_current_session_id()

    if scope is not None:
        # Scope is set (HTTP/JWT transport) — enforce column-level access control.
        catalog = get_catalog_schema()

        try:
            uses = extract_column_provenance(clean_sql, catalog, session_id=session_id)
        except ScratchSessionError as exc:
            # Scratch table belongs to a different session (D64).
            logger.error(
                "run_query: scratch session violation code=SCRATCH_SESSION_VIOLATION"
            )
            raise ColumnScopeError(
                message="Query accesses a scratch table that does not belong to this session.",
                code="SCRATCH_SESSION_VIOLATION",
            ) from exc
        except ProvenanceExtractionError as exc:
            # Parse/qualify failed — fail-closed (D63): never execute.
            logger.error(
                "run_query: provenance extraction failed code=PARSE_FAILED_CLOSED"
            )
            raise ParseFailedError(
                message=(
                    "Column provenance could not be extracted from this query. "
                    "Use explainQuery to diagnose, then resubmit."
                ),
                code="PARSE_FAILED_CLOSED",
            ) from exc

        # Empty frozenset == ALLOW-ALL: skip the column-allowlist check entirely.
        # Only a non-empty scope enforces the column allowlist (D57).
        # Scratch columns (database prefix == "scratch") are session-gated, not
        # scope-gated (D69/OQ-4) — they are intentionally excluded regardless.
        if scope:
            forbidden = {
                f"{db_tbl}.{col}"
                for db_tbl, col in uses
                if not db_tbl.startswith("scratch.")
                and f"{db_tbl}.{col}" not in scope
            }

            if forbidden:
                # Log only the count to keep server logs terse; the column
                # NAMES themselves are catalog metadata (surfaced to the model
                # in the error message below), not a secret.
                logger.warning(
                    "run_query: column scope violation forbidden_count=%d code=COLUMN_SCOPE_VIOLATION",
                    len(forbidden),
                )
                # Column NAMES are catalog metadata (not PII / cell values, D25),
                # so it is safe to name the out-of-scope columns here — this is
                # what lets the model see WHICH columns it lacks.
                raise ColumnScopeError(
                    message=(
                        "This query needs access to columns outside your "
                        f"permitted scope: {', '.join(sorted(forbidden))}. "
                        "You do not have access to those columns — remove them "
                        "from the query, or ask the user to grant access."
                    ),
                    code="COLUMN_SCOPE_VIOLATION",
                )
    # ---------------------------------------------------------------------------

    # Apply caller-supplied limit override.
    # The substitution pattern preserves a trailing OFFSET clause (captured in
    # group 1) so that paging queries are not silently broken.
    if limit is not None:
        effective_limit = min(limit, settings.max_response_rows)
        clean_sql = _LIMIT_TAIL_RE.sub(
            lambda m: f"LIMIT {effective_limit}{m.group(1) or ''}",
            clean_sql,
        )

    # Refresh the caller's employee row-policy set if stale/cold (no-op when the
    # feature is disabled or there is no principal). Fail-closed-loud on a cold JTI.
    ensure_employee_access_fresh(settings)

    columns, rows = _execute(clean_sql, settings)

    return _compact_result(columns, rows, settings.max_response_rows)


def explain_query(
    sql: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Wrap *sql* in EXPLAIN and return the query plan.

    The inner SQL is still validated by the guardrails.  truncated is always
    False because EXPLAIN output is tiny.

    Returns the compact {columns, rows, row_count, truncated=False} shape.

    Raises:
        QueryValidationError:    if the inner SQL fails the security guardrails.
        ClickHouseQueryError:    if ClickHouse returns a query-level error.
        ClickHouseUnavailableError: if ClickHouse is unreachable.
    """
    if settings is None:
        settings = get_settings()

    try:
        inner_sql = validate_and_sanitize(sql, settings.default_limit)
    except HTTPException as exc:
        raise QueryValidationError(
            message=exc.detail.get("error", "Query validation failed"),
            code=exc.detail.get("code", "QUERY_VALIDATION_ERROR"),
        ) from exc

    explain_sql = f"EXPLAIN {inner_sql}"
    columns, rows = _execute(explain_sql, settings)

    return _compact_result(columns, rows, settings.max_response_rows, truncated_override=False)
