"""Execute server-authored column lookups on the caller's resolved source.

SQL is backend-native (Trino when enabled, ClickHouse otherwise). Unqualified
lookup tables live alongside the owning catalog table. Named :claim placeholders
are bound from the verified JWT; SQL and physical names never reach the agent.
"""

from __future__ import annotations

import json
import logging

import sqlglot
from sqlglot import Dialect, exp
from sqlglot.errors import ErrorLevel
from sqlglot.optimizer.qualify_tables import qualify_tables
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.tokens import TokenType

from app.config import Settings
from app.errors import ClickHouseUnavailableError
from app.principal import get_current_principal
from app.security import validate_and_sanitize


def parse_context_sql(sql: str, dialect: str):
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("context_sql must be non-empty SQL")
    clean = validate_and_sanitize(sql, 1, inject_limit=False)
    statements = sqlglot.parse(clean, read=dialect, error_level=ErrorLevel.RAISE)
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError("context_sql must be a single SELECT/WITH query")
    return statements[0]


def _compile(sql: str, entry: dict, settings: Settings):
    from app.shard_routing import resolve_mysql_shard, validate_identifier
    from app.trino_metadata import canonical_database, physical_target

    principal = get_current_principal()
    if principal is None:
        raise ValueError("Column context requires an authenticated request")
    dialect = "trino" if settings.trino_enabled else "clickhouse"
    tree = qualify_tables(parse_context_sql(sql, dialect), db=entry["database"], dialect=dialect)
    # Validate the complete relation set before routing or backend I/O.
    sources = []
    for scope in traverse_scope(tree):
        for _, source in scope.selected_sources.values():
            if not isinstance(source, exp.Table):
                continue
            if (source.catalog or not isinstance(source.this, exp.Identifier)
                    or canonical_database(source.db) != entry["database"]):
                raise ValueError("Context relations must belong to the owning logical database")
            validate_identifier(source.name)
            sources.append(source)
    for table in tree.find_all(exp.Table):
        if table not in sources and (table.db or table.catalog or not isinstance(table.this, exp.Identifier)):
            raise ValueError("Unsupported context relation")

    # Validate all parameters before performing shard resolution.
    for placeholder in tree.find_all(exp.Placeholder):
        value = principal.claims.get(placeholder.name)
        if value is None or not isinstance(value, (str, int, float, bool)):
            raise ValueError("Missing or invalid context parameter claim")

    mysql_catalog = None
    if settings.trino_enabled:
        from app.trino_client import request_identity
        identity = request_identity(settings)
        if entry.get("source", "clickhouse") == "mysql":
            mysql_catalog = resolve_mysql_shard(identity[1], settings)
        catalog, schema, _ = physical_target(entry, mysql_catalog or "", settings)
        from app.trino_sources import clickhouse_source, mysql_source
        from app.semantic_catalog import get_semantic_catalog
        authored = get_semantic_catalog()
        for source in sources:
            if entry.get("source", "clickhouse") == "clickhouse":
                replacement = clickhouse_source({**entry, "table": source.name}, settings)
            else:
                logical = authored.get(f"{entry['database']}.{source.name}")
                if logical is None:
                    # Server-authored raw lookup SQL owns its tenant predicates.
                    source.set("catalog", exp.to_identifier(catalog, quoted=True))
                    source.set("db", exp.to_identifier(schema, quoted=True))
                    continue
                if logical.get("source", "clickhouse") != "mysql":
                    raise ValueError("Context lookup crosses source backends")
                replacement = mysql_source(logical, mysql_catalog, identity, settings)
            replacement.set("alias", source.args.get("alias"))
            source.replace(replacement)
    elif entry.get("source", "clickhouse") == "mysql":
        raise ValueError("MySQL context requires Trino")
    else:
        for source in sources:
            source.set("db", exp.to_identifier(entry["database"], quoted=True))

    class NamedGenerator(Dialect.get_or_raise(dialect).generator_class):
        def placeholder_sql(self, expression):
            return f":{expression.name}"

    rendered = NamedGenerator(unsupported_level=ErrorLevel.RAISE).generate(tree)
    # Generators can render CTEs after SELECT internally, then prepend their
    # text. Bind in final SQL token order, never AST traversal/generation order.
    tokens = Dialect.get_or_raise(dialect).tokenize(rendered)
    parameters, pieces, offset = [], [], 0
    for before, name in zip(tokens, tokens[1:]):
        if before.token_type == TokenType.COLON and name.token_type == TokenType.VAR:
            parameters.append(principal.claims[name.text])
            marker = "?" if settings.trino_enabled else f"%(context_{len(parameters) - 1})s"
            pieces.extend([rendered[offset:before.start], marker])
            offset = name.end + 1
    pieces.append(rendered[offset:])
    rendered = "".join(pieces)
    return rendered, parameters, mysql_catalog


def _check_size(rows: list, settings: Settings) -> None:
    if (len(rows) > settings.catalog_context_max_rows or
            len(json.dumps(rows, default=str, ensure_ascii=False).encode()) > settings.catalog_context_max_bytes):
        raise ValueError("Column context exceeded its result limit")


def execute_context_sql(sql: str, entry: dict, settings: Settings):
    """Trusted read-only execution without recursive permission resolution."""
    if settings.trino_enabled and not settings.trino_security_ready:
        raise ValueError("Trino security is not configured")
    rendered, parameters, mysql_catalog = _compile(sql, entry, settings)
    if entry.get("source", "clickhouse") == "clickhouse":
        from app.employee_access import ensure_employee_access_fresh
        ensure_employee_access_fresh(settings)
    if not settings.trino_enabled:
        from app.clickhouse_client import execute_query
        columns, rows = execute_query(
            rendered, settings,
            {f"context_{i}": value for i, value in enumerate(parameters)} or None,
        )
        _check_size(rows, settings)
        return columns, rows

    from app.trino_client import _close, _connect, request_identity
    conn = cursor = None
    try:
        conn = _connect(settings, "|".join(request_identity(settings)), mysql_catalog=mysql_catalog)
        cursor = conn.cursor()
        if parameters:
            cursor.execute(rendered, parameters)
        else:
            cursor.execute(rendered)
        rows = []
        while batch := cursor.fetchmany(min(1000, settings.catalog_context_max_rows + 1 - len(rows))):
            rows.extend([list(row) for row in batch])
            _check_size(rows, settings)
        return [c[0] for c in cursor.description or []], rows
    finally:
        _close(cursor, conn)


def _context_values(columns: list[str], rows: list) -> dict:
    if len(set(columns)) != len(columns) or "context_key" not in columns or len(columns) < 2:
        raise ValueError("Context results require context_key and at least one value field")
    result = {}
    for row in rows:
        if len(row) != len(columns):
            raise ValueError("Invalid context result shape")
        record = dict(zip(columns, row))
        key = record.pop("context_key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Invalid context key")
        result.setdefault(key, []).append(record)
    return result


def enrich_column_context(response: dict, entry: dict, settings: Settings) -> None:
    """Only emitted columns are enriched; the shared authored catalog is untouched."""
    for column in response["columns"]:
        definition = entry.get("columns", {}).get(column["name"], {})
        if "context_sql" not in definition:
            continue
        try:
            columns, rows = execute_context_sql(definition["context_sql"], entry, settings)
            column["context"] = _context_values(columns, rows)
            # Persist lookup dependencies with schema evidence so a narrower
            # session scope cannot replay formerly accessible context values.
            receipt = response.setdefault("context_provenance", {"version": 1, "columns": []})
            receipt["columns"].append([f"{entry['database']}.{entry['table']}", column["name"]])
        except Exception:
            logging.getLogger(__name__).error("Column context lookup failed: code=CATALOG_CONTEXT_UNAVAILABLE")
            raise ClickHouseUnavailableError(
                "Column context could not be loaded. Ask the operator to check the catalog context query and source permissions.",
                "CATALOG_CONTEXT_UNAVAILABLE",
            ) from None
