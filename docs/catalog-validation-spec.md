# Catalog based validation and startup view verification

Status: Implemented with a dummy permission resolver

The authored schema catalog is the authoritative contract for query validation. Provided views and native source relations must implement that contract. Validate their alignment at startup and remove redundant live column discovery from every query request. User access is determined by schema and column scope applied to the catalog, while existing source row policies continue to enforce row access.

## Current behavior and problem

MCP `runQuery` always requests provenance. The service loads a schema for provenance and scope validation through `get_catalog_schema()`. With Trino enabled, this bypasses the direct ClickHouse schema cache and calls live discovery. The first discovery in an MCP HTTP request sweeps all eligible relations; later calls reuse only that request's snapshot.

This introduces a connectivity probe, metadata reads, and relation validation probes before business SQL executes. Native MySQL sources use `SELECT * ... LIMIT 0` to obtain columns. The sweep can include tables unrelated to the query and repeats on the next request.

Column provenance and scope enforcement require a reliable schema definition. They do not require repeated source discovery when the authored catalog is the contract.

## Decisions

- Use the authored schema catalog for column names, logical types, table resolution, star expansion, and provenance validation.
- Verify that configured views and native sources implement that catalog at startup, before accepting query traffic.
- Remove live schema discovery and availability probes from the query validation path, including `runQuery`, `explainQuery`, and equivalent internal service calls.
- Serve metadata tools from the catalog filtered by the caller's effective scope. `sampleRows` uses the same catalog and scope rules before executing its data query.
- Keep authorization checks on every request. Removing discovery must not remove column checks, schema checks, tenant routing, row policies, or scratch session isolation.
- Introduce an explicit schema and column scope contract. A server-side permission-query adapter determines which portion of the catalog a user may access; its initial dummy grants the full catalog.

## Catalog and scope model

Maintain one validated base catalog for the running deployment. It describes the exposed logical schemas, tables, columns, and types, including mappings to their configured source relations. Physical metadata must never silently add columns to this catalog or remove authored entries from it.

Schema scope selects the logical schemas available to a caller. Column scope selects allowed columns within those schemas and tables. Schema scope here means logical database or schema names; table access follows from the permitted catalog entries and columns. Any future table-level grant must be represented explicitly rather than inferred from a source probe.

The effective user catalog is the intersection of the base catalog, deployment restrictions, schema scope, and column scope. A column grant cannot override a denied schema. Metadata tools expose only this effective catalog, omitting tables with no visible columns.

Use explicit representations for unrestricted access, a restricted allowlist, and denied access. A missing or failed scope resolution must not mean unrestricted access. JWT column scopes are retired. Tokens provide identity and session binding only; their former column_scope claim is ignored. The token issuer neither accepts nor emits it. Server-resolved CatalogScope is the authorization input.

For execution, resolve dependencies against the base catalog and check every dependency against the effective user catalog. This includes columns used only in filters, joins, grouping, ordering, and nested queries. Do not resolve a disallowed column as though it were nonexistent and accidentally authorize a different binding.

Preserve the existing strict behavior for `SELECT *`: expand against the base catalog and reject if any expanded user-visible column is outside scope. Do not silently project a narrower result. Preserve the existing treatment of internal row-policy columns and the separate session-bound scratch schema rules.

## Startup verification

Load and validate the authored catalog, source mappings, and native source contracts during application startup. Using a deployment-controlled verification identity, inspect the provided view or source output and compare it with the catalog. Verification must cover each configured source target and eligible shard that the deployment will serve, rather than assuming one shard represents every shard.

For each exposed relation, verify:

- The mapped relation exists and its dependencies can be resolved.
- Every catalog column exists under its expected mapped name.
- Output types are compatible with the logical catalog types under an explicit backend type compatibility policy.
- The user-visible column set matches the catalog. Extra physical columns are permitted only when explicitly designated as internal and prevented from entering user query results or metadata.

Compare logical mappings and compatible types rather than requiring identical backend type strings. Column order is part of the contract wherever star expansion or result shaping relies on it. Verification may use metadata queries and zero-row probes; it must not fetch business rows.

A missing relation, missing column, incompatible type, unexpected exposed column, or inability to verify a required target fails startup readiness. Report actionable relation and column diagnostics without credentials, tenant data, or private source SQL. Do not substitute stale discovered metadata or silently shrink the catalog to make startup pass.

The verification identity and required target inventory must be explicitly configured. Startup cannot rely on an arbitrary request's JWT or prove every user's runtime permissions. Row-policy behavior and user-specific authorization remain separate checks.

Publish the immutable catalog snapshot and its version only after verification succeeds. Catalog, source contract, or served-target changes require verification before activation, through restart or an explicit reload operation. A newly routed target must already be verified for the active catalog version; do not trigger lazy discovery from a user query.

## Request execution

For each query:

1. Resolve the authenticated identity and effective schema and column scope.
2. Parse and validate SQL against the loaded catalog and existing SQL guardrails.
3. Resolve provenance and enforce schema, column, and scratch session restrictions.
4. Route and rewrite the query using existing tenant identity and source policies.
5. Execute the requested SQL and return its results and provenance.

Steps 2 and 3 perform no database metadata I/O. Remove the redundant `SELECT 1`, `information_schema.columns`, and `LIMIT 0` discovery calls from this path. This requirement applies to MySQL, mixed-source, and ClickHouse queries using the Trino validation path. Direct ClickHouse validation should also use the authored catalog rather than rebuilding schema from `system.columns`; source verification belongs at startup.

After startup, a removed view, changed column, or permission revocation is reported through the actual execution failure. Do not automatically rediscover schema, broaden scope, or retry with a different catalog. Startup verification establishes deployment alignment; it does not guarantee that sources remain available forever.

## Permission query resolver

`query_permission_scope(principal)` is the server-controlled permission adapter.
It currently returns an unrestricted `CatalogScope`, granting every authored
schema, table, and user-visible column without a database round trip. Deployment
restrictions, hidden-column projection, and source row policies continue to apply.

Resolve permissions once after identity and session validation, before executing
a tool or route, and bind them to the request principal. REST and MCP use the
same resolver. Repeated metadata and validation operations within a request reuse
that scope; concurrent requests never share permission state. Resolver exceptions
or invalid return values fail closed with `SCOPE_RESOLUTION_UNAVAILABLE` (503).
An explicit empty allowlist denies access; it is never treated as the dummy default.

A future real query replaces only this adapter. It receives verified identity
parameters and must use a trusted execution path that does not recursively call
`runQuery` or depend on the scope it is computing. Unknown grants cannot expand
the authored catalog. Its SQL, result mapping, and revocation behavior will be
specified when the authorization source is available. No physical discovery is
part of permission resolution.

JWT `column_scope` is no longer required, parsed, or used for authorization.
Legacy tokens containing it remain usable but their claim is discarded. The token
issuer rejects that field, including injection through extra claims, and emits
identity/session claims only.

## Implementation outline

- Change `app/catalog.py` to expose the validated authored catalog snapshot for query validation.
- Separate startup verification from `app/trino_metadata.py` discovery and register it in application startup and readiness handling.
- Update service metadata, provenance, and scope paths to use the catalog snapshot and scope resolver.
- Remove request discovery memoization where it is no longer used, including its MCP middleware binding. Keep physical inspection only in startup verification and explicit operator diagnostics.
- Preserve backend routing, source row policies, native contract checks, SQL guardrails, and scratch isolation.
- Update documentation and tests that currently assert one discovery connection per query to assert zero discovery calls.

## Acceptance criteria

- Startup succeeds for matching catalog and source contracts, and readiness fails for required target verification failures or schema mismatches.
- Verification covers both secured view mode and native MySQL source mode, including configured shard differences and backend type mappings.
- Repeated `runQuery` and `explainQuery` calls perform no discovery probes, even on the first request. Validation-only failures execute neither metadata probes nor business SQL.
- `listDatabases`, `listTables`, and `getTableSchema` read the scoped catalog without source discovery. `sampleRows` performs only its authorized data execution, excluding unrelated existing authorization or routing operations.
- Provenance remains correct for joins, CTEs, aliases, expressions, and star expansion. Out-of-scope dependencies are rejected even when absent from the output projection.
- Schema denial overrides column grants. Explicit unrestricted scope, empty restricted scope, missing scope, and failed scope resolution have distinct tested behavior. Concurrent users never share effective permissions.
- Existing tenant row isolation and scratch session isolation tests continue to pass.
- Source drift after startup produces an execution error without metadata rediscovery or scope changes.
- Catalog reloads or newly served targets cannot become active before successful verification.

## Scope of delivery

Implemented: authored-catalog query validation on both backends; catalog-backed
warehouse metadata and sample projections; removal of Trino request discovery
and middleware memo binding; startup verification for REST and MCP; verified-shard
routing guard; per-request schema and column permission resolution; dummy full
catalog grants; removal of JWT column-scope support from validation and issuance.

Dynamic scratch metadata retains live inspection after session checks. Startup
verification configuration is required before serving traffic, and catalog or
target updates use verified restart. The dummy permission adapter is the current
policy; replacing it with the real authorization query awaits that source's SQL
and result contract.
