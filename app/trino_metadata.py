"""Tenant-scoped live discovery intersected with the authored semantic catalog."""

from __future__ import annotations

import contextvars
import logging
from pathlib import Path

from app.config import Settings
from app.errors import ClickHouseUnavailableError, QueryValidationError
from app.semantic_catalog.loader import DEFAULT_DATABASE, get_semantic_catalog
from app.shard_routing import resolve_mysql_shard, validate_identifier

PUBLIC_DATABASE = "dbpcm_datawarehouse"

# Per-REQUEST discovery memo, following the same contextvars request-isolation
# pattern as app.principal (current_principal / current_scope / current_session_id):
# the MCP HTTP middleware binds a fresh dict next to the principal and resets it
# (via the token) when the request ends — the memo dies with the request context,
# so metadata can never be shared ACROSS requests or leak onto a pooled thread.
# None (the default) means UNMEMOIZED one-shot discovery: REST (never scope-bound,
# at most one discovery per call), stdio/local-trust, internal callers and tests
# that never bind it.
#
# Entries are keyed by (identity_tuple, database, table) — the identity is a
# discover() input (it drives both the shard route and the effective Trino
# user), so keying on it preserves identity isolation WITHIN a shared request
# context: one principal's results are never served under another identity,
# while scopes derived from the same full sweep are shared freely. The first
# discover() of a request sweeps ALL eligible relations on ONE connection and
# later shapes (scoped table reads, the provenance catalog) project from that
# in-memory snapshot — one MCP operation previously paid up to three separate
# discovery connections for the identical metadata. Failures are NEVER cached:
# a failed discovery raises and a same-request retry reconnects.
discovery_memo: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "trino_discovery_memo", default=None
)


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


def _project(full: dict, database: str | None, table: str | None) -> dict:
    """Project a full discovery result to a (database, table) scope in memory."""
    return {
        key: entry
        for key, entry in full.items()
        if (database is None or entry["database"] == canonical_database(database))
        and (table is None or entry["table"] == table)
    }


def discover(
    settings: Settings, database: str | None = None, table: str | None = None
) -> dict:
    """Resolve before connecting; never reuse another request's metadata or identity.

    When a per-request memo is bound (MCP HTTP transport; see ``discovery_memo``),
    the first call of the request pays for ONE full live sweep and every later
    call — scoped table reads included — projects from that in-memory snapshot
    keyed on (identity, database, table). With no memo bound, each call discovers
    exactly the requested scope.
    """
    from app.trino_client import request_identity

    identity = request_identity(settings)
    memo = discovery_memo.get()
    if memo is None:
        return _discover_live(settings, identity, database, table)
    from app.principal import get_current_principal
    principal = get_current_principal()
    # ClickHouse may use JTI while MySQL uses user_name. Both identities must
    # participate in the request memo key after introducing native RLS sources.
    ch_identity = tuple(sorted(
        (key, str(principal.claims.get(claim)))
        for key, claim in settings.clickhouse_tenant_settings.items()
    )) if principal else ()
    memo_identity = (identity, ch_identity, settings.trino_mysql_source_mode)
    key = (memo_identity, database, table)
    found = memo.get(key)
    if found is None:
        full_key = (memo_identity, None, None)
        full = memo.get(full_key)
        if full is None:
            # First metadata I/O of this request: sweep every eligible relation
            # on ONE connection — narrower shapes would only pay the same probe
            # again when the provenance catalog (always full) asks later.
            full = _discover_live(settings, identity, None, None)
            memo[full_key] = full
        found = _project(full, database, table)
        memo[key] = found
    return found


def _discover_live(
    settings: Settings,
    identity: tuple[str, str, str],
    database: str | None = None,
    table: str | None = None,
) -> dict:
    """Probe Trino for the requested scope; never reuse another request's identity.

    SELECT 1 probes Trino. information_schema verifies source metadata; LIMIT 0
    validates each eligible relation (including view dependencies) without fetching
    business rows. Connectivity/access failures are errors, not stale fallback.
    """
    from app.trino_client import _close, _connect

    shard = None
    entries = {
        key: entry
        for key, entry in logical_catalog(settings).items()
        if (database is None or entry["database"] == canonical_database(database))
        and (table is None or entry["table"] == table)
    }
    groups = {}
    native_entries = {}
    for key, entry in entries.items():
        # Resolve the shard LAZILY (mirroring rewrite_query): only a candidate
        # set that actually contains a mysql-source entry requires a route, so a
        # tenant without one still discovers clickhouse-source relations; the
        # route stays fail-closed the moment a mysql-source entry IS a candidate.
        # proc_center is NOT a routing input; client_code (identity[1]) routes.
        if entry.get("source", "clickhouse") == "mysql" and shard is None:
            shard = resolve_mysql_shard(identity[1], settings)
        if entry.get("source", "clickhouse") == "mysql" and settings.trino_mysql_source_mode == "system_query":
            # Like absent secured views, unprovisioned native sources are not
            # discoverable. Existing invalid contracts still fail closed below.
            path = Path(settings.trino_mysql_native_sources_dir) / f'{validate_identifier(entry["table"])}.json'
            if not path.exists():
                continue
            native_entries[key] = (entry, shard)
            continue
        catalog, schema, name = physical_target(entry, shard or "", settings)
        groups.setdefault((catalog, schema), {})[name] = key
    conn = cursor = None
    try:
        conn = _connect(settings, "|".join(identity))
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        if list(cursor.fetchone() or []) != [1]:
            raise RuntimeError("Metadata connectivity probe failed")
        found = {}
        from app.trino_native_sources import source_relation
        for key, (entry, native_shard) in native_entries.items():
            relation = source_relation(entry, native_shard, identity, settings)
            cursor.execute(f'SELECT * FROM {relation.sql(dialect="trino")} AS metadata_source LIMIT 0')
            cursor.fetchall()
            columns = {
                column[0]: {**entry["columns"][column[0]], "physical_type": str(column[1])}
                for column in (cursor.description or []) if column[0] in entry["columns"]
            }
            if columns:
                found[key] = {**entry, "columns": columns}
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
                relation = source_relation(entries[key], shard or "", identity, settings)
                cursor.execute(f'SELECT "{column}" FROM {relation.sql(dialect="trino")} AS metadata_source LIMIT 0')
                cursor.fetchall()
        return found
    except QueryValidationError:
        raise
    except Exception as exc:
        from app.error_logging import log_failure
        log_failure(logging.getLogger(__name__), "trino.discovery", exc, code="TRINO_DISCOVERY_UNAVAILABLE")
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
        [name, info["type"], info.get("description", "")]
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
