"""Source freshness for successful results, independent of the execution engine."""

import json
import logging
from datetime import datetime, timezone

import httpx
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from app.principal import get_current_principal
from app.semantic_catalog import get_semantic_catalog
from app.semantic_catalog.loader import DEFAULT_DATABASE
from app.trino_metadata import canonical_database

logger = logging.getLogger(__name__)


def _receipts(tables, settings):
    principal = get_current_principal()
    if not principal or not settings.clickhouse_host:
        return {}
    pc, client = (principal.claims.get(k) for k in ("proc_center", "clientcode"))
    if not isinstance(pc, str) or not pc or not isinstance(client, str) or not client:
        return {}
    placeholders = ", ".join(
        "{table_" + str(i) + ":String}" for i in range(len(tables))
    )
    sql = (
        "SELECT data_product_key, toUnixTimestamp64Milli(toDateTime64(max(last_sync_date), 3)) "
        "FROM dbpcm_warehouse.etl_product_refresh_status FINAL "
        "WHERE processing_center = {pc:String} AND client_code = {client:String} "
        f"AND data_product_key IN ({placeholders}) GROUP BY data_product_key FORMAT JSONCompactEachRow"
    )
    fields = {
        "query": sql,
        "param_pc": pc,
        "param_client": client,
        "readonly": "1",
        "max_execution_time": str(settings.data_as_of_timeout_seconds),
        **{f"param_table_{i}": table for i, table in enumerate(tables)},
    }
    scheme = "https" if settings.clickhouse_secure else "http"
    host = settings.clickhouse_host
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    with httpx.Client(timeout=settings.data_as_of_timeout_seconds) as http:
        response = http.post(
            f"{scheme}://{host}:{settings.clickhouse_port}",
            auth=(settings.clickhouse_user, settings.clickhouse_password),
            files={key: (None, value) for key, value in fields.items()},
        )
        response.raise_for_status()
    stamps = {}
    for line in response.text.splitlines():
        if not line.strip():
            continue
        table, stamp = json.loads(line)
        if table not in tables or isinstance(stamp, bool):
            raise ValueError("Invalid freshness receipt")
        if isinstance(stamp, str) and stamp.lstrip("-").isdigit():
            stamp = int(stamp)
        if type(stamp) is not int:
            raise ValueError("Invalid freshness timestamp")
        stamps[table] = (
            datetime.fromtimestamp(stamp / 1000, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return stamps


def result_freshness(sql, settings, retrieved_at):
    if not settings.data_as_of_enabled:
        return {}
    sources = {}
    unresolved = False
    try:
        catalog = get_semantic_catalog()
        tree = sqlglot.parse_one(sql, read="clickhouse")
        for scope in traverse_scope(tree):
            for _, source in scope.selected_sources.values():
                if not isinstance(source, exp.Table):
                    continue
                if source.catalog or not isinstance(source.this, exp.Identifier):
                    unresolved = True
                    continue
                database = canonical_database(source.db or DEFAULT_DATABASE)
                name = f"{database}.{source.name}"
                entry = catalog.get(name)
                kind = "unknown"
                if entry is not None:
                    physical = (
                        entry.get("source", "clickhouse")
                        if settings.trino_enabled
                        else "clickhouse"
                    )
                    if physical == "mysql":
                        kind = "live"
                    elif physical == "clickhouse" and database == DEFAULT_DATABASE:
                        kind = "ingested"
                sources[name] = {"table": name, "kind": kind}
        ingested = sorted(
            name.rsplit(".", 1)[1]
            for name, value in sources.items()
            if value["kind"] == "ingested"
        )
        stamps = {}
        if ingested and settings.data_as_of_receipts_enabled:
            try:
                stamps = _receipts(ingested, settings)
            except Exception:  # noqa: BLE001 — optional metadata must never fail the data result
                # Optional metadata must not fail the result or leak SQL/tenant/error detail.
                logger.info("Source freshness receipts unavailable")
        for name, source in sources.items():
            if source["kind"] == "ingested" and name.rsplit(".", 1)[1] in stamps:
                source["data_as_of"] = stamps[name.rsplit(".", 1)[1]]
        known = (
            bool(sources)
            and not unresolved
            and all(s["kind"] == "live" or "data_as_of" in s for s in sources.values())
        )
        kinds = {s["kind"] for s in sources.values()}
        mode = (
            ("mixed" if len(kinds) > 1 else next(iter(kinds))) if known else "unknown"
        )
        result = {
            "retrieved_at": retrieved_at,
            "freshness": {
                "mode": mode,
                "sources": [sources[k] for k in sorted(sources)],
            },
        }
        if known and ingested:
            result["data_as_of"] = min(stamps.values())
        return result
    except Exception:  # noqa: BLE001 — optional metadata must never fail the data result
        logger.info("Source freshness classification unavailable")
        return {
            "retrieved_at": retrieved_at,
            "freshness": {"mode": "unknown", "sources": []},
        }
