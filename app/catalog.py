"""Interim catalog loader — builds the column catalog dict from ClickHouse system.columns.

This is an interim source until the runtime-side catalog (D78) exists.  The
catalog is keyed at ``"database.table"`` granularity and maps to
``{column_name: type_string}`` dicts, which is exactly the shape that
extract_column_provenance() expects.

Caching
-------
The catalog is cached in-memory with a short TTL (default 60 s).  A cache miss
re-queries system.columns for all allowed databases.  This is cheap — system.columns
is a metadata table that does not scan data.  The TTL keeps the cache from going
stale after a schema change without adding a background refresh thread.

Security note
-------------
Only databases in the ALLOWED_DATABASES allowlist are included.  Tables from
other databases cannot appear in the catalog and therefore cause fail-closed
behaviour when referenced in a query (ProvenanceExtractionError).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.clickhouse_client import execute_query
from app.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level in-memory cache
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS: int = 60

_cached_catalog: dict[str, dict[str, str]] | None = None
_cache_timestamp: float = 0.0


def _is_cache_fresh() -> bool:
    return _cached_catalog is not None and (time.monotonic() - _cache_timestamp) < _CACHE_TTL_SECONDS


def _build_catalog() -> dict[str, dict[str, str]]:
    """Query system.columns for all allowed databases and return the catalog dict."""
    settings = get_settings()
    allowed = settings.allowed_databases_list()

    if allowed is None:
        # All databases are permitted; query system.columns across all of them.
        # Exclude system / information_schema to keep the catalog tight.
        sql = (
            "SELECT database, table, name, type "
            "FROM system.columns "
            "WHERE database NOT IN ('system', 'information_schema', 'INFORMATION_SCHEMA') "
            "ORDER BY database, table, position"
        )
        _, rows = execute_query(sql, settings)
    else:
        if not allowed:
            return {}
        # Build a parameterised IN-list using ClickHouse {param:Array(String)} syntax.
        # execute_query passes parameters as bind parameters so this is injection-safe.
        sql = (
            "SELECT database, table, name, type "
            "FROM system.columns "
            "WHERE database IN {dbs:Array(String)} "
            "ORDER BY database, table, position"
        )
        _, rows = execute_query(sql, settings, parameters={"dbs": allowed})

    catalog: dict[str, dict[str, str]] = {}
    for row in rows:
        db: str = row[0]
        tbl: str = row[1]
        col: str = row[2]
        col_type: str = row[3]
        key = f"{db}.{tbl}"
        catalog.setdefault(key, {})[col] = col_type

    return catalog


def get_catalog_schema() -> dict[str, dict[str, str]]:
    """Return the ``{database.table: {column: type}}`` catalog, refreshing if stale.

    Thread safety: the cache is a module-level dict.  Concurrent requests may
    rebuild the cache simultaneously on the first call after a TTL expiry, which
    is acceptable — the result is idempotent and the extra system.columns queries
    are cheap.  A lock would add complexity for negligible benefit here.
    """
    global _cached_catalog, _cache_timestamp

    if _is_cache_fresh():
        return _cached_catalog  # type: ignore[return-value]

    logger.debug("catalog: rebuilding from system.columns")
    try:
        catalog = _build_catalog()
    except Exception:
        logger.exception("catalog: failed to build from system.columns")
        if _cached_catalog is not None:
            # Return stale cache rather than crashing all queries
            logger.warning("catalog: returning stale cache after rebuild failure")
            return _cached_catalog
        raise

    _cached_catalog = catalog
    _cache_timestamp = time.monotonic()
    logger.debug("catalog: rebuilt with %d table entries", len(catalog))
    return catalog


def invalidate_catalog_cache() -> None:
    """Force the next get_catalog_schema() call to rebuild.

    Useful in tests to reset state between cases.
    """
    global _cached_catalog, _cache_timestamp
    _cached_catalog = None
    _cache_timestamp = 0.0
