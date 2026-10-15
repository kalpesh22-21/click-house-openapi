"""Trusted source generation after logical SQL validation; no caller-native SQL.

MySQL definitions are private deployment configuration, not semantic metadata.
They contain a read-only native SELECT and the existing wrapper's Trino SELECT
projection. Values are encoded separately for each SQL dialect. Definition files
are intentionally not cached: a changed policy must not leave a stale cached SQL
body in the process. Never cache rendered SQL across principals.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from app.config import Settings
from app.errors import QueryValidationError
from app.shard_routing import validate_identifier


def _invalid() -> QueryValidationError:
    return QueryValidationError(
        "Native MySQL source is missing or invalid. Check the private source definition.",
        "TRINO_NATIVE_SOURCE_INVALID",
    )


def _literal(value: str, dialect: str) -> str:
    return exp.Literal.string(value).sql(dialect=dialect)


def _passthrough(catalog: str, sql: str) -> exp.Table:
    # Preserve DBA catalog casing while applying the existing identifier grammar.
    validate_identifier(catalog.lower())
    # SQL string rendering must happen a second time for the Trino envelope.
    query = sqlglot.parse_one(
        f'SELECT * FROM TABLE("{catalog}".system.query('
        f"query => {_literal(sql, 'trino')}))",
        read="trino",
    )
    return query.args["from_"].this


def clickhouse_source(entry: dict, settings: Settings) -> exp.Table:
    from app.clickhouse_client import readonly_settings, tenant_settings
    from app.principal import get_current_principal

    if get_current_principal() is None or not settings.clickhouse_tenant_settings:
        raise QueryValidationError(
            "ClickHouse identity settings are required.", "TRINO_IDENTITY_REQUIRED"
        )
    values = {**tenant_settings(settings), **readonly_settings(settings)}
    assignments = []
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
            raise QueryValidationError(
                "Invalid ClickHouse setting name.", "TRINO_IDENTITY_REQUIRED"
            )
        rendered = (
            _literal(value, "clickhouse") if isinstance(value, str) else str(int(value))
        )
        # The ClickHouse JDBC parser treats unquoted FINAL as a modifier, returning no
        # ResultSet metadata. Quoting the setting preserves query-global FINAL
        # semantics (including underlying views) and works during DESCRIBE.
        setting_name = "`final`" if key == "final" else key
        assignments.append(f"{setting_name} = {rendered}")
    database = validate_identifier(entry["database"])
    table = validate_identifier(entry["table"])
    sql = f"SELECT * FROM `{database}`.`{table}` SETTINGS " + ", ".join(assignments)
    return _passthrough(settings.trino_clickhouse_catalog, sql)


def mysql_source(
    entry: dict, shard: str, identity: tuple[str, str, str], settings: Settings
) -> exp.Subquery:
    """Load an explicit private policy contract; unsupported entities fail closed.

    native_sql uses {{CLIENT_CODE}}, {{USERNAME}}, {{MYSQL_SCHEMA}} placeholders.
    projection_sql is SELECT ... FROM native_source e, preserving Trino types.
    Its optional {{PROC_CENTER}} is a quoted Trino literal generated here.
    authorization is 'employee' or 'tenant', declaring the required identity pins.
    Trust in the policy itself belongs to deployment review, as for secured DDL.
    """
    table = validate_identifier(entry["table"])
    try:
        path = Path(settings.trino_mysql_native_sources_dir) / f"{table}.json"
        definition = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(definition, dict) or not all(
            isinstance(value, str) for value in definition.values()
        ):
            raise ValueError("Definition values must be strings")
        if set(definition) != {"authorization", "native_sql", "projection_sql"}:
            raise ValueError("Invalid definition fields")
        policy = definition["authorization"]
        template = definition["native_sql"]
        if policy not in {"employee", "tenant"} or "{{CLIENT_CODE}}" not in template:
            raise ValueError("Missing tenant contract")
        if policy == "employee" and "{{USERNAME}}" not in template:
            raise ValueError("Missing employee contract")

        # Hex UTF-8 literals avoid any dependence on MySQL NO_BACKSLASH_ESCAPES.
        def mysql_value(value: str) -> str:
            return f"CONVERT(X'{value.encode('utf-8').hex()}' USING utf8mb4)"

        sql = template.replace("{{CLIENT_CODE}}", mysql_value(identity[1]))
        sql = sql.replace("{{USERNAME}}", mysql_value(identity[2]))
        sql = sql.replace(
            "{{MYSQL_SCHEMA}}", f"`{validate_identifier(settings.trino_mysql_schema)}`"
        )
        if "{{" in sql or "}}" in sql:
            raise ValueError("Unresolved placeholder")
        statements = sqlglot.parse(sql, read="mysql", error_level=ErrorLevel.RAISE)
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise ValueError("Native source must be one SELECT")
        if statements[0].args.get("into") or statements[0].args.get("locks"):
            raise ValueError("Writable source")
        # The native SQL stays byte-for-byte authored; sqlglot validates it but
        # does not translate away collation/encoding policy expressions.
        projection = definition["projection_sql"].replace(
            "{{PROC_CENTER}}", _literal(identity[0], "trino")
        )
        projections = sqlglot.parse(
            projection, read="trino", error_level=ErrorLevel.RAISE
        )
        if len(projections) != 1 or not isinstance(projections[0], exp.Select):
            raise ValueError("Invalid projection")
        outer = projections[0]
        refs = list(outer.find_all(exp.Table))
        if (
            len(refs) != 1
            or refs[0].name != "native_source"
            or refs[0].db
            or refs[0].catalog
        ):
            raise ValueError("Invalid projection source")
        if (
            outer.args.get("where")
            or outer.args.get("joins")
            or outer.args.get("with_")
        ):
            raise ValueError("Projection cannot change authorization")
        relation = _passthrough(shard, sql)
        relation.set("alias", refs[0].args.get("alias"))
        refs[0].replace(relation)
        return outer.subquery()
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlglot.errors.SqlglotError,
    ) as exc:
        raise _invalid() from exc


def source_relation(
    entry: dict, shard: str, identity: tuple[str, str, str], settings: Settings
) -> exp.Expression:
    if entry.get("source", "clickhouse") == "mysql":
        if settings.trino_mysql_source_mode == "system_query":
            return mysql_source(entry, shard, identity, settings)
        from app.trino_metadata import physical_target

        catalog, schema, name = physical_target(entry, shard, settings)
        return exp.Table(
            this=exp.to_identifier(name),
            db=exp.to_identifier(schema, quoted=True),
            catalog=exp.to_identifier(catalog, quoted=True),
        )
    return clickhouse_source(entry, settings)
