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
