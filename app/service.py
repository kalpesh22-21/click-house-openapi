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
    CartesianJoinError,
    ClickHouseQueryError,
    ClickHouseUnavailableError,
    ColumnScopeError,
    DatabaseNotAllowedError,
    ParseFailedError,
    QueryValidationError,
    TableNotFoundError,
)
from app.principal import get_current_scope, get_current_session_id
from app.security import statement_supports_provenance, validate_and_sanitize
from app.semantic_catalog import (
    build_table_schema_response,
    get_catalog_sha,
    get_semantic_catalog,
    hidden_columns_for,
)
from app.sqlparse import (
    CartesianJoinForbiddenError,
    ProvenanceExtractionError,
    ScratchSessionError,
    extract_column_provenance,
    reject_cartesian_joins,
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


def _references_scratch_db(sql: str, scratch_db: str) -> bool:
    """Fail-closed-safe over-approximation: does *sql* reference the scratch DB?

    Any REAL reference to a scratch table (``scratch.<tbl>``, the backtick-quoted
    ``\\`scratch\\`.\\`<tbl>\\``, or the double-quoted ``"scratch"."<tbl>"``)
    REQUIRES the literal scratch-database identifier to appear as a token in the
    SQL. We replace identifier quote characters with WHITESPACE (NOT empty string)
    and then search case-insensitively for the scratch_db name as a word-boundary
    token.

    Why replace-with-space, not delete: deleting the quote FUSES the preceding
    keyword with the identifier — ``FROM"scratch"`` would collapse to
    ``FROMscratch``, destroying the left word boundary so ``\\bscratch\\b`` no
    longer matches (a verified false-negative bypass on the REST path). A real
    scratch reference always has whitespace OR a quote between ``FROM``/``JOIN`` and
    the identifier (``FROMscratch`` with neither is a single invalid identifier that
    cannot reference the DB), so substituting a space for every quote GUARANTEES a
    left boundary and kills that false-negative class.

    This is DELIBERATELY an over-approximation (fail-closed-safe): it MAY return
    True spuriously — e.g. a column or alias literally named ``scratch`` — which
    only causes the provenance parse to run (a safe, fail-closed cost that never
    grants access). It must NEVER return False for a query that actually references
    the scratch DB, because a false negative would let a scratch reference skip the
    session gate entirely (the H3 fail-open). When in doubt it returns True.

    NOT self-sufficient: this pre-check relies on upstream ``validate_and_sanitize``
    to strip comments, reject multi-statement input, and deny table functions —
    so a comment-split token (``scr/**/atch``) or a table-function reference to the
    scratch DB (``merge('scratch', ...)``) is handled by that layer / by the
    provenance parse itself, not by this token scan.
    """
    # Replace identifier quoting with a SPACE (preserving word boundaries) so a
    # backtick-/double-quoted, no-space `"scratch"` still exposes a `\bscratch\b`
    # token to the search below. See docstring — delete-the-quote fused the token
    # onto the preceding keyword and re-opened the H3 bypass.
    stripped = sql.replace("`", " ").replace('"', " ")
    pattern = re.compile(rf"\b{re.escape(scratch_db)}\b", re.IGNORECASE)
    return pattern.search(stripped) is not None


def _forbidden_out_of_scope_columns(
    uses: frozenset[tuple[str, str]],
    scope: frozenset[str],
) -> set[str]:
    """Return the qualified columns in *uses* that are outside *scope*.

    Shared by run_query and sample_rows so both apply the SAME rule (D57/D83):

      - scratch columns (db prefix ``scratch.``) are session-gated, not
        scope-gated (D69/OQ-4) — excluded here.
      - ``mcp_projection.hidden_columns`` (RLS physical columns like client_code /
        proc_center, and the labor_allocation ``version``) are NEVER required in
        scope (A-H1): they are stripped from every model-facing surface, so a
        ``SELECT *`` that expands to include them must not raise a spurious
        COLUMN_SCOPE_VIOLATION. The hidden set is resolved from the SAME semantic
        catalog the getTableSchema overlay uses (`hidden_columns_for`).
      - everything else must be present in *scope* or it is forbidden.
    """
    catalog = get_semantic_catalog()
    forbidden: set[str] = set()
    for db_tbl, col in uses:
        if db_tbl.startswith("scratch."):
            continue
        if col in hidden_columns_for(catalog, db_tbl):
            continue
        if f"{db_tbl}.{col}" not in scope:
            forbidden.add(f"{db_tbl}.{col}")
    return forbidden


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

    Hidden-column projection (A-H1): sampleRows never emits a column the
    getTableSchema overlay hides. Instead of `SELECT *`, it introspects the
    table's columns and projects the VISIBLE set (introspected columns MINUS
    `mcp_projection.hidden_columns` — the RLS physical columns client_code /
    proc_center, and labor_allocation's `version`). This holds on EVERY path
    (scope=None stdio/local-trust, empty/allow-all, and enforced), so the sample
    is consistent with getTableSchema, and a `SELECT *` can never leak an
    RLS-hidden column's name or values.

    Column-scope enforcement (D83, matching runQuery's SELECT * treatment,
    D69/OQ-2): the same provenance-extraction + scope-allowlist check `run_query`
    applies is reused here — the query is REJECTED (never partially projected) if
    any VISIBLE column it would return is outside `column_scope`. Hidden columns
    are never in the projection and never force a violation (A-H1). If the table
    can't be enumerated (not in the provenance catalog), extraction fails closed
    and the sample is rejected. Skipped entirely when scope is None or empty —
    identical three-state model as run_query.

    Raises:
        DatabaseNotAllowedError: if *database* is not in the allowlist.
        TableNotFoundError:      if the table has no columns (does not exist).
        ColumnScopeError:        if any visible column of the table is outside
                                  scope (COLUMN_SCOPE_VIOLATION) or the table
                                  can't be enumerated under scope enforcement.
        ParseFailedError:        if column provenance can't be extracted
                                  (PARSE_FAILED_CLOSED) — the sample is rejected,
                                  never executed.
    """
    if settings is None:
        settings = get_settings()

    _check_database_allowed(database, settings)

    # Cap at 50 regardless of caller input.
    effective_limit = max(1, min(limit, 50))

    safe_db = database.replace("`", "``")
    safe_table = table.replace("`", "``")

    # Catalog-hidden columns (RLS physical columns client_code / proc_center, and
    # labor_allocation's internal `version`) must NEVER be surfaced by sampleRows
    # (A-H1) — on any transport, independent of column_scope, exactly as
    # getTableSchema strips them. Resolved from the SAME hidden-column source the
    # overlay uses.
    hidden = hidden_columns_for(get_semantic_catalog(), f"{database}.{table}")

    if hidden:
        # This table hides columns: introspect its real columns and project ONLY
        # the visible set (introspected MINUS hidden), so a `SELECT *` can never
        # leak a hidden column's name or values. Only catalogued RLS tables reach
        # this branch — every other table keeps the plain `SELECT *` below (its
        # visible set == all columns, so the two are equivalent), avoiding an
        # introspection round-trip for the common case. The metadata query is the
        # same one get_table_schema uses; it does not scan table data.
        intro_sql = (
            "SELECT name "
            "FROM system.columns "
            "WHERE database = {db:String} AND table = {tbl:String} "
            "ORDER BY position"
        )
        _, col_rows = _execute(
            intro_sql, settings, parameters={"db": database, "tbl": table}
        )
        if not col_rows:
            raise TableNotFoundError(
                message=(
                    f"Table '{database}.{table}' not found or has no columns. "
                    "Check the database and table names with listTables."
                ),
                code="TABLE_NOT_FOUND",
            )
        visible_columns = [row[0] for row in col_rows if row[0] not in hidden]
        if not visible_columns:
            raise TableNotFoundError(
                message=(
                    f"Table '{database}.{table}' has no columns available to sample "
                    "after applying the hidden-column projection."
                ),
                code="TABLE_NOT_FOUND",
            )
        projection = ", ".join(f"`{c.replace('`', '``')}`" for c in visible_columns)
        sql = f"SELECT {projection} FROM `{safe_db}`.`{safe_table}` LIMIT {effective_limit}"
    else:
        sql = f"SELECT * FROM `{safe_db}`.`{safe_table}` LIMIT {effective_limit}"

    # ---------------------------------------------------------------------------
    # Column-scope enforcement (D83) — reuses run_query's exact enforcement path.
    # ---------------------------------------------------------------------------
    scope = get_current_scope()
    session_id = get_current_session_id()

    # Same trigger as _enforce_query_guardrails (ADR-0002/H3): run provenance when
    # a scope is bound (scoped MCP path, unchanged) OR the sample SQL references the
    # scratch DB while the gate is on. A session-less sampleRows of a foreign scratch
    # table (scope=None on REST) then fails closed via _validate_scratch_name. A
    # normal warehouse sample references no scratch DB → no provenance parse.
    references_scratch = _references_scratch_db(sql, settings.scratch_database)
    need_provenance = (scope is not None) or (
        settings.require_session_scratch_gate and references_scratch
    )

    if need_provenance:
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
            # Hidden columns are already excluded from the projection above, so
            # they cannot appear in `uses`; the shared helper also excludes them
            # defensively (A-H1) and scratch columns (session-gated).
            forbidden = _forbidden_out_of_scope_columns(uses, scope)

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


def _enforce_query_guardrails(
    clean_sql: str,
    settings: Settings,
    caller: str = "query_guardrails",
) -> None:
    """Apply the shared security guardrails to already-sanitized *clean_sql*.

    This is the SINGLE enforcement code path shared by run_query and
    explain_query so the two can never drift — a scoped caller must not be able
    to bypass a gate by wrapping a query in EXPLAIN. It applies, in order:

      1. Cartesian-join rejection (UNCONDITIONAL — every caller, scoped or not).
      1b. Statement-kind gate, applied ONLY when provenance is required (below):
         SHOW / DESCRIBE / DESC / EXPLAIN are rejected up front with
         DISALLOWED_STATEMENT_TYPE because the extractor cannot process them.  They
         remain available to unscoped callers (REST/stdio), which never enter that
         branch.
      2. Scratch-session isolation + column-scope enforcement, triggered when
         EITHER a scope is bound (``scope is not None`` — the scoped MCP path,
         behavior identical to before) OR the query references the scratch
         database while ``require_session_scratch_gate`` is on (ADR-0002, H3).
         The latter disjunct is what makes scratch access fail closed on EVERY
         transport — including REST/stdio where scope is None — so a session-less
         caller can never read another session's scratch table. A normal
         warehouse query on a scope-less transport references no scratch DB, so
         provenance is NOT run and the GPT Action keeps working unchanged (no new
         PARSE_FAILED_CLOSED regression).

    Args:
        clean_sql: SQL already passed through validate_and_sanitize.
        settings:  Runtime settings — supplies ``scratch_database`` (the identifier
                   the scratch-reference pre-check scans for) and
                   ``require_session_scratch_gate`` (the ADR-0002 off-switch).
        caller:    Short label used only as the log-line prefix so incident triage
                   can tell which entry point (run_query / explain_query) tripped
                   the gate. It never affects control flow or the raised codes.

    Raises the identical exception types/codes the inline run_query block raised:
    CARTESIAN_JOIN_FORBIDDEN, SCRATCH_SESSION_VIOLATION, PARSE_FAILED_CLOSED,
    and COLUMN_SCOPE_VIOLATION — plus QueryValidationError/DISALLOWED_STATEMENT_TYPE
    from the statement-kind gate. Returns None when the query is permitted.
    """
    # ---------------------------------------------------------------------------
    # Cartesian-join guardrail (applies to EVERY caller — scoped HTTP/JWT AND the
    # no-scope/stdio path — so the block cannot be bypassed by dropping the scope).
    # NOTE: this parses clean_sql once here even though the scoped column-provenance
    # path below re-parses inside extract_column_provenance. This minor double-parse
    # is intentional for this slice (the guard must run unconditionally, before the
    # `scope is not None` branch); the two parse passes can be folded together later.
    # reject_cartesian_joins is a no-op on unparseable SQL — that case is already
    # fail-closed by the provenance parse (PARSE_FAILED_CLOSED).
    # ---------------------------------------------------------------------------
    try:
        reject_cartesian_joins(clean_sql)
    except CartesianJoinForbiddenError as exc:
        logger.warning(
            "%s: cartesian join forbidden tables=(%s, %s) code=CARTESIAN_JOIN_FORBIDDEN",
            caller,
            exc.table_a,
            exc.table_b,
        )
        raise CartesianJoinError(
            message=exc.message,
            code="CARTESIAN_JOIN_FORBIDDEN",
        ) from exc

    # ---------------------------------------------------------------------------
    # Column-scope and scratch-isolation enforcement (D57, D63, D64, ADR-0002/H3)
    # ---------------------------------------------------------------------------
    scope = get_current_scope()
    session_id = get_current_session_id()

    # Trigger provenance extraction when EITHER a scope is bound (scoped MCP path,
    # unchanged) OR the query references the scratch DB while the ADR-0002 gate is
    # on. The scratch-reference disjunct is what closes H3 on scope-less transports
    # (REST/stdio): a scratch reference with a None/mismatched session then fails
    # closed inside extract_column_provenance. A normal warehouse query on a
    # scope-less transport references no scratch DB → need_provenance is False → no
    # parse, no PARSE_FAILED_CLOSED regression (the GPT Action stays working).
    references_scratch = _references_scratch_db(clean_sql, settings.scratch_database)
    need_provenance = (scope is not None) or (
        settings.require_session_scratch_gate and references_scratch
    )

    if need_provenance:
        # -----------------------------------------------------------------------
        # Statement-kind gate — reject metadata SQL HONESTLY, before the extractor.
        #
        # SHOW / DESCRIBE / DESC / EXPLAIN clear validate_and_sanitize's allowlist
        # because they are legitimate on the unscoped REST/stdio paths, where this
        # branch is never entered.  Once provenance IS required they cannot succeed:
        # SHOW/EXPLAIN parse as exp.Command and DESCRIBE/DESC as exp.Describe, none
        # of which extract_column_provenance accepts, so they used to fall through to
        # PARSE_FAILED_CLOSED — an error telling the caller to "simplify the SQL",
        # which is unactionable because no rewrite makes SHOW TABLES a SELECT.
        #
        # Found by tracing live agent turns: the model retried rejected metadata SQL
        # round-trip after round-trip against that advice and ran out its budget.
        # DISALLOWED_STATEMENT_TYPE names the allowed set and the tools that serve the
        # same need, so the model can correct on the first attempt.
        #
        # Routing these PAST provenance instead would be a scope hole, not a fix:
        # SHOW TABLES reveals table names and DESCRIBE reveals column names, which is
        # precisely what column scope governs.  listTables / getTableSchema serve that
        # need with scope filtering applied.
        # -----------------------------------------------------------------------
        if not statement_supports_provenance(clean_sql):
            logger.warning(
                "%s: metadata statement rejected code=DISALLOWED_STATEMENT_TYPE", caller
            )
            raise QueryValidationError(
                message=(
                    "Only SELECT and WITH statements can be run here. SHOW, DESCRIBE "
                    "and EXPLAIN cannot be checked against your column scope — use the "
                    "listTables and getTableSchema tools for metadata, and pass a plain "
                    "SELECT (no EXPLAIN prefix) to explainQuery for a query plan."
                ),
                code="DISALLOWED_STATEMENT_TYPE",
            )

        catalog = get_catalog_schema()

        try:
            uses = extract_column_provenance(clean_sql, catalog, session_id=session_id)
        except ScratchSessionError as exc:
            # Scratch table belongs to a different session (D64).
            logger.error(
                "%s: scratch session violation code=SCRATCH_SESSION_VIOLATION", caller
            )
            raise ColumnScopeError(
                message="Query accesses a scratch table that does not belong to this session.",
                code="SCRATCH_SESSION_VIOLATION",
            ) from exc
        except ProvenanceExtractionError as exc:
            # Parse/qualify failed — fail-closed (D63): never execute.
            logger.error(
                "%s: provenance extraction failed code=PARSE_FAILED_CLOSED", caller
            )
            raise ParseFailedError(
                message=(
                    "Column provenance could not be extracted from this query. "
                    "Simplify or restructure the SQL so it parses — EXPLAIN is "
                    "subject to the same verification and will not bypass it."
                ),
                code="PARSE_FAILED_CLOSED",
            ) from exc

        # Empty frozenset == ALLOW-ALL: skip the column-allowlist check entirely.
        # Only a non-empty scope enforces the column allowlist (D57).
        # Scratch columns (database prefix == "scratch") are session-gated, not
        # scope-gated (D69/OQ-4) — they are intentionally excluded regardless.
        # Catalog-hidden RLS columns (client_code / proc_center / version) are
        # NEVER required in scope (A-H1): a `SELECT *` that qualify_columns expands
        # to include a hidden physical column must not raise a spurious
        # COLUMN_SCOPE_VIOLATION, since that column is stripped from every
        # model-facing surface and is enforced server-side by the row policy.
        if scope:
            forbidden = _forbidden_out_of_scope_columns(uses, scope)

            if forbidden:
                # Log only the count to keep server logs terse; the column
                # NAMES themselves are catalog metadata (surfaced to the model
                # in the error message below), not a secret.
                logger.warning(
                    "%s: column scope violation forbidden_count=%d code=COLUMN_SCOPE_VIOLATION",
                    caller,
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
        CartesianJoinError:      if the query cross-joins two physical base tables
                                  without a join condition (CARTESIAN_JOIN_FORBIDDEN).
        ColumnScopeError:        if the query references a scratch table owned by
                                  another session (SCRATCH_SESSION_VIOLATION) or a
                                  column outside the caller's scope
                                  (COLUMN_SCOPE_VIOLATION).
        ParseFailedError:        if column provenance cannot be extracted, so the
                                  query is fail-closed unexecuted (PARSE_FAILED_CLOSED).
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

    _enforce_query_guardrails(clean_sql, settings, caller="run_query")

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

    explain_query applies the SAME scope/session/scratch guardrails as run_query
    BEFORE wrapping the SQL in EXPLAIN, so it is NOT an escape hatch: a query that
    fails those gates is rejected here too, without a plan ever being built.

    Returns the compact {columns, rows, row_count, truncated=False} shape.

    Raises:
        QueryValidationError:    if the inner SQL fails the security guardrails.
        CartesianJoinError:      if the query cross-joins two physical base tables
                                  without a join condition (CARTESIAN_JOIN_FORBIDDEN).
        ColumnScopeError:        if the query references a scratch table owned by
                                  another session (SCRATCH_SESSION_VIOLATION) or a
                                  column outside the caller's scope
                                  (COLUMN_SCOPE_VIOLATION).
        ParseFailedError:        if column provenance cannot be extracted, so the
                                  query is fail-closed unexecuted (PARSE_FAILED_CLOSED).
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

    # Apply the SAME guardrails run_query applies BEFORE wrapping in EXPLAIN.
    # Without this, a scoped caller could EXPLAIN a SELECT against another
    # session's scratch table (or an out-of-scope column) and have ClickHouse
    # confirm its existence/structure while building the plan — the exact leak
    # this shared gate closes.
    _enforce_query_guardrails(inner_sql, settings, caller="explain_query")

    explain_sql = f"EXPLAIN {inner_sql}"
    columns, rows = _execute(explain_sql, settings)

    return _compact_result(columns, rows, settings.max_response_rows, truncated_override=False)
