"""Replaceable tenant routing boundary; no database/network lookup yet."""

from __future__ import annotations

import re

from app.config import Settings
from app.errors import QueryValidationError

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")


def validate_identifier(value: str) -> str:
    """Configuration identifiers, never caller-supplied SQL fragments."""
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise QueryValidationError(
            "Invalid Trino routing identifier in server configuration.",
            "INVALID_TRINO_ROUTING",
        )
    return value


def resolve_mysql_shard(proc_center: str, client_code: str, settings: Settings) -> str:
    """Return e.g. mysql_shard_07. Replace this body with the real routing API.

    The signature is deliberately independent of SQL and model input. The
    configuration stub is indexed by BOTH tenant claims; there is no fallback.
    """
    shard = settings.trino_shard_routes.get(proc_center, {}).get(client_code)
    if shard is None:
        raise QueryValidationError(
            "No MySQL shard route is configured for the authenticated tenant.",
            "MYSQL_SHARD_NOT_FOUND",
        )
    return validate_identifier(shard)
