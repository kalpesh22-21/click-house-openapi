"""Server-owned source relations inserted only after caller SQL is authorized.

MySQL uses private Trino SELECT templates, not deployed views. ClickHouse uses
the connector's native-query table function so tenant settings reach its engine.
Rendered relations are never cached across callers or exported in the catalog.
"""

from pathlib import Path
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel
from sqlglot.optimizer.scope import traverse_scope

from app.config import Settings
from app.errors import QueryValidationError
from app.shard_routing import validate_identifier


def _invalid() -> QueryValidationError:
    return QueryValidationError(
        "Source SQL definition is missing or invalid. Ask the operator to check the private source configuration.",
        "TRINO_SOURCE_DEFINITION_INVALID",
    )


def mysql_source(entry: dict, shard: str, identity: tuple[str, str, str], settings: Settings) -> exp.Subquery:
    """Resolve base relations and fold trusted identity references into literals."""
    try:
        table = validate_identifier(entry["table"])
        validate_identifier(shard.lower())
        schema = validate_identifier(settings.trino_mysql_schema)
        directory = Path(settings.trino_mysql_sources_dir)
        path = directory / f"{table}.txt"
        # Public renames do not require replacing deployed private templates.
        if not path.is_file():
            from app.table_names import physical_table_name

            path = directory / f"{physical_table_name(table)}.txt"
        # Catalog spelling is labor_allocation; the supplied private file uses labour.
        if table == "labor_allocation" and not path.is_file():
            path = directory / "labour_allocation.txt"
        text = path.read_text(encoding="utf-8")
        identifiers = {
            "SHARD_CATALOG": shard, "shard": shard,
            "MYSQL_SCHEMA": schema, "database": schema,
        }
        for key, value in identifiers.items():
            text = text.replace("{{" + key + "}}", exp.to_identifier(value, quoted=True).sql(dialect="trino"))
        for key, value in zip(("PROC_CENTER", "CLIENT_CODE", "USERNAME"), identity):
            text = text.replace("{{" + key + "}}", exp.Literal.string(value).sql(dialect="trino"))
        if "{{" in text or "}}" in text:
            raise ValueError("Unknown source placeholder")
        statements = sqlglot.parse(text, read="trino", error_level=ErrorLevel.RAISE)
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise ValueError("Source must be a single SELECT/WITH")
        tree = statements[0]
        # Supplied definitions use SPLIT_PART(CURRENT_USER, '|', N). Replace
        # the AST node, never splice identity strings into SQL text.
        for split in list(tree.find_all(exp.SplitPart)):
            delimiter, index = split.args.get("delimiter"), split.args.get("part_index")
            if (isinstance(split.this, exp.CurrentUser)
                    and isinstance(delimiter, exp.Literal) and delimiter.this == "|"
                    and isinstance(index, exp.Literal) and index.this in {"1", "2", "3"}):
                split.replace(exp.Literal.string(identity[int(index.this) - 1]))
        for current_user in list(tree.find_all(exp.CurrentUser)):
            current_user.replace(exp.Literal.string("|".join(identity)))
        physical = set()
        for scope in traverse_scope(tree):
            for _, source in scope.selected_sources.values():
                if not isinstance(source, exp.Table):
                    continue
                if (not isinstance(source.this, exp.Identifier)
                        or source.catalog != shard or source.db != schema):
                    raise ValueError("Source relation is outside the routed MySQL database")
                physical.add(id(source))
        for relation in tree.find_all(exp.Table):
            if id(relation) not in physical and (
                relation.catalog or relation.db or not isinstance(relation.this, exp.Identifier)
            ):
                raise ValueError("Unsupported source relation")
        if not physical:
            raise ValueError("Source must read the routed MySQL database")
        return exp.Subquery(this=tree)
    except Exception:
        raise _invalid() from None


def clickhouse_sql(entry: dict, settings: Settings) -> str:
    from app.clickhouse_client import readonly_settings, tenant_settings
    from app.principal import get_current_principal

    if get_current_principal() is None or not settings.clickhouse_tenant_settings:
        raise QueryValidationError("ClickHouse tenant identity settings are required.", "TRINO_IDENTITY_REQUIRED")
    values = {**readonly_settings(settings), **tenant_settings(settings), "final": 1}
    assignments = []
    for name, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
            raise QueryValidationError("Invalid tenant setting name.", "TRINO_IDENTITY_REQUIRED")
        # Quote final to avoid ambiguity with the native FINAL modifier.
        key = "`final`" if name == "final" else name
        literal = exp.convert(value).sql(dialect="clickhouse")
        assignments.append(f"{key} = {literal}")
    from app.table_names import physical_table_name

    database, table = validate_identifier(entry["database"]), validate_identifier(physical_table_name(entry["table"]))
    return f"SELECT * FROM `{database}`.`{table}` SETTINGS " + ", ".join(assignments)


def clickhouse_source(entry: dict, settings: Settings) -> exp.Table:
    catalog = validate_identifier(settings.trino_clickhouse_catalog)
    native_sql = clickhouse_sql(entry, settings)
    # Escape again for the enclosing Trino string; the two SQL dialects use
    # different quote/backslash rules. The caller cannot supply native SQL.
    literal = exp.Literal.string(native_sql).sql(dialect="trino")
    query = sqlglot.parse_one(
        f'SELECT * FROM TABLE("{catalog}".system.query(query => {literal}))', read="trino"
    )
    return query.args["from_"].this


def source_relation(entry: dict, shard: str, identity: tuple[str, str, str], settings: Settings):
    if entry.get("source", "clickhouse") == "mysql":
        return mysql_source(entry, shard, identity, settings)
    return clickhouse_source(entry, settings)
