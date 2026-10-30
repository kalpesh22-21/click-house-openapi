# User-scoped semantic catalog

Status: access-adapter composition and per-column SQL enrichment implemented.
Both access adapters deliberately allow all until production permission rules are supplied.

## Catalog lifecycle

Authored YAML supplies logical schemas, tables, columns, types, and semantic
metadata. Query provenance resolves dependencies against this base schema, then
checks those dependencies against the request's effective permissions. Metadata
responses apply the same scope and deployment restrictions and omit hidden columns.
Physical metadata never silently changes the base schema.

Startup validates the local catalog, including its RLS projection. It does not
require verification identities or an all-shard view sweep. Runtime registry
resolution and catalog-name checks remain intact. Existing health probes report
backend connectivity separately. YAML changes require a restart.

## Session access service

`resolve_principal_permissions` runs during `POST /session/catalog` with the user
JWT and `X-Session-Id`. It invokes:

- `jwt_permission_scope(principal)`: JWT access adapter, currently allow-all.
- `sql_permission_scope(principal)`: SQL access adapter, currently allow-all and
  performing no database I/O.

The adapters' schema and column grants are intersected. `None` means no additional
restriction; an empty frozenset denies access. Database aliases are canonicalized
before intersection. Invalid results or exceptions return
`SCOPE_RESOLUTION_UNAVAILABLE`; there is no allow-all fallback on failure.

The verified principal is bound while the adapters run, then restored. The final
scope is frozen into explicit grants in a signed scope token, alongside a trimmed
catalog snapshot. The agent persists both in its existing Couchbase
`session::<session_id>` document. `MCP_SERVICE_KEY` signs and verifies the token;
no separate signing key is required. `SESSION_SCOPE_TTL_SECONDS` defaults to 28800 (8 hours).
Every live request verifies the user JWT and `X-Catalog-Scope`, including owner,
session, expiry, signature, and authored-catalog version, without running permission
adapters. Missing, stale, expired, or invalid scopes fail closed. Refresh replaces
the snapshot and token together. Context SQL stays on demand and is omitted from
the snapshot. Future SQL adapter calls can use the caller's bound identity without
recursively calling the public query service. JWT claim names, access SQL, and
result mapping remain to be defined. The legacy `column_scope` claim is still
ignored; neither stub interprets it.

Execution checks dependencies including filters, joins, grouping, and ordering.
SELECT * expands against the base catalog and rejects inaccessible dependencies
rather than silently changing its result shape. Source row policies and scratch
session ownership remain independent security controls.

## Trino authentication

Trino authentication is unchanged: `TRINO_USER` / `TRINO_PASSWORD` authenticate
the service connection, while the query user is the verified claim tuple
`proc_center|clientcode|user_name` (claim names are configurable). Trino must permit
the service principal to impersonate those identities. Request JWTs are **not**
forwarded to Trino. Context queries use the same connection helper and identity.

## Per-column context SQL

One optional `context_sql` on a column returns **all dynamic context for that
column**. getTableSchema first applies scope, hidden-column projection, and any
requested column filter. Only retained columns' context queries run, once each.

Example authoring shape (lookup relation and fields are illustrative, not a
production earning-code mapping):

```yaml
database: dbpcm_warehouse
table: accrual_events
source: mysql
columns:
  earn_code:
    type: Nullable(String)
    client_defined: true
    description: Client-defined accrual earning code.
    context_sql: |
      SELECT category AS context_key, earn_code, earn_description
      FROM earning_code_lookup
      WHERE client_code = :clientcode
```

The SQL uses the serving backend's native dialect: Trino when enabled,
ClickHouse otherwise. It must be one read-only SELECT/WITH statement. Existing
statement, write/settings, and unsafe table-function restrictions apply. Lookups
can reference physical relations absent from the semantic catalog, but only
inside the owning entry's logical database/source. Bare relations are resolved
into that source's physical catalog/schema using the caller's shard. Caller SQL
cannot select a different shard via this mechanism. Catalogued MySQL relations expand through their private source templates. Raw
MySQL lookup relations resolve directly to shard.dbonline; their server-authored
context SQL must supply the required tenant/access predicates, using bound claims.
ClickHouse lookups use native-query relations with FINAL and tenant/JTI settings
when served through Trino. Source row policies must cover lookup tables too.

Named `:claim_name` placeholders bind scalar values from verified JWT claims.
They never interpolate SQL text or identifiers. Missing/non-scalar claim values
fail the operation. Parameters are ordered by final rendered SQL, including CTEs.
Context queries run directly through the trusted read-only backend path, without
recursing through access resolution or interpreting physical lookup names as
agent-authored logical SQL. ClickHouse-source lookups retain employee-access
refresh behavior. Trino context requires `TRINO_SECURITY_READY`.

### Result contract

Return uniquely named columns including a non-empty string `context_key` and at
least one other field. Each row's remaining fields become a record under that
key. Multiple rows with the same key form a list; SQL controls their ordering.

```yaml
columns:
  earn_code:
    type: Nullable(String)
    client_defined: true
    description: Client-defined accrual earning code.
    context:
      vacation:
        - earn_code: VAC
          earn_description: Vacation
      timeoff:
        - earn_code: PTO
          earn_description: Paid time off
```

Values above are illustrative. Zero rows with a valid result schema produce
`context: {}`. The query's context replaces dynamic context for that column; the
shared YAML definition is never mutated. Other semantic metadata is preserved.
MCP getTableSchema and the REST `/tables/schema` column response expose context.

### Execution boundaries and errors

runQuery, explainQuery, sampleRows, listDatabases, and ordinary listTables do not
execute context SQL. Catalog exports remain static and do not enrich columns.
`context_sql` is never emitted by getTableSchema or catalog exports. A fresh lookup
runs on each applicable schema request; there is no cross-request context cache.

Context runs under caller identity and existing backend execution limits. It also
has `CATALOG_CONTEXT_MAX_ROWS` (default 1000) and `CATALOG_CONTEXT_MAX_BYTES`
(default 1048576) caps. The service does not inject a truncating LIMIT into these
queries: oversized results fail instead of returning incomplete context. An
explicit authored LIMIT remains the author's chosen semantics.

Missing identity, invalid SQL, routing failures, source errors, invalid result
shape, or exceeded caps fail the entire schema operation with sanitized
`CATALOG_CONTEXT_UNAVAILABLE`. MCP preserves this stage-specific error and advises
operator action. Backend exceptions, private SQL, and identity values are not
included in the error. There is no silent partial success or empty fallback.

## Deployment work remaining

Supply the JWT permission contract and SQL permission lookup when ready. Add real
per-column context SQL to the desired YAML entries using the result contract above,
then regenerate the catalog content fingerprint through the existing authoring
workflow. This implementation deliberately does not invent production access rules
or business-code mappings.
