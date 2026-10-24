"""Domain exceptions for the ClickHouse API service layer.

These exceptions are transport-agnostic — they carry structured error
information that can be mapped to HTTP status codes by FastAPI routers or
to MCP tool errors by the MCP server.  Neither exception inherits from
HTTPException so the service layer has no dependency on FastAPI.
"""

from __future__ import annotations


class ClickHouseAPIError(Exception):
    """Base class for all domain errors raised by app/service.py."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class QueryValidationError(ClickHouseAPIError):
    """Raised when SQL fails the security guardrails (validate_and_sanitize).

    Maps to HTTP 400 in the REST layer.
    """


class ArgumentValidationError(ClickHouseAPIError):
    """Raised when a NON-SQL tool/route argument is malformed (M3).

    Distinct from QueryValidationError, which means "your SQL failed the
    security guardrails" and whose caller-facing message tells the model to fix
    its SQL.  A malformed `columns` narrowing list on getTableSchema has no SQL
    to fix — reusing QueryValidationError would hand a self-correcting model a
    misleading instruction and send it editing a query it never sent.

    Code: INVALID_ARGUMENT

    Maps to HTTP 400 in the REST layer.
    """


class DatabaseNotAllowedError(ClickHouseAPIError):
    """Raised when the requested database is not in the ALLOWED_DATABASES allowlist.

    Maps to HTTP 403 in the REST layer.
    """


class TableNotAllowedError(ClickHouseAPIError):
    """Raised when the requested table is not in the semantic catalog allowlist.

    Only fires when RESTRICT_TO_CATALOGUED_TABLES is enabled: a table that is not
    present in the semantic catalog (the 'schema dictionary') is not permitted to
    be described or sampled, and is hidden from listTables.  The scratch database
    is exempt (its tables are never catalogued and are governed by per-session
    isolation instead).

    Distinct from TableNotFoundError: the table may well exist in ClickHouse — it
    is simply not exposed through this API because it is uncatalogued.

    Code: TABLE_NOT_ALLOWED

    Maps to HTTP 403 in the REST layer.
    """


class TableNotFoundError(ClickHouseAPIError):
    """Raised when the requested table has no columns (i.e. does not exist).

    Maps to HTTP 404 in the REST layer.
    """


class ClickHouseQueryError(ClickHouseAPIError):
    """Raised when ClickHouse returns a query-level error (syntax, unknown table, etc.).

    Maps to HTTP 400 in the REST layer.
    """


class ClickHouseUnavailableError(ClickHouseAPIError):
    """Raised when the ClickHouse server is unreachable.

    Maps to HTTP 502 in the REST layer.
    """


class ColumnScopeError(ClickHouseAPIError):
    """Raised when a query references columns outside the caller's permitted scope.

    Two codes are used:
      COLUMN_SCOPE_VIOLATION — one or more warehouse columns are not in the JWT scope.
      SCRATCH_SESSION_VIOLATION — a scratch table does not belong to the caller's session.

    Maps to HTTP 403 in the REST layer.
    """


class ParseFailedError(ClickHouseAPIError):
    """Raised when column provenance cannot be extracted from the SQL (D63 fail-closed).

    The query is rejected without execution. explainQuery enforces the same
    verification and cannot bypass this failure.

    Code: PARSE_FAILED_CLOSED

    Maps to HTTP 400 in the REST layer.
    """


class CartesianJoinError(ClickHouseAPIError):
    """Raised when a query cross-joins two physical base tables without a join condition.

    A cartesian product between two base tables (CROSS JOIN, comma join, or a bare
    JOIN with no ON/USING) is rejected before execution — it is almost always a
    mistake and can multiply row counts catastrophically.  Cross joins where one
    side is a subquery / CTE / table function / VALUES (the common
    ``CROSS JOIN (SELECT …)`` parameter pattern) are exempt.

    The error message names the two offending base tables so the model can
    self-correct by adding an ON/USING condition or wrapping a constant side in a
    subquery.

    Code: CARTESIAN_JOIN_FORBIDDEN

    Maps to HTTP 400 in the REST layer.
    """


class ExportTooLargeError(ClickHouseAPIError):
    """Raised when an answer-snapshot export exceeds its total row/byte budget.

    Enforced LOCALLY by app/export_service.py against EXPORT_TOTAL_MAX_ROWS /
    EXPORT_TOTAL_MAX_BYTES while streaming — never inferred from ClickHouse
    error text.  Any partially-staged/uploaded objects for the aborted snapshot
    are deleted before this propagates.

    Code: EXPORT_TOO_LARGE

    Maps to HTTP 400 in the REST layer.
    """


class SnapshotNotFoundError(ClickHouseAPIError):
    """Raised when a snapshot id is unknown OR belongs to a different tenant hash.

    The two cases are deliberately indistinguishable on the wire — a caller must
    never be able to probe which snapshot ids exist for other tenants.

    Code: SNAPSHOT_NOT_FOUND

    Maps to HTTP 404 in the REST layer.
    """


class SnapshotExpiredError(ClickHouseAPIError):
    """Raised when a snapshot exists but its TTL (EXPORT_TTL_SECONDS) has elapsed.

    Expiry is enforced app-side on every read; the bucket lifecycle rule is only
    the storage backstop.

    Code: SNAPSHOT_EXPIRED

    Maps to HTTP 410 in the REST layer.
    """


class SnapshotScopeChangedError(ClickHouseAPIError):
    """409: same owner/session, but the snapshot's grants no longer match."""
