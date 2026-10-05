"""Tenant-scoped live discovery intersected with the authored semantic catalog."""

from __future__ import annotations

from app.config import Settings
from app.errors import ClickHouseUnavailableError, QueryValidationError
from app.semantic_catalog.loader import DEFAULT_DATABASE, get_semantic_catalog
from app.shard_routing import resolve_mysql_shard, validate_identifier

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
        catalog = settings.trino_mysql_view_catalog or shard
        schema = (
            shard if settings.trino_mysql_view_catalog else settings.trino_mysql_schema
        )
    else:
        catalog, schema = settings.trino_clickhouse_catalog, entry["database"]
    return tuple(validate_identifier(x) for x in (catalog, schema, entry["table"]))


def discover(
    settings: Settings, database: str | None = None, table: str | None = None
) -> dict:
    """Resolve before connecting; never reuse another request's metadata or identity.

    SELECT 1 probes Trino. information_schema verifies source metadata; LIMIT 0
    validates each eligible relation (including view dependencies) without fetching
    business rows. Connectivity/access failures are errors, not stale fallback.
    """
    from app.trino_client import _close, _connect, request_identity

    identity = request_identity(settings)
    shard = resolve_mysql_shard(identity[0], identity[1], settings)
    entries = {
        key: entry
        for key, entry in logical_catalog(settings).items()
        if (database is None or entry["database"] == canonical_database(database))
        and (table is None or entry["table"] == table)
    }
    groups = {}
    for key, entry in entries.items():
        catalog, schema, name = physical_target(entry, shard, settings)
        groups.setdefault((catalog, schema), {})[name] = key
    conn = cursor = None
    try:
        conn = _connect(settings, "|".join(identity))
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        if list(cursor.fetchone() or []) != [1]:
            raise RuntimeError("Metadata connectivity probe failed")
        found = {}
        for (catalog, schema), names in groups.items():
            placeholders = ", ".join("?" for _ in names)
            cursor.execute(
                f'SELECT table_name, column_name, data_type FROM "{catalog}".information_schema.columns '
                f"WHERE table_schema = ? AND table_name IN ({placeholders}) ORDER BY table_name, ordinal_position",
                [schema, *names],
            )
            # Consume incrementally and retain only catalogued columns.
            while batch := cursor.fetchmany(1000):
                for name, column, data_type in batch:
                    key = names.get(name)
                    if key is not None and column in entries[key]["columns"]:
                        entry = found.setdefault(key, {**entries[key], "columns": {}})
                        entry["columns"][column] = {
                            **entries[key]["columns"][column],
                            "physical_type": data_type,
                        }
            for name, key in names.items():
                if key not in found:
                    continue
                column = next(iter(found[key]["columns"])).replace('"', '""')
                cursor.execute(
                    f'SELECT "{column}" FROM "{catalog}"."{schema}"."{name}" LIMIT 0'
                )
                cursor.fetchall()
        return found
    except QueryValidationError:
        raise
    except Exception as exc:
        raise ClickHouseUnavailableError(
            "Trino discovery failed. Check the resolved source, views and metadata permissions.",
            "TRINO_DISCOVERY_UNAVAILABLE",
        ) from exc
    finally:
        _close(cursor, conn)


def column_rows(database: str, table: str, settings: Settings) -> list[list[str]]:
    entry = discover(settings, database, table).get(
        f"{canonical_database(database)}.{table}", {}
    )
    return [
        [name, info["physical_type"], info.get("description", "")]
        for name, info in entry.get("columns", {}).items()
    ]


def catalog_schema(settings: Settings) -> dict[str, dict[str, str]]:
    # Authored types preserve the existing ClickHouse-dialect validation contract;
    # live discovery determines which relations and columns actually exist.
    return {
        key: {
            name: info.get("type", "String") for name, info in entry["columns"].items()
        }
        for key, entry in discover(settings).items()
    }
