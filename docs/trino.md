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

### ClickHouse SQL conversion coverage

Conversion is a supported subset, not a guarantee of arbitrary ClickHouse SQL
compatibility. SQLGlot is pinned to 30.12.0 so local, deployment, and CI parsing
agree. ClickHouse-only queries continue through the native route.

The construct regression suite supports:

- Literal scalar `WITH` bindings, including nested query shadowing. Expression
  bindings are rejected; table CTEs are retained.
- Unary `groupUniqArray(x)` with NULL arguments excluded and an empty array for
  empty input, composed with unary `arraySort` and `arrayStringConcat` on string
  arrays (optional separator, default empty string).
- Constant positive tuple positions, including `x.1` in array lambdas, rendered
  as Trino row subscripts `x[1]`.
- `today()` and integer-literal day offsets such as `today() - 30`. The date is
  evaluated in the Trino session timezone; deployments must align that timezone
  with the intended ClickHouse timezone. Cross-timezone equivalence is not implied.

The following now fail before execution with `TRINO_UNSUPPORTED_SQL`:
unsupported parameterized aggregates (including non-median quantiles and size-limited
`groupUniqArray`), unsupported `intDiv` overloads, `formatReadableQuantity`, expression
scalar WITH bindings, and unsupported signatures of the new array/date mappings.
Exact quantiles are never silently replaced by approximate percentiles.

Type-aware semantics now preserve the following behavior:

- `sum` / `sumIf`: zero for empty non-nullable numeric input; NULL for empty
  nullable input. No blanket `COALESCE` for nullable salaries.
- `avg` / `avgIf`: NaN for empty non-nullable input; NULL for nullable input.
  Decimal values are averaged as DOUBLE to match ClickHouse's Float64 result,
  rather than rounding the mean to the input decimal scale.
- Numeric `minIf` / `maxIf`: zero for an empty non-nullable input and NULL
  for an empty nullable input. String/date defaults remain unsupported.
- `quantileExactLow(0.5)` / `quantileExactHigh(0.5)` and their `If` variants:
  exact lower/upper medians using sorted non-null source values. Decimal and
  integer values are not cast to DOUBLE. Empty inputs preserve zero for
  integer/decimal, NaN for float, and NULL for nullable inputs. Odd/even groups,
  duplicates and conditional selection are supported. Other levels and
  windowed quantiles remain explicit rejections; no approximate substitution.
- Windowed `sum`, `avg`, `sumIf`, `avgIf`, numeric `minIf` and `maxIf`:
  retain PARTITION BY, ORDER BY, ROWS/RANGE frames and named windows. Empty-frame
  defaults wrap the complete window result. Conditional windows use CASE
  inside the aggregate because Trino does not support their FILTER syntax.
  Grouped totals such as `sum(count()) OVER ()` are supported.
- Scalar `argMax`: skip rows with a NULL value or key; preserve the source
  nullability-dependent empty-set default. Ties remain unspecified, as at source.
- `length`: count UTF-8 bytes for strings and elements for arrays.
- `toDateTime`: convert known integer Unix epochs (including nullable inputs),
  saturate to the DateTime UInt32 range, and truncate supported finite numeric
  literals to whole seconds. ISO date/time string literals without fractional
  seconds are supported. Session timezones must still match.
- Literal `JSONExtractString` string-key paths: preserve string values, NULL
  input, and the empty-string default for missing/null values or malformed JSON.

These functions fail before dispatch when input type/nullability cannot be
resolved from the logical catalog, supported derived projections, or explicit
SQL types. Outer-join and UNION inference, windowed argMax, tuple argMax,
non-literal floating epochs, decimal epochs, dynamic string-to-DateTime parsing,
date/timestamp-to-DateTime range wrapping, and timezone overloads are not admitted. An explicit cast to a non-nullable type
is not treated as proof when its source can be NULL.

Dynamic JSON extraction, numeric JSON paths, duplicate keys, and serialization
of non-string JSON values are explicitly rejected: ClickHouse's formatting is
not equivalent to Trino's JSON scalar extraction. Literal extraction does not
use approximate serialization. Unknown functions outside these guarded families
may still reach Trino and fail there; this is not universal dialect equivalence.

NaN/Infinity retain their SQL semantics during calculation. If they remain in
returned rows, the shared REST/MCP result boundary raises `NON_FINITE_RESULT`
instead of silently converting them to NULL or failing JSON serialization.
Queries must handle such final values explicitly.

### Exact median execution cost

Exact median conversion uses ARRAY_AGG + ARRAY_SORT and COUNT over the same
eligible rows, followed by integer positional selection. It retains O(n)
values per group and sorts them; it does not guarantee source pushdown.
Large payroll groups can hit Trino query memory/time limits. These limits
must fail loudly, never trigger a silent approximate result. Windowed medians
and arbitrary percentile levels remain out of scope for this implementation.
ClickHouse-only queries continue through the native ClickHouse route.

The source definitions are documented in [ClickHouse Low](https://clickhouse.com/docs/reference/functions/aggregate-functions/quantileExactLow)
and [High](https://clickhouse.com/docs/reference/functions/aggregate-functions/quantileExactHigh).

### Schema-aware SQLGlot analysis

The converter uses SQLGlot `qualify` and `annotate_types` on a copy of the
logical query, using catalog column types. Only validated type metadata returns
to the executable AST; qualification does not change routing, authorization,
projections, stars, or user aliases. A qualification failure retains the
conservative scoped analysis rather than assuming a type.

This enables aggregates over supported CASE expressions, numeric COALESCE,
ABS, and decimal/integer ROUND, including derived projections. ClickHouse
nullability rules remain explicit: an omitted CASE ELSE is nullable, COALESCE
is non-nullable when a guaranteed non-null input exists, and unknown functions
remain unknown. Floating-point ROUND is not admitted by this new inference
because ClickHouse and Trino have different tie-breaking rules.

SQLGlot 30.12.0 and 30.22.0 were compared on nine synthetic analysis queries.
Both inferred useful expression types, but neither supplied the required
nullability guarantees for COALESCE, mixed-nullability UNIONs, or outer joins;
both also reported ROUND(Decimal) as DOUBLE. The deployment pin therefore
remains 30.12.0. Run `scripts/evaluate_sqlglot_analysis.py` under a candidate
Python environment to reproduce the comparison without database access.
Window-frame semantics, outer-join settings, and remaining timestamp overloads
still require separate compatibility work. The library analysis alone does not
remove those guards.

The remote failure-matrix revisit also added type inference for day-based
`dateDiff` over Date/DateTime64 inputs, with DATE-cast endpoints to preserve
ClickHouse calendar-day boundaries. Explicit timezone overloads remain rejected.
Identity paging wrappers (`SELECT * FROM (ordered SELECT) LIMIT/OFFSET`) now
repeat projected ordering at the outer level, because Trino may discard an
inner ORDER BY. Paging wrappers with unprojected ordering expressions fail
explicitly instead of returning an unstable page.


### Integer division and query failure guidance

`intDiv(value, positive_integer_literal)` now supports signed-64-compatible
integer inputs and Decimal precision up to 18, including nullable columns and
literal scalar WITH divisors. Decimal inputs are truncated before conversion to
BIGINT, then divided using integer arithmetic. This preserves salary bucket
boundaries and truncation toward zero for negative values without a DOUBLE
round-trip. Nested bucket labels such as `intDiv(bucket, 1000)` are supported
when the bucket's type is known. Float inputs, wide decimals/UInt64, dynamic,
negative or zero divisors remain explicit rejections rather than guessed
conversions. Arithmetic elsewhere in the expression retains its existing
range/overflow limitations.

REST and MCP runQuery/explainQuery errors now include a bounded excerpt derived
only from the caller's original ClickHouse SQL, with literals/comments removed.
A named rejected function gets a targeted expression; execution failures without
a reliable logical source location show clearly labelled query context. Physical
Trino locations are never presented as logical SQL locations. Unparseable SQL
gets quoting/parenthesis guidance rather than unsafe raw-text echoing.

Recovery advice explicitly requests ClickHouse SQL on logical catalog tables.
Known unsupported families include practical alternatives (e.g. numeric output
or `toString(bucket)` instead of human-readable quantity formatting), with
semantic differences stated. No guidance suggests approximate replacements for
exact medians. Authorization denials retain their policy instructions; outages
retain operator instructions rather than suggesting policy bypasses or SQL edits.
ClickHouse execution errors, like Trino errors, expose allowlisted error labels
and authored hints only; private backend messages, expanded view SQL and data
values are not returned. These excerpts are response-only, not log payloads.


### Schema authoring contract

In Trino mode, `getTableSchema` exposes catalogued ClickHouse types, including
`Nullable`, intersected with live available columns. Physical Trino types are
retained internally; they are not SQL-authoring instructions. This aligns schema
responses with the semantic validator and keeps one dialect for the model.
Use typed date bounds (`toDate('2026-01-01')` for Date inputs). A catalog type is
an authored contract, not proof of physical compatibility: deployments must still
check precision, scale, nullability, date ranges and timezone semantics against
actual source projections. Never fix a disagreement by silently rounding money,
dropping Nullable, or inventing a type for an unknown expression.

### Year extraction and integer conversion

`toYear(x)` accepts known logical Date/Date32 and timezone-free timestamp inputs.
Use catalog columns or supported date constructors; strings, epoch numbers and
explicit timezone overloads are not inferred as equivalent dates.

`toInt64(x)` truncates fractional values toward zero before the BIGINT cast.
Supported inputs are signed-64-compatible integer types, Decimal precision at
most 18, and finite Float32/Float64 values in `[-2^63, 2^63)`. Nullable inputs
remain nullable, including inside aggregates. Out-of-range/non-finite floats
raise an execution error; string parsing, UInt64 wraparound and wider numeric
contracts are rejected during conversion. For exact salary bucketing, prefer
`intDiv(annual_salary, 25000)` over first converting monetary values to float.

Type-error feedback lists bounded, literal-redacted candidate expressions from
the submitted ClickHouse SQL (CASE/coalesce, UNION positions, window defaults,
and comparisons). These are investigation targets, not confirmed coordinator
locations or automatic casts. Physical source SQL is never used for excerpts.

### Model-facing execution errors

MCP exposes backend-neutral `[CODE]` errors with a versioned JSON body containing
`code`, `message`, `input_dialect: "clickhouse"`, `guidance`, and `retry`.
A redacted `candidate_expression` is included when caller SQL is available.
SQL/type/function failures require a query change; outages require service
recovery, configuration failures require operator action, and identity failures
require restored access. Resource limits require a scope or capacity change.
Existing authorization codes remain unchanged and offer no bypass.

Backend adapters still sanitize and log the original error category. The shared
query-feedback builder supplies structured feedback directly to the MCP formatter;
MCP does not reparse REST error prose. REST error codes remain compatible. The
agent reads the versioned fields directly; regex diagnosis is retained only for
legacy API responses and saved traces. Neither physical SQL nor coordinator
messages are introduced into the public contract.

Function recovery guidance is curated from observed trace failures and the remote
query-pattern report. It covers numeric conversion/year extraction, salary
bucketing and display formatting, exact median variants, directory-array
functions, date windows, conditional extrema, and exact distinct counts.
Runtime function errors do not expose coordinator SQL or identify an offender
from an untrusted backend message: guidance is selected from parsed caller
functions and explicitly labels them as candidates. Unknown functions receive
signature/schema guidance without an invented replacement. Examples remain
ClickHouse SQL and preserve exactness, NULL rules, filters and grouping; a
replacement result shape is offered only when it meets the user's request.
This guidance does not expand the transpiler's supported semantic domain.
