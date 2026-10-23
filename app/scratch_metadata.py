"""Live metadata for session-owned ClickHouse scratch relations; never cached globally."""

from sqlglot import exp

from app.config import Settings
from app.errors import ColumnScopeError, QueryValidationError, TableNotFoundError
from app.principal import get_current_session_id
from app.scratch_ingest import build_scratch_client
from app.sqlparse.provenance import scratch_table_belongs_to_session


def require_owner(table: str) -> None:
    if not scratch_table_belongs_to_session(table, get_current_session_id()):
        raise ColumnScopeError(
            "Scratch table does not belong to this session.",
            "SCRATCH_SESSION_VIOLATION",
        )


def _query(sql: str, parameters: dict, settings: Settings):
    client = build_scratch_client(settings)
    try:
        return client.query(sql, parameters=parameters).result_rows
    finally:
        client.close()


def columns(table: str, settings: Settings):
    require_owner(table)
    rows = _query(
        "SELECT name, type, comment FROM system.columns WHERE database = %(db)s AND table = %(tbl)s ORDER BY position",
        {"db": settings.scratch_database, "tbl": table},
        settings,
    )
    if not rows:
        raise TableNotFoundError(
            "Scratch table is missing or expired.", "TABLE_NOT_FOUND"
        )
    return rows


def catalog_entries(tree: exp.Expression, settings: Settings) -> dict:
    names = {
        t.name
        for t in tree.find_all(exp.Table)
        if t.db == settings.scratch_database and not t.catalog
    }
    if not names:
        return {}
    allowed = settings.allowed_databases_list()
    if allowed is not None and settings.scratch_database not in allowed:
        raise QueryValidationError(
            "Scratch database is not allowed.", "TABLE_NOT_ALLOWED"
        )
    # Authorize every relation before any metadata I/O.
    for name in names:
        require_owner(name)
    return {
        f"{settings.scratch_database}.{name}": {
            "database": settings.scratch_database,
            "table": name,
            "source": "scratch",
            "columns": {row[0]: {"type": row[1]} for row in columns(name, settings)},
        }
        for name in sorted(names)
    }


def uploaded_tables(settings: Settings):
    """Discover only uploads owned by the bound session, never DAG intermediates."""
    session_id = get_current_session_id()
    if not session_id:
        return []
    rows = _query(
        "SELECT database, name, engine FROM system.tables WHERE database = %(db)s ORDER BY name",
        {"db": settings.scratch_database},
        settings,
    )
    return [row for row in rows if is_uploaded_table(row[1], session_id)]


def is_uploaded_table(table: str, session_id: str | None) -> bool:
    return scratch_table_belongs_to_session(table, session_id) and table.startswith(
        f"s_{session_id}_upload_"
    )
