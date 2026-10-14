# Trino and ClickHouse execution

This runbook intentionally excludes proprietary view definitions, source-report
formulas, private infrastructure URLs and live tenant measurements. Keep those
in deployment-local files, not in repository documentation or tests.

## Routing and configuration

`TRINO_ENABLED=false` uses the existing direct ClickHouse backend.
With `TRINO_ENABLED=true`, ClickHouse-only queries still execute directly in
ClickHouse; MySQL and mixed-source queries use Trino. Restart the API/MCP process
when changing configuration. Logical names and authorization remain unchanged.

MySQL routing matches the authenticated tenant against the trusted local shard
registry configured by `TRINO_SHARD_REGISTRY_PATH` (default
`docker/trino/shards.json`). `TRINO_ENV_TYPE` selects the environment and
`TRINO_MYSQL_SCHEMA` selects the source schema. Missing or ambiguous routes fail
closed. Neither SQL nor caller headers may override the registry or identity.

`TRINO_MYSQL_VIEW_CATALOG` selects the secured-view catalog in view mode.
`TRINO_SECURITY_READY=true` must only be set once connector accounts and source
policies are provisioned. Trino credentials require TLS when a password is set.
Direct ClickHouse credentials are required separately for direct execution.

## Provisioning

Use `scripts/refresh_shard_registry.py --help` to refresh the private registry
from an operator-provided endpoint or saved export, and
`scripts/setup_trino_catalogs.py --help` to generate connector catalogs and render
private templates. The runtime range resolver and generator use the same snapshot.

The local coordinator is defined by `docker-compose.trino.yml`. Connector
catalogs are loaded at startup. Secured views in the memory catalog must be
reapplied after a coordinator restart. Use
`scripts/apply_mysql_views.py --help` and `scripts/apply_trino_views.py --help`
for their dry-run/apply options. Dry-run SQL output is private operational data;
do not commit it or attach it to public logs.

The MySQL view, Trino view and native-source directories, generated connector
properties, credentials and runtime shard registry are ignored by Git and the
Docker build context. Mount the required private configuration separately.
Tests must use synthetic definitions and require no private view files.

## Metadata and hidden join keys

Discovery intersects live backend metadata with the authored semantic catalog.
The public database alias is `dbpcm_datawarehouse`; scope keys use the canonical
`dbpcm_warehouse` namespace. Metadata is memoized only within an authenticated
request, with source-mode and identity separation.

`trino_join_keys` is internal metadata mapping a logical string key to its
numeric column and declared encoding. Eligible joins are rewritten only when
both operands declare the same encoding. The deployment must validate the
encoding's correctness; a matching label does not prove collision freedom.
This metadata is removed from model-facing schema and catalog exports.

## Verification

Run the unit suite with `python -m pytest tests --ignore=tests/integration
--ignore=tests/private`. Live tests must use isolated synthetic data or explicit
operator-provided test access. Do not commit plans containing identities, source
SQL, grant lists or business rows. Validate private policy equivalence and source
index plans before enabling a new mode in production.

## Native source substitution (October 2026)

ClickHouse logical relations now compile to `TABLE(clickhouse.system.query(...))`.
The server injects authenticated tenant settings and the existing read limits,
`readonly=2`, and quoted `final` setting into each native SELECT. ClickHouse must
apply row policies to the connector account and the underlying views; a definer
view that bypasses those policies is not safe. Employee-access freshness is checked
before executing any ClickHouse or mixed-source query, including numeric-join retries.
The model continues to see logical tables, not native SQL or hidden join metadata.

MySQL source selection is controlled by `TRINO_MYSQL_SOURCE_MODE`:

- `views` (default): existing shard routing and secured Trino-view substitution.
- `system_query`: shard routing followed by a private native SELECT containing
  tenant and employee authorization. Trino applies the original wrapper projection
  above it to preserve column names, casts and placeholders.

`TRINO_MYSQL_NATIVE_SOURCES_DIR` defaults to `docker/trino/native_sources`.
Each supported logical MySQL table requires a `<table>.json` policy contract with
exactly three string fields: `authorization` (`employee` or `tenant`), `native_sql`,
and `projection_sql`. Native SELECT placeholders are `{{CLIENT_CODE}}`,
`{{USERNAME}}`, and `{{MYSQL_SCHEMA}}`; the projection is a SELECT from
`native_source e` and can use `{{PROC_CENTER}}`. Identity values are server-authored;
MySQL values use UTF-8 hex literals. Definitions are trusted deployment policy,
not model input: placeholder validation does not prove the policy is correct.
Keep indexed string narrowing plus exact comparisons where needed to preserve
Trino authorization semantics. Queries against missing or invalid definitions fail closed. Discovery omits
unprovisioned sources, as it omits absent secured views; malformed existing
contracts fail discovery. Provision each desired catalog entry before switching modes.
There is no fallback from a native policy failure to an unprotected source.

The native definitions and both engines' proprietary view files are Git-ignored
and Docker-build-ignored. Mount the private directory read-only into the API
container separately. A clean checkout intentionally does not contain these files.
The existing `TRINO_SECURITY_READY` deployment guard still applies.

Verification used isolated Trino 483, MySQL 8.0 and ClickHouse 25.3 containers with
synthetic data: 12 query shapes in both modes returned identical rows and columns,
including CTEs, inner/left joins and three-relation cross-source queries. The payroll
fixture contained 200,000 rows, of which the test principal could access 50,000.
Plans showed grouped decimal SUM, COUNT and COUNT DISTINCT pushed into ClickHouse.
Decimal AVG, conditional SUM and grouping by a derived month stayed in Trino.
Joining payroll to MySQL before aggregation transferred 50,000 payroll rows;
explicitly aggregating payroll in a CTE before the join transferred 5 rows with
identical results for that fixture. This is query-shape dependent, not a universal
rewrite: preserve join multiplicity and grouping semantics when moving aggregates.
`system.query` does not inherently prevent projection or aggregate pushdown, but
neither does it guarantee them. No blanket aggregate rewrite or EAGER setting is
introduced. These synthetic results do not certify production view policies or plans.

MySQL aggregation probes returned identical results in both modes. Native-mode
COUNT and ungrouped SUM pushed into MySQL; the secured-view equivalents stayed
in Trino with the permission join. GROUP BY employee_code remained in Trino in
both modes because the textual grouping semantics still differ between engines.

## Direct execution for ClickHouse-only queries

With `TRINO_ENABLED=true`, query execution now inspects every referenced source in
all CTEs, subqueries and UNION branches. Queries whose physical sources are all
catalogued ClickHouse tables execute through the existing direct ClickHouse client.
This includes sample rows and EXPLAIN. Native functions such as decimal AVG,
sumIf and toStartOfMonth execute in ClickHouse without Trino translation. Public
logical database aliases are normalized, and physical tables are qualified so the
client's default database cannot change their meaning. Queries with MySQL sources
retain Trino routing and the configured MySQL source mode. Source-free or unknown
shapes retain the existing Trino path and validation.

Service scope and SQL guards still run before dispatch. Direct execution refreshes
the employee permission set and uses the existing authenticated tenant settings,
read-only controls and result caps. A direct execution failure is returned; it does
not trigger a retry on Trino. `TRINO_ENABLED=false` retains the original direct-only
behavior. Metadata discovery remains on its existing configured-backend path.
The API deployment therefore needs working direct ClickHouse credentials and row
policies even when Trino is enabled; Trino's JDBC credentials are not reused.

A live isolated ClickHouse 25.3 fixture with 200,000 synthetic payroll rows passed
15 direct execution checks: decimal AVG, conditional SUM, monthly grouping, a CTE
aggregate and EXPLAIN across an allowed principal, a restricted principal and an
ungranted tenant. Tests explicitly prohibited calls to the Trino executor.

## Trino failure handling

Numeric-key fallback now allows only COLUMN_NOT_FOUND, TYPE_MISMATCH and
OPERATOR_NOT_FOUND, and still requires that disabling the optimization changes
SQL. Cancellations, resource limits, authorization failures, transport failures and
unrelated SQL errors do not rerun the query. Partial results are discarded and
cursor/connection cleanup runs after execution or fetch failure.

The Python client's base TrinoQueryError carries INSUFFICIENT_RESOURCES errors;
these now receive sanitized resource guidance under TRINO_QUERY_ERROR rather than
a generic connectivity response. Current 483 names AMBIGUOUS_NAME and
EXPRESSION_NOT_AGGREGATE are recognized, along with overflow, stale/invalid views,
invalid policies and cancellation/capacity errors. Client authentication failures
remain TRINO_UNAVAILABLE with operator guidance; result-decoding failures become
TRINO_QUERY_ERROR with supported-type guidance. Raw connector messages remain
excluded. Discovery keeps its fail-closed TRINO_DISCOVERY_UNAVAILABLE response.

## Deployment error logs

REST and MCP HTTP startup configure application logs on stdout with timestamps,
severity and logger names. ERROR remains visible even with LOG_LEVEL=CRITICAL.
Handled REST domain/HTTP/validation failures and MCP tool/auth/scratch failures
emit ERROR records containing operation, error code, status where available and
exception class. Trino discovery/readiness and numeric-join fallback failures also
log at ERROR; ClickHouse query failures no longer log only at WARNING.
Boundary logs exclude exception messages, request payloads, tokens, SQL and row
values. Existing Trino execution logs retain the coordinator query ID for lookup.
A failure can appear at both the backend and response boundary.

For MCP stdio, application logs and the entrypoint banner go to stderr because
stdout is reserved for JSON-RPC. The HTTP/container deployment uses stdout.
Subprocess tests verify the stream and severity configuration; response-handler
tests cover handled failures without changing HTTP bodies or auth headers.
