# Experimental Trino backend

`TRINO_ENABLED=false` (default) preserves the existing ClickHouse path. Set it
to true to use Trino for runQuery, explainQuery and sampleRows. Schema discovery
and column provenance then use the authored semantic catalog, without querying
ClickHouse `system.columns`. Logical names and the existing column-scope checks
stay unchanged. This is a foundation for federation, not a completed migration
of employee/expense SQL or production row policies.

## Catalog and routing contract

Each `app/semantic_catalog/data/*.yaml` table declares:

```yaml
database: dbpcm_warehouse
table: employee
source: mysql
```

`source` accepts `mysql` or `clickhouse`; omitted means `clickhouse` for backwards
compatibility. It only changes routing when Trino is enabled. Payroll,
total_earning and punch_pair are marked ClickHouse; all other current tables are
marked MySQL. The catalog export carries this metadata and the fingerprint changes.

`app/shard_routing.py::resolve_mysql_shard(proc_center, client_code, settings)` is
the replaceable routing boundary. For now it uses an explicitly configured map:

```dotenv
TRINO_SHARD_ROUTES={"pc1":{"10R37":"mysql_shard_07"},"pc2":{"10R37":"mysql_shard_08"}}
```

Both values come from verified JWT claims. Missing claims, unknown tenants and
invalid identifiers fail closed; there is no fallback shard. Replace the function
body with the real routing service later. No user/tenant identity lookup table is
created by this implementation.

Trino uses three-part physical names. By default:

```text
employee                   -> mysql_shard_07.dbonline.employee
dbpcm_warehouse.department -> mysql_shard_07.dbonline.department
dbpcm_warehouse.payroll    -> clickhouse.dbpcm_warehouse.payroll
```

`TRINO_MYSQL_SCHEMA` changes `dbonline`. To route MySQL-backed logical tables to
secured Trino views instead, set `TRINO_MYSQL_VIEW_CATALOG=reporting`:

```text
employee -> reporting.mysql_shard_07.employee
```

That catalog must support view storage and be provisioned separately. Merely
generating a MySQL connector catalog does not create a Trino view store.
`TRINO_CLICKHOUSE_CATALOG` selects the ClickHouse-backed catalog (default
`clickhouse`); it can also name a provisioned secured view catalog with matching
schema/table names. All targets must expose the normalized column names and grain
declared in the YAML. The legacy MySQL employee query is NOT installed/converted
by this change; raw `eecode` does not magically become `employee_code`.

The boundary parses existing logical SQL as ClickHouse SQL, qualifies aliases,
resolves only actual base tables (not CTE references), and emits Trino SQL. CTEs,
self joins, subqueries and unions are supported. Physical catalog references,
uncatalogued sources, table functions, scratch reads, SHOW/DESCRIBE, EXPLAIN ANALYZE,
and translations SQLGlot cannot support are rejected. Use the metadata tools
instead of SHOW. Logical column types remain catalog-authored ClickHouse types;
this does not yet provide a general native-Trino SQL interface.

## Identity and row security

The adapter builds the effective Trino user from three verified claims:

```text
proc_center|clientcode|username
pc1|10R37|aabusaad
```

Claim names default to `proc_center`, `clientcode`, and `user_name`, configurable
with `TRINO_PROC_CENTER_CLAIM`, `TRINO_CLIENT_CODE_CLAIM`, and
`TRINO_USERNAME_CLAIM`. Missing/blank/oversize claims, delimiters and control
characters are rejected. No second JWT is minted. If a service password is set,
the adapter authenticates as `TRINO_USER` using HTTPS and requests this effective
user via Trino impersonation. Provision restricted impersonation rules allowing
only the backend to use these identities; prevent reporting identities from
changing identity or reading raw data outside secured views/policies. Connections
are per execution and closed/cancelled after completion or failure.

Secured views may read `CURRENT_USER` and split fields 1/2/3 for processing center,
client and username. This identity format is not automatically recognized by
MySQL or ClickHouse, and sending it alone enforces NO source row security.

**`TRINO_SECURITY_READY` defaults to false.** Business execution is rejected until
the deployment explicitly enables it after provisioning and validating tenant,
employee and column protection in the Trino/source layer. This check exists
because a connector switch must not silently discard the existing ClickHouse
`paycom_*` / `getSetting()` row policies. This adapter does NOT forward those
settings or refresh the ClickHouse employee-access snapshot. If those policies
remain the security mechanism, implement their transport/refresh integration
before using this path. Provisioning full policies is outside this initial layer.

The old ClickHouse path still injects its settings and refreshes access as before.
The adapter does not silently fall back to ClickHouse on a Trino failure.

Result row/byte caps and a Trino `query_max_run_time` session limit are applied.
Backend errors are returned without connector SQL/credentials. Trino cluster
resource groups and source scan limits need deployment configuration;
ClickHouse's `max_rows_to_read` has no direct adapter equivalent here. Confirm
string/tenant/username pushdown with EXPLAIN and MySQL source logs before assessing
index use. Do not assume the complex employee view, its joins or aggregation are
fully pushed down.

## Local coordinator and shard catalogs

This compose file is standalone and leaves the existing ClickHouse compose stack
unchanged. It runs a single Trino 483 coordinator on localhost port 18081. It has
no authentication and is for local development only, never public deployment.

```bash
# Edit/copy the example inventory first to set your source endpoints.
python scripts/setup_trino_catalogs.py --inventory docker/trino/shards.example.json
docker compose -f docker-compose.trino.yml up -d
docker compose -f docker-compose.trino.yml exec trino trino --execute 'SHOW CATALOGS'
```

The generator creates one `mysql_shard_XX.properties` per inventory entry and an
optional ClickHouse connector. It makes no database connections or changes. Files
go under ignored `docker/trino/catalog/*.properties`. Re-running refuses to
overwrite unless `--overwrite` is supplied; unrelated/stale files are not deleted.
Restart Trino after regeneration. `--output-dir` supports another deployment path.

Source credentials are `${ENV:...}` references, not literal secrets in generated
files. Override `docker/trino/defaults.env` by creating the ignored `.env.trino`:

```dotenv
TRINO_MYSQL_USER=reporting_reader
TRINO_MYSQL_PASSWORD=replace-locally
TRINO_CLICKHOUSE_USER=reporting_reader
TRINO_CLICKHOUSE_PASSWORD=replace-locally
```

Each inventory entry may specify `user_env` / `password_env` for per-shard
credentials; add those variables to `.env.trino`. The generator currently accepts
plain host/port endpoints; adapt its connection URL construction for source TLS,
failover and your real inventory. Use read-only source accounts. The example
points at host ports 3306/3307/8123 and does not start those databases.

For an MCP running on the host, configure:

```dotenv
TRINO_ENABLED=true
TRINO_HOST=localhost
TRINO_PORT=18081
TRINO_HTTP_SCHEME=http
TRINO_USER=mcp_backend
TRINO_PASSWORD=
TRINO_SHARD_ROUTES={"pc1":{"10R37":"mysql_shard_07"}}
TRINO_MYSQL_SCHEMA=dbonline
TRINO_SECURITY_READY=false
```

Keep the last setting false until equivalent security is provisioned. For
production use HTTPS, backend credentials and restricted impersonation. A
containerized MCP must use a reachable Trino hostname/port instead of localhost.
Normal OIDC verification at the MCP boundary is still required.

Trino mode does not require ClickHouse connection settings for query execution.
The separate scratch/upload subsystem remains ClickHouse-specific and is not
supported as a Trino report source. MCP health advertises scratch as unsupported
in this mode; do not enable scratch/upload workflows for this experimental path.
Health probes check Trino connectivity, not security readiness or source view
existence. Schema discovery is the declared contract, not proof that physical
relations have been deployed.

## Copyable minimal employee view

This example uses the existing MySQL `employee`, `cldepts`, and `on_eeaccess`
tables. It exposes only the columns needed for a department-count demo, not the
complete employee catalog. Queries for other catalog columns (including a full
`sampleRows` projection) require extending the view first.

Prerequisites: provision a view-capable Trino catalog named `reporting` (for
example, a Hive connector backed by a metastore), and configure the
`mysql_shard_07` source catalog. The compose file and catalog generator above do
not provision this view store. Run the following SQL through an administrative
Trino CLI/SQL connection, not through the read-only MCP API. Use an unused demo
schema or adapt the names if this view already exists.

The example assumes processing center `pc1` owns this source's employee data.
Change that literal and the physical catalog name for your deployment. If a
physical source contains overlapping client codes across processing centers,
also constrain the actual source processing-center column or choose a source
schema that isolates the processing center; a caller-identity check alone cannot
disambiguate those rows.

```sql
CREATE SCHEMA IF NOT EXISTS reporting.mysql_shard_07;

CREATE VIEW reporting.mysql_shard_07.employee
SECURITY DEFINER
AS
SELECT
    e.clientcode AS client_code,
    'pc1' AS proc_center,
    e.eecode AS employee_code,
    e.eename AS employee_name,
    e.deptcode AS department_code,
    d.deptdesc AS department_name
FROM mysql_shard_07.dbonline.employee e
LEFT JOIN mysql_shard_07.dbonline.cldepts d
    ON d.clientcode = e.clientcode
   AND d.deptcode = e.deptcode
WHERE split_part(CURRENT_USER, '|', 1) = 'pc1'
  AND e.clientcode = split_part(CURRENT_USER, '|', 2)
  AND EXISTS (
      SELECT 1
      FROM mysql_shard_07.dbonline.on_eeaccess a
      WHERE a.clientcode = split_part(CURRENT_USER, '|', 2)
        AND a.username = split_part(CURRENT_USER, '|', 3)
        AND a.clientcode = e.clientcode
        AND a.eecode = e.eecode
  );
```

`EXISTS` avoids multiplying employees when access rows are duplicated. This
example assumes `employee` is unique on `(clientcode, eecode)` and `cldepts` is
unique on `(clientcode, deptcode)`. The client and username comparisons leave
source columns unwrapped, making them candidates for indexed filtering; verify
actual pushdown and index use with Trino EXPLAIN and the MySQL execution plan.

Trino's `CURRENT_USER` is the caller even in a `SECURITY DEFINER` view; the
definer's permissions are used to access underlying tables. Configure access
control so reporting callers can read secured views and cannot bypass them via
raw tables, and only the backend can impersonate the encoded identities. See
[Trino CREATE VIEW security](https://trino.io/docs/current/sql/create-view.html#security).

In the API environment, in addition to the connection settings above:

```dotenv
TRINO_ENABLED=true
TRINO_MYSQL_VIEW_CATALOG=reporting
TRINO_SHARD_ROUTES={"pc1":{"10R37":"mysql_shard_07"}}
```

Keep `TRINO_SECURITY_READY=false` until the deployment's security is configured
and validated, then set it to true to permit execution. Restart the API after
changing its environment. With verified JWT claims `proc_center=pc1`,
`clientcode=10R37`, and `user_name=aabusaad`, the adapter sends the effective
Trino user `pc1|10R37|aabusaad`; no second JWT or identity lookup is needed.

Submit this logical SQL to the MCP `runQuery` tool:

```sql
SELECT department_code, department_name, COUNT(*) AS employee_count
FROM dbpcm_warehouse.employee
WHERE department_name = 'dev'
GROUP BY department_code, department_name;
```

The API rewrites the FROM target to `reporting.mysql_shard_07.employee`. The view
adds the client and employee-access restrictions internally. For a direct CLI
smoke check against the local unauthenticated development coordinator, use:

```bash
docker compose -f docker-compose.trino.yml exec trino trino \
  --user 'pc1|10R37|aabusaad' \
  --execute "SELECT department_code, department_name, COUNT(*) AS employee_count FROM reporting.mysql_shard_07.employee WHERE department_name = 'dev' GROUP BY department_code, department_name"
```

This CLI command tests the view directly and does not test API JWT verification.
Expect only employees permitted by `on_eeaccess` for that client and username;
the result can be empty depending on source data.

`TRINO_MYSQL_VIEW_CATALOG` applies to **all** tables marked `source: mysql`.
Create corresponding views before querying those tables. This minimal example
does not install the full employee query, other MySQL views, or ClickHouse row
policies, and it does not change discovery: metadata still lists the authored
YAML tables and columns.

## Verification

```bash
python -m pytest -q tests/test_trino.py
python tools/check_catalog_parity.py
docker compose -f docker-compose.trino.yml config --quiet
```

The automated suite checks route isolation, aliases/CTEs/unions, mixed-source
queries, security gating, metadata, bounded execution and catalog generation.
Live source integration additionally needs your views, credentials, MySQL index
plans, Trino authorization and ClickHouse policy setup.
