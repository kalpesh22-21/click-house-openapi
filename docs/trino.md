# Trino and ClickHouse execution

This runbook intentionally excludes proprietary view definitions, source-report
formulas, private infrastructure URLs and live tenant measurements. Keep those
in deployment-local files, not in repository documentation or tests.

## Routing and configuration

`TRINO_ENABLED=false` uses the existing direct ClickHouse backend.
With `TRINO_ENABLED=true`, ClickHouse-only queries still execute directly in
ClickHouse; MySQL and mixed-source queries use Trino. Restart the API/MCP process
when changing configuration. Logical names and authorization remain unchanged.

MySQL routing reads the operator-configured HTTPS endpoint in `TRINO_ITDBA_URL`.
There is no built-in infrastructure URL and no runtime `shards.json` fallback.
An unset endpoint leaves MySQL routing unavailable unless an allowed, validated
file cache is available. ClickHouse-only execution does not need this endpoint.
`TRINO_ENV_TYPE` selects PROD, QA2 or DEV; `TRINO_MYSQL_SCHEMA` and
`TRINO_ITDBA_NAME` select the source database and application.

Active rows are grouped by environment, database and inclusive client-code range.
Replicas must agree on name, site and level; conflicting groups fail closed.
Catalog names are derived as `name__site__level__database`, replacing hyphens
with underscores and preserving case and leading zeroes. Catalogs must already
exist on the cluster. The API never creates catalogs or views. Optional cached
`SHOW CATALOGS` checks repair a unique case mismatch and exclude `err__` catalogs;
check failures warn and preserve the derived name.

Inventory is memoized per process for `TRINO_ITDBA_CACHE_TTL_SECONDS` (300 by
default). Successful fetches atomically write `TRINO_ITDBA_CACHE_PATH` when set,
with owner-only file permissions. On transport failure, an optional validated
file cache may be served with a stale-age warning. Expired process entries alone
are not a fallback. Set `TRINO_ITDBA_ALLOW_STALE=false` to refuse stale data.
Malformed live routing entries and conflicting groups fail closed. Cache files
must satisfy the same entry validation, filter matching and 1–10,000 entry band.
Keep the cache directory writable only by the service account; stale entries can
outlive a range reassignment when stale serving is enabled.

Registry requests use verified TLS, reject credentials embedded in URLs, refuse
redirects and cap responses at 16 MiB. Errors do not echo identities, SQL, endpoint
URLs or registry payloads. Routing errors are:

- `MYSQL_SHARD_NOT_FOUND`: no range matches.
- `MYSQL_SHARD_AMBIGUOUS`: multiple ranges match.
- `MYSQL_SHARD_REGISTRY_INVALID`: malformed or conflicting live inventory.
- `MYSQL_SHARD_REGISTRY_UNAVAILABLE`: fetch failed with no permitted usable cache.
- `INVALID_TRINO_ROUTING`: invalid server-side SQL identifier.

Configure `TRINO_HOST`, `TRINO_USER` and `TRINO_PASSWORD` through deployment
secrets (the deployment may source these from Vault key `trino`). HTTPS and port
443 are the defaults. User and password must be supplied together; password
authentication refuses HTTP. One connection helper is shared by execution,
metadata and catalog checks. Direct ClickHouse credentials remain separate.

Trino execution expands server-owned MySQL SELECT definitions and wraps ClickHouse
sources in the connector's native-query table function. Logical SQL is validated
before source expansion. Trino owns join planning and connector pushdown.

Set `TRINO_SECURITY_READY=true` only after the private SQL templates and Trino/ClickHouse
policies are provisioned. The query user is always the verified claim tuple
`proc_center|clientcode|user_name` (claim names are configurable). `TRINO_USER` and
`TRINO_PASSWORD` authenticate the service account; they do not replace the query
user. Grant that authenticated principal permission to impersonate the permitted
composite identities in Trino access control. A denied impersonation fails the
request; there is no retry as the service user. Connectivity and catalog inventory
probes may use the service account because they are not data requests.

The MySQL template renderer replaces `split_part(CURRENT_USER, '|', N)` with
escaped literals from the verified identity tuple. The registry selects shards, not grants. Do not expose unauthenticated
Trino directly to callers: without service authentication a supplied user name is
only a claim, not proof of identity. See the official Python client's
[user impersonation contract](https://github.com/trinodb/trino-python-client#user-impersonation).

## Trino Gateway endpoint

For a Gateway deployment, point the existing `TRINO_HOST`/`TRINO_PORT` settings
(or the Vault Trino endpoint) at Gateway over HTTPS. Keep the service credentials
and verified `proc_center|clientcode|user_name` query identity unchanged.
`TRINO_ROUTING_GROUP=interactive` adds the server-configured
`X-Trino-Routing-Group` header to execution, catalog checks and scratch metadata
connections. Empty retains direct-coordinator compatibility. Caller/JWT values
cannot override this routing group.

`helm/clickhouse-api/values-trino-gateway.yaml` is an API/MCP overlay. Merge it with
the deployment's other environment entries and private source mounts; Helm lists
such as `extraEnv` replace rather than append. If Vault supplies the Trino endpoint,
update that secret too. The Gateway and cluster deployments live in the separate
`trino` repository (`deploy/gateway`, `deploy/clusters`, `docs/cluster.md`).

All backends in a group must have matching catalogs, permissions, source mappings
and the same scratch database. The API does not retry a failed query through a
direct coordinator. Existing native ClickHouse-only routing is unchanged.

## Catalog lifecycle

The authored YAML catalog supplies logical types and columns for query validation
on both backends. REST and MCP startup load and validate the local catalog,
including its hidden RLS projection. Startup does not enumerate tenant shards,
impersonate verification identities, or probe every source view. No verification
identity file or startup-approved shard set is required.

Physical targets are resolved from the authenticated caller at execution time.
Registry validation and runtime catalog-name checks remain in place. A newly
routed shard does not require a service restart solely to pass a startup gate.
Source unavailability or schema drift surfaces as an execution error; it does not
silently change the authored schema. Changes to YAML still require a restart.
Backend connectivity is reported by the existing health probes; those probes do
not prove user permissions or view correctness.

Warehouse metadata currently reads the scoped authored catalog. Query validation
does not run `SELECT 1`, `information_schema.columns`, or `LIMIT 0` discovery
sweeps. The unused diagnostic discovery/memo API has been removed. Dynamic
scratch metadata retains its session-gated live inspection.

Catalog permissions are resolved at session initialization or refresh by
`resolve_principal_permissions(principal)` in `app/catalog_scope.py`. It intersects the
JWT and SQL adapter results. Both adapters currently return unrestricted access
by explicit policy; production permission claims and SQL will be supplied later.
Invalid results or adapter errors fail closed. Legacy JWT `column_scope` remains
ignored. Deployment restrictions, hidden columns, row policies, and scratch-session
gates still apply. The agent saves the scoped catalog and signed scope token in
its session document. Subsequent calls send `X-Catalog-Scope` with the user JWT
and `X-Session-Id`; verification uses the existing `MCP_SERVICE_KEY` and does not
repeat permission lookups. There is no default catalog fallback.

Columns may declare server-only `context_sql`. getTableSchema executes one lookup
per requested accessible column on the caller's resolved source and attaches its
complete result as column context. Listings, ordinary queries, and exports do not
run these lookups; exports omit the SQL. See [the catalog design and result
contract](catalog-validation-spec.md) for authoring, limits, and failure behavior.

## Cache operations

Warm the optional cache using the same normalization and validation as runtime:

```sh
python scripts/refresh_shard_registry.py --output /writable/private/registry-cache.json
```

The command reads `TRINO_ITDBA_URL`; `--url` can override it, or `--input` can
supply a saved API export. It performs no DDL. Restart API workers after warming
if they must discard an existing process memo immediately; otherwise the memo
expires naturally. A refresh failure preserves the previous file.

Mount the optional cache on a private writable volume. Do not commit credentials, inventory snapshots or private SQL.
There is no rollback to the removed local snapshot-based routing implementation.

## Metadata

Request metadata and validation use the authored catalog, filtered by server-resolved
permissions. The public database alias is `dbpcm_datawarehouse`; scope keys use
`dbpcm_warehouse`. Legacy internal join metadata remains excluded from public
exports; it does not change query joins. Access-adapter composition and per-column context
are described in [the catalog design](catalog-validation-spec.md).

## Verification

Run the unit suite with `python -m pytest tests --ignore=tests/integration
--ignore=tests/private`. Live tests must use isolated synthetic data or explicit
operator-provided test access. Do not commit plans containing identities, source
SQL, grant lists or business rows. Validate template authorization and deployment policies before serving traffic.

## Source execution

MySQL logical tables expand into private Trino SELECT templates loaded from
`TRINO_MYSQL_SOURCES_DIR` (default `app/semantic_catalog/mysql_views`). Each template is named
`<logical_table>.txt`; `labor_allocation` also accepts `labour_allocation.txt`.
The public names `time_off_request` and `time_off_balance` load the existing
private files `accrual_events.txt` and `accrual_balances.txt`, respectively. Do not
rename these MySQL templates; they are not shipped in Git.
The six supported tables are time_off_request, time_off_balance, employee,
department, expense and labor_allocation. The six catalog entries without
source definitions are retained under `app/semantic_catalog/archive`, outside
the active catalog. Missing or invalid templates fail with
`TRINO_SOURCE_DEFINITION_INVALID`; there is no deployed-view fallback.

Templates use `{{shard}}.{{database}}.<base_table>` or the existing
`{{SHARD_CATALOG}}.dbonline.<base_table>` spelling. The renderer resolves the
caller shard and `TRINO_MYSQL_SCHEMA` (dbonline), checks that every physical
relation belongs there, and binds the caller identity. The supplied templates
own tenant and `on_eeaccess` predicates. No CREATE VIEW is executed or required.
Definitions are loaded for each query; rendered identity-bearing SQL is not cached.

Private SQL remains Git-ignored and excluded from the image. Mount it at runtime,
for example with the Helm chart's optional volume hooks:

```yaml
config:
  TRINO_MYSQL_SOURCES_DIR: /etc/trino-sources
extraVolumes:
  - name: trino-sources
    secret:
      secretName: private-trino-source-definitions
extraVolumeMounts:
  - name: trino-sources
    mountPath: /etc/trino-sources
    readOnly: true
```

In the Trino path, ClickHouse sources become
`TABLE(clickhouse.system.query(query => '<native SELECT>'))`. The native SELECT
reads `dbpcm_warehouse.<table>` with `final=1`, configured tenant/JTI settings,
and read-only resource limits. Trino does not accept ClickHouse SETTINGS on an
ordinary three-part table query. Configure the ClickHouse connector with
`clickhouse.map-string-as-varchar=true` so textual keys can join MySQL VARCHAR
keys; otherwise ClickHouse String maps to VARBINARY. See the connector's
[native-query contract](https://trino.io/docs/current/connector/clickhouse.html#query-varchar-table).

Queries whose sources are all allowed, catalogued ClickHouse tables retain the
direct ClickHouse route, including samples and EXPLAIN. Mixed-source queries use
Trino. Direct execution refreshes employee access and applies tenant settings;
failures do not retry through Trino. Service authentication and composite Trino
query identity remain unchanged.

Every MySQL execution connection sets
`<resolved_shard>.aggregation_pushdown_enabled=false`. Configure each MySQL
connector with `aggregation-pushdown.enabled=false` as the deployment default.
Trino plans the expanded source SELECT together with the caller query.

Local verification used isolated Trino 483, ClickHouse 25.3 and MySQL 8.0 with
synthetic data and the actual private accrual_events template. MySQL and mixed
queries returned different authorized employees for two users, isolated a second
tenant, and returned no rows for a quote-containing injection-shaped username.
ClickHouse returned the newest ReplacingMergeTree row with merges stopped,
confirming FINAL, and enforced tenant/JTI row policies through the native query.

The expanded queries retain textual tenant/employee access checks. Numeric
employee join substitutions, binary client keys, and native hex-literal SQL are
removed from the active path. MySQL collation clauses were converted to Trino
string comparisons; validate case, trailing-space, date, and numeric behavior
against the deployed source types before publishing. Invalid MySQL zero dates
must be handled by the JDBC source configuration before Trino can read them.

A view is planned together with its outer query, not automatically materialized
in full. Projection and supported predicate pushdown can reduce reads, but the
standard MySQL connector documents no CHAR/VARCHAR predicate pushdown. Tenant
and access predicates on strings can therefore be evaluated in Trino after
broader source scans. Authorization limits returned rows, not necessarily rows
read from MySQL. Confirm physical reads and aggregate placement with EXPLAIN and
controlled EXPLAIN ANALYZE; do not promise tenant-bounded transfer without evidence.
See [MySQL predicate pushdown](https://trino.io/docs/current/connector/mysql.html#predicate-pushdown-support)
and [Trino pushdown](https://trino.io/docs/current/optimizer/pushdown.html).

## Trino failure handling

Queries execute once. Execution or fetch failures discard partial results and close
the cursor and connection; there is no alternate join rewrite or backend retry.

The Python client's base TrinoQueryError carries INSUFFICIENT_RESOURCES errors;
these now receive sanitized resource guidance under TRINO_QUERY_ERROR rather than
a generic connectivity response. Current 483 names AMBIGUOUS_NAME and
EXPRESSION_NOT_AGGREGATE are recognized, along with overflow, stale/invalid views,
invalid policies and cancellation/capacity errors. Client authentication failures
remain TRINO_UNAVAILABLE with operator guidance; result-decoding failures become
TRINO_QUERY_ERROR with supported-type guidance. Raw connector messages remain
excluded.

## Deployment error logs

REST and MCP HTTP startup configure application logs on stdout with timestamps,
severity and logger names. ERROR remains visible even with LOG_LEVEL=CRITICAL.
Handled REST domain/HTTP/validation failures and MCP tool/auth/scratch failures
emit ERROR records containing operation, error code, status where available and
exception class. Trino readiness failures also
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
agree. ClickHouse-only queries retain direct ClickHouse execution.

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
ClickHouse-only queries retain direct ClickHouse execution.

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
`Nullable`, filtered by the request access scope. No live column discovery is
performed; physical schema mismatches fail during execution. This aligns schema
responses with the semantic validator and keeps one dialect for the model.
Use typed date bounds (`toDate('2026-01-01')` for Date inputs). A catalog type is
an authored contract, not proof of physical compatibility: deployments must still
check precision, scale, nullability, date ranges and timezone semantics against
actual source projections. Never fix a disagreement by silently rounding money,
dropping Nullable, or inventing a type for an unknown expression.

### Truncation validation and fractional literals

`dateTrunc`/`date_trunc` require a quoted constant unit and a known logical
date/timestamp input. The same pre-dispatch checks apply to typed SQLGlot
DateTrunc and TimestampTrunc nodes (including `toStartOfMonth` and `toMonday`).
Unknown units, untyped string inputs and timezone overrides are rejected locally.
The original unit token is checked before parsing because some SQLGlot versions
coerce column-like unit arguments into strings.

`toDate` and `toDateTime64` validate timezone-free ISO literals with 1–9 fractional
digits independently of Python's `fromisoformat` implementation. Calendar
validation uses a microsecond copy; emitted timestamp SQL preserves the original
fraction, truncated to the requested scale. This does not extend the separate
`toDateTime` contract to fractional literals or guarantee every typed SQL function
has a verified conversion.

### Calendar parts and Int32

`toHour`, `toMinute`, and `toDayOfYear` now use typed Trino calendar extraction,
retaining input nullability. Timezone overrides and untyped string/numeric date
inputs remain unsupported. `toLastDayOfMonth` preserves ClickHouse's default
Date result, including UInt16 day wrapping for out-of-range Date32 values; this
contract assumes `enable_extended_results_for_datetime_functions=0`.

`toInt32` uses the existing verified numeric domain for `toInt64`, truncates
fractions toward zero, and wraps to signed 32 bits. It is not a plain INTEGER cast:
ClickHouse wraps overflow whereas Trino rejects it. String parsing and wider or
unverified numeric domains remain unsupported.

Standalone `toRelativeDayNum` remains rejected because ClickHouse documents its
origin as an implementation detail. Use `dateDiff('day', start, end)` when the
intent is a calendar-day difference; do not substitute an invented epoch.

### Imported blueprint execution coverage

The imported corpus has 188 executable blueprints; five definitions that require
unavailable source columns remain archived. The local repair matrix covers 810
slot variants (824 node executions), including ASC/DESC, optional filters and
composite scratch steps. See
[`verification/blueprint-execution-repairs-2026-10-28.json`](verification/blueprint-execution-repairs-2026-10-28.json)
for results and fixture limitations. These checks establish local executability,
not production schema parity or correctness of every HR interpretation.

Verified conversions now include multi-input `uniqExact` (excluding rows with
any NULL argument), `stddevPop` with nullable/empty-input semantics, integer
`range` with an exclusive upper endpoint, integer `toUInt32`, and
`toIntervalMonth` used in date arithmetic. Decimal/floating-point `toUInt32` remains
rejected because its overflow semantics are not portable. Hour/minute/second
`dateDiff` counts unit boundaries and preserves nanosecond input precision.

Expression normalization can duplicate scalar subqueries. Every copied source
is routed using its validated source metadata so physical expansion and identity
filters remain attached. Large employee KPI/DQ blueprints use one employee scan
instead of repeated expanded views. Empty date-window anchors explicitly return
NULL rather than subtracting months from ClickHouse's empty MAX date default.

Native ClickHouse can omit column headers on an empty result. The regular query
path recovers them with a metadata-only `DESCRIBE TABLE (query)` using the same
parameters and read-only settings; it does not rerun the data query. This permits
empty composite results to retain their columns during scratch materialization.

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

### Session scratch tables

The session catalog reports `scratch_database` from API settings; the agent uses
that value instead of configuring a physical namespace. Scratch data is still
materialized in ClickHouse. With Trino enabled, consumers
read `dbpcm_scratch.s_<session>_*` through the configured ClickHouse connector,
including joins to MySQL-backed tables. The API checks session ownership before
metadata access or execution; scratch types are inspected live, not added to the
shared semantic catalog. New user uploads (`s_<session>_upload_*`) are advertised
through `listTables` only to their owning session. Blueprint intermediates
(`s_<session>_bp_*`) stay hidden; the executor binds them directly into dependent
DAG steps. Explicit schema inspection still checks session ownership. Legacy
uploads used the same `bp_*` pattern as intermediates and cannot be classified
from their names; re-upload them to make them discoverable.

Enable `dbpcm_scratch` in `ALLOWED_DATABASES`. The scratch writer and Trino's
ClickHouse connector must reach the same scratch tables; the connector user needs
`SELECT ON dbpcm_scratch.*`. Keep existing scratch writer privileges and TTL
configuration. Blueprint templates retain `scratch.<output>` placeholders; the
agent substitutes the fully qualified name returned by materialization.

## Connected Phoenix tracing

The data agent propagates the active tool span in W3C `traceparent`/`tracestate`
headers on MCP and REST side-channel calls. The API extracts only trace context,
not baggage, and restores it after each request. Trace headers never grant access.

Set `OTLP_ENDPOINT=http://phoenix:6006/v1/traces` on clickhouse-api (the full
OTLP/HTTP URL, not the agent's gRPC endpoint). Set `OTLP_PROJECT_NAME` to the same
project as the agent; the default is `data-agent-runtime`. Keep distinct service
names (`OTLP_SERVICE_NAME=clickhouse-api`). Export uses a bounded background batch
queue; an unavailable collector does not delay query execution waiting for export.
Standard `OTEL_EXPORTER_OTLP_TRACES_HEADERS` can supply collector authentication.

The trace tree is `agent tool → trino.execute → trino.convert`. Conversion records
logical SQL as input and rewritten Trino SQL as output, including rejected
conversions with a bounded error code. Execution records the diagnostic correlation
ID and coordinator query ID. Execution duration includes conversion and result
fetching (or streaming); conversion has its own duration. No result rows, raw JWTs,
exception messages or stacks are exported. Authenticated session IDs link spans.

`OTLP_DISABLE_REDACTION=false` masks SQL literals and removes comments. Setting it
true records SQL verbatim, including injected tenant settings; SQL attributes are
bounded to 32 KiB. Local restricted diagnostic files retain their existing redaction
regardless of this Phoenix flag. With no incoming trace context, API work starts
its own trace. Calls made without an active agent span cannot be attached to an
older turn merely by session ID.
