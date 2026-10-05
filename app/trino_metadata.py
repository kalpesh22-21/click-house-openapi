"""Logical metadata for Trino mode; no ClickHouse introspection dependency.

The initial adapter exposes the authored catalog, not unrestricted physical
catalog discovery. Physical sources must implement this normalized contract.
"""

from __future__ import annotations

from app.config import Settings
from app.semantic_catalog.loader import get_semantic_catalog


def logical_catalog(settings: Settings) -> dict:
    allowed = settings.allowed_databases_list()
    return {
        key: entry
        for key, entry in get_semantic_catalog().items()
        if allowed is None or entry["database"] in allowed
    }


def column_rows(database: str, table: str, settings: Settings) -> list[list[str]]:
    entry = logical_catalog(settings).get(f"{database}.{table}", {})
    return [
        [name, info.get("type", "String"), info.get("description", "")]
        for name, info in entry.get("columns", {}).items()
    ]


def catalog_schema(settings: Settings) -> dict[str, dict[str, str]]:
    return {
        key: {
            name: info.get("type", "String") for name, info in entry["columns"].items()
        }
        for key, entry in logical_catalog(settings).items()
    }
