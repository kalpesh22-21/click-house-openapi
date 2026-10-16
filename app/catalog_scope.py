"""Scope resolver boundary for trusted grants; no physical schema discovery.

The permission-query adapter currently grants the entire authored catalog.
JWT claims cannot choose scope. None means unrestricted within the catalog;
an empty allowlist means deny all. Tenant row policies remain independent.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.constants import SCRATCH_DATABASE
from app.principal import Principal, get_current_principal, get_current_scope
from app.semantic_catalog import hidden_columns_for


@dataclass(frozen=True)
class CatalogScope:
    schemas: frozenset[str] | None = None
    columns: frozenset[str] | None = None

    def allows_schema(self, schema: str) -> bool:
        from app.trino_metadata import canonical_database

        return self.schemas is None or canonical_database(schema) in {
            canonical_database(s) for s in self.schemas
        }

    def allows_column(self, table: str, column: str) -> bool:
        return self.allows_schema(table.rsplit(".", 1)[0]) and (
            self.columns is None or f"{table}.{column}" in self.columns
        )


def query_permission_scope(principal: Principal) -> CatalogScope:
    """Dummy permission query: grant all catalog schemas, tables, and columns.

    This is the replacement point for a server-controlled, identity-parameterized
    authorization query. The dummy does no I/O. Unrestricted here means the
    authored catalog, subject to deployment restrictions and hidden columns;
    it never grants access to uncatalogued physical relations or changes RLS.
    """
    return CatalogScope()


def resolve_principal_permissions(principal: Principal) -> Principal:
    """Resolve once per authenticated request; failures never grant access."""
    from dataclasses import replace

    from app.errors import ClickHouseUnavailableError

    try:
        scope = query_permission_scope(principal)
        if not isinstance(scope, CatalogScope):
            raise TypeError("Invalid permission result")
        for grants in (scope.schemas, scope.columns):
            if grants is not None and (
                not isinstance(grants, frozenset)
                or not all(isinstance(grant, str) and grant for grant in grants)
            ):
                raise ValueError("Invalid permission grants")
    except Exception:  # noqa: BLE001 — never leak authorization SQL or identity data
        raise ClickHouseUnavailableError(
            "Unable to resolve catalog permissions.", "SCOPE_RESOLUTION_UNAVAILABLE"
        ) from None
    return replace(principal, catalog_scope=scope)


def resolve_catalog_scope() -> CatalogScope:
    """Read server-resolved permissions or an explicit local service scope."""
    principal = get_current_principal()
    if principal is not None and principal.catalog_scope is not None:
        return principal.catalog_scope
    # Local service callers may bind a trusted scope directly. No transport
    # populates it from token claims. Preserve its existing empty=all convention.
    scope = get_current_scope()
    return CatalogScope(columns=frozenset(scope) if scope else None)


def scoped_catalog(catalog: dict) -> dict:
    scope = resolve_catalog_scope()
    result = {}
    for key, entry in catalog.items():
        hidden = hidden_columns_for(catalog, key)
        columns = {
            name: info
            for name, info in entry["columns"].items()
            if name not in hidden and scope.allows_column(key, name)
        }
        if columns:
            result[key] = {**entry, "columns": columns}
    return result


def enforce_catalog_scope(sql: str, uses, catalog: dict) -> None:
    from app.errors import ColumnScopeError, ParseFailedError
    from app.sqlparse import ProvenanceExtractionError, extract_referenced_base_tables

    scope = resolve_catalog_scope()
    if scope.schemas is None and scope.columns is None:
        return
    try:
        referenced = extract_referenced_base_tables(sql)
    except ProvenanceExtractionError as exc:
        raise ParseFailedError(
            "Could not verify catalog scope.", "PARSE_FAILED_CLOSED"
        ) from exc
    if any(
        not scope.allows_schema(key.rsplit(".", 1)[0])
        or (
            scope.columns is not None
            and not any(
                grant.startswith(key + ".")
                and (
                    key not in catalog
                    or (
                        grant[len(key) + 1 :] in catalog[key]["columns"]
                        and grant[len(key) + 1 :]
                        not in hidden_columns_for(catalog, key)
                    )
                )
                for grant in scope.columns
            )
        )
        for key in referenced
    ):
        raise ColumnScopeError(
            "Query references a table outside your catalog scope.",
            "COLUMN_SCOPE_VIOLATION",
        )
    for table, column in uses or ():
        if table.startswith(f"{SCRATCH_DATABASE}.") or column in hidden_columns_for(
            catalog, table
        ):
            continue
        if not scope.allows_column(table, column):
            raise ColumnScopeError(
                "Query references columns outside your catalog scope.",
                "COLUMN_SCOPE_VIOLATION",
            )
