"""Authored catalog metadata and physical source mapping; no discovery probes."""

from __future__ import annotations

from app.config import Settings
from app.semantic_catalog.loader import DEFAULT_DATABASE, get_semantic_catalog
from app.shard_routing import validate_identifier

PUBLIC_DATABASE = "dbpcm_datawarehouse"


def canonical_database(database: str) -> str:
    return DEFAULT_DATABASE if database == PUBLIC_DATABASE else database


def public_database(database: str) -> str:
    return PUBLIC_DATABASE if database == DEFAULT_DATABASE else database


def logical_catalog(settings: Settings) -> dict:
    allowed = settings.allowed_databases_list()
    allowed = None if allowed is None else {canonical_database(db) for db in allowed}
    return {
        key: entry
        for key, entry in get_semantic_catalog().items()
        if allowed is None or entry["database"] in allowed
    }


def physical_target(
    entry: dict, shard: str, settings: Settings
) -> tuple[str, str, str]:
    if entry.get("source", "clickhouse") == "mysql":
        catalog, schema = shard, settings.trino_mysql_schema
    else:
        catalog, schema = settings.trino_clickhouse_catalog, entry["database"]
    validate_identifier(catalog.lower())
    validate_identifier(schema.lower())
    from app.table_names import physical_table_name

    return catalog, schema, validate_identifier(physical_table_name(entry["table"]))


def catalog_schema(settings: Settings) -> dict[str, dict[str, str]]:
    # The authored catalog is the validation contract; request validation
    # must not discover or silently shrink this schema.
    return {
        key: {
            name: info.get("type", "String") for name, info in entry["columns"].items()
        }
        for key, entry in logical_catalog(settings).items()
    }
