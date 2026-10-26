"""Public time-off names map to existing warehouse storage after authorization."""

PHYSICAL_TABLE_NAMES = {
    "timeoff_requests": "accrual_events",
    "timeoff_balances": "accrual_balances",
}


def physical_table_name(table: str) -> str:
    return PHYSICAL_TABLE_NAMES.get(table, table)


def native_query(sql: str) -> str:
    """Rewrite warehouse relations only; preserve CTEs, aliases and string literals."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.scope import traverse_scope

    if not any(name in sql for name in PHYSICAL_TABLE_NAMES):
        return sql
    tree = sqlglot.parse_one(sql, dialect="clickhouse")
    changed = False
    for scope in traverse_scope(tree):
        for alias, (_, source) in scope.selected_sources.items():
            if not isinstance(source, exp.Table) or source.name not in PHYSICAL_TABLE_NAMES:
                continue
            if source.catalog or source.db not in ("", "dbpcm_warehouse"):
                continue
            source.set("this", exp.to_identifier(physical_table_name(source.name), quoted=True))
            if not source.alias:
                source.set("alias", exp.TableAlias(this=exp.to_identifier(alias, quoted=True)))
            for column in scope.columns:
                if column.table == alias:
                    column.set("db", None)
                    column.set("catalog", None)
            changed = True
    return tree.sql(dialect="clickhouse") if changed else sql
