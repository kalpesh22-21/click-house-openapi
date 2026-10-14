"""Choose direct ClickHouse only when every physical source is known to it."""

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.optimizer.qualify_tables import qualify_tables
from sqlglot.optimizer.scope import traverse_scope

from app.config import Settings
from app.semantic_catalog import get_semantic_catalog
from app.semantic_catalog.loader import DEFAULT_DATABASE


def direct_clickhouse_sql(sql: str, settings: Settings) -> str | None:
    """Return normalized SQL for an eligible query, otherwise retain Trino.

    This is a routing decision, not a replacement for service authorization.
    Inspect all scopes (including unused CTEs), not just the first table.
    Unknown sources, table functions and source-free queries stay on Trino,
    where the existing validation continues to apply. Preserve native functions.
    """
    from app.trino_client import normalize_logical_sql
    from app.trino_metadata import canonical_database

    explain = bool(re.match(r"^\s*EXPLAIN\s+", sql, re.IGNORECASE))
    inner = re.sub(r"^\s*EXPLAIN\s+", "", sql, count=1, flags=re.IGNORECASE)
    normalized = normalize_logical_sql(inner)
    try:
        statements = sqlglot.parse(
            normalized, read="clickhouse", error_level=ErrorLevel.RAISE
        )
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            return None
        tree = qualify_tables(statements[0], db=DEFAULT_DATABASE, dialect="clickhouse")
        catalog = get_semantic_catalog()
        allowed = settings.allowed_databases_list()
        allowed = (
            None if allowed is None else {canonical_database(db) for db in allowed}
        )
        physical = set()
        for scope in traverse_scope(tree):
            for _, source in scope.selected_sources.values():
                if not isinstance(source, exp.Table):
                    continue
                if source.catalog or not isinstance(source.this, exp.Identifier):
                    return None
                entry = catalog.get(f"{source.db}.{source.name}")
                if (
                    entry is None
                    or entry.get("source", "clickhouse") != "clickhouse"
                    or (allowed is not None and source.db not in allowed)
                ):
                    return None
                physical.add(id(source))
        for table in tree.find_all(exp.Table):
            if id(table) not in physical and (
                table.db or table.catalog or not isinstance(table.this, exp.Identifier)
            ):
                return None
        if not physical:
            return None
        # Render qualified names so the direct client's default database cannot
        # change what an unqualified logical table refers to.
        rendered = tree.sql(dialect="clickhouse", unsupported_level=ErrorLevel.RAISE)
        return f"EXPLAIN {rendered}" if explain else rendered
    except SqlglotError:
        return None
