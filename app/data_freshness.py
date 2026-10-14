"""Internal, caller-scoped ingestion receipts. Never exposed as query data."""

import logging
from datetime import UTC, datetime

from app.clickhouse_client import execute_query
from app.principal import get_current_principal
from app.sqlparse import extract_referenced_base_tables

STATUS_TABLE = "etl_product_refresh_status"
logger = logging.getLogger(__name__)


def data_as_of(sql, settings):
    """Return None when refresh metadata is absent, including a missing status table.

    This optional lookup must never turn a successful data query into an error.
    The result builder omits the wire key whenever this returns None.
    """
    principal = get_current_principal()
    if principal is None or getattr(settings, "trino_enabled", False):
        return None
    mapping = settings.clickhouse_tenant_settings
    pc = principal.claims.get(mapping.get("paycom_proc_center", "proc_center"))
    client = principal.claims.get(mapping.get("paycom_client_code", "clientcode"))
    if not isinstance(pc, str) or not pc or not isinstance(client, str) or not client:
        return None
    try:
        tables = extract_referenced_base_tables(sql, default_db=settings.clickhouse_database)
        # Scratch lineage cannot be established from its name alone.
        if settings.scratch_database.lower() in sql.lower():
            return None
        names = {table.rsplit(".", 1)[-1] for table in tables}
        if not names or STATUS_TABLE in names:
            return None
        _, rows = execute_query(
            "SELECT data_product_key, toUnixTimestamp64Milli(toDateTime64(max(last_sync_date), 3)) "
            "FROM dbpcm_warehouse.etl_product_refresh_status FINAL "
            "WHERE processing_center = %(fresh_pc)s "
            "AND client_code = %(fresh_client)s "
            "AND data_product_key IN %(fresh_tables)s "
            "GROUP BY data_product_key",
            settings,
            {"fresh_pc": pc, "fresh_client": client, "fresh_tables": sorted(names)},
        )
        stamps = {row[0]: row[1] for row in rows}
        if set(stamps) != names or any(value is None for value in stamps.values()):
            return None
        return (
            datetime.fromtimestamp(min(stamps.values()) / 1000, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except Exception:
        # Optional receipt failures must not invent a timestamp or fail the data read.
        logger.warning("Data freshness metadata unavailable")
        return None
