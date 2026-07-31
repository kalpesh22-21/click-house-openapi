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


class DatabaseNotAllowedError(ClickHouseAPIError):
    """Raised when the requested database is not in the ALLOWED_DATABASES allowlist.

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

    The query is rejected without execution.  The caller should use explainQuery
    to diagnose whether the SQL is valid, then reformulate if needed.

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
