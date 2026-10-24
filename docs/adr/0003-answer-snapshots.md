# ADR-0003: Answer snapshots — frozen full-result exports on S3-compatible storage

**Status:** Accepted
**Date:** 2026-10-22
**Supersedes/relates:** extends ADR-0001 (per-tenant JWT row isolation) unchanged;
the snapshot guardrail chain reuses ADR-0002's shared `_enforce_query_guardrails`.
Ticket: V4-0101010. Full styled plan: `docs/plans/answer-snapshot-export.html`.

---

## Context

The data-agent UI paged query results by RE-EXECUTING the SQL for every page
(`SELECT * FROM (<sql>) LIMIT n OFFSET m`), and exported by looping the client
over those pages with a hard 5000-row cap.  Consequences: every page is a fresh
query (cost, and no consistency between pages or between screen and file), the
UI's "N results" byline was a growing estimate with no true total, and exports
were silently truncated.

Locked product requirements:

1. The export file always contains ALL the data — or is delivered as ordered
   downloadable parts.
2. The frontend pages from a server-side snapshot, never by re-running the query.
3. Data on the UI must EXACTLY match the export file (same snapshot).
4. "Refresh data" is the ONLY way data changes.
5. A new answer always pulls fresh data — byte-identical SQL mints its own
   snapshot.

---

## Decision

1. **Separate export path; interactive `/query` untouched.**  New REST routes
   `POST /query/snapshots`, `GET /query/snapshots/{id}/page`,
   `GET /query/snapshots/{id}/download-urls`, registered only when
   `EXPORT_ENABLED=true`.  No new MCP tool — REST-only surface.

2. **Same guardrails, one deliberate exception.**  `create_snapshot` runs the
   identical chain as `run_query` (`validate_and_sanitize` →
   `_enforce_query_guardrails` → `ensure_employee_access_fresh` → execute),
   except validation is called with the new keyword-only `inject_limit=False`:
   the snapshot materializes the FULL result set, so the injected
   `DEFAULT_LIMIT` is never appended.  Every other validation transform is
   byte-identical.

3. **Fail loud, never silently truncate.**  The export streams with its own
   caps bundle (`app/clickhouse_client.export_settings`) whose values derive
   EXCLUSIVELY from `EXPORT_*` config — never merged with the operator's
   interactive `MAX_*` overrides — with `result_overflow_mode='throw'`
   (interactive uses 'break').  Budgets (`EXPORT_TOTAL_MAX_ROWS` /
   `EXPORT_TOTAL_MAX_BYTES`, defaults 1M rows / 100 MB) are enforced LOCALLY
   per streamed row by `app/export_service.py`; an over-budget mint aborts,
   deletes any partial objects, and raises 400 `EXPORT_TOO_LARGE` with a
   message advising a narrower WHERE/LIMIT.  ClickHouse error text is never
   parsed to detect overflow — the CH-side `max_result_rows` cap deliberately
   sits ONE row above the row budget so the local counter always trips first
   (both sides count the same streamed rows), making the domain error
   authoritative.  CH-side byte cap is a coarse backstop only (serialized
   result bytes ≠ staged file bytes).

4. **Auto-split CSV parts; numbered, standalone, header-bearing.**  Parts split
   at `EXPORT_MAX_ROWS` rows OR `EXPORT_PART_MAX_BYTES` per part (defaults
   100k / 10 MB), whichever trips first; every part carries the header row so
   it is a valid CSV alone, and header bytes count toward the part cap.  The
   download route returns the ordered presigned-URL list
   (`{part, url, rows, filename}` — `query-export-part-NNNN.csv`, signed with
   `ResponseContentDisposition`, lifetime `EXPORT_PRESIGN_TTL_SECONDS` default
   300s).  A single streamed row larger than the part cap is written anyway
   (a row cannot be split); the total budget backstops that pathology.

5. **Object layout + commit point.**  Under
   `{EXPORT_S3_PREFIX}/{tenant_hash}/{snapshot_id}/`: `rows.jsonl.gz` (gzip
   JSON Lines, one JSON array of REST-wire values per line — paging source of
   truth), `data.part-NNNN.csv` part(s), then `meta.json` written LAST (the
   commit point: a snapshot only "exists" once meta.json lands).  `meta.json`
   carries `{snapshot_id, tenant_hash, columns: [{name, type}], row_count,
   created_at, expires_at, parts, sql_sha256, source_sql, access_hash}` — SQL is
   retained privately for revalidation/rebuild. Metadata is never exposed raw to callers.  Staging is
   local temp files (uploads start only after the stream completes), so an
   aborted mint leaves no orphan objects.

6. **Tenant binding = tenant-hash equality; expiry = app-side clock.**  The
   `tenant_hash` is the full sha256 of canonical JSON (sorted keys) of the
   per-request tenant settings dict (`clickhouse_client.tenant_settings()`).
   Page/download recompute it from the CURRENT request and demand equality —
   a foreign-tenant id returns 404 `SNAPSHOT_NOT_FOUND`, wire-indistinguishable
   from an unknown id (existence is never probeable across tenants).  TTL
   (`EXPORT_TTL_SECONDS`, default 4h) is enforced app-side on every read → 410
   `SNAPSHOT_EXPIRED`; the bucket lifecycle rule (~1 day) is only the storage
   backstop.  Snapshot ids are server-minted UUIDv4 (never derived from SQL —
   refresh mints a fresh id) and are shape-checked before use in object keys.
   RLS/row-policy freshness is evaluated once at MINT time
   (`ensure_employee_access_fresh`); page/download verify owner/session, current grants +
   expiry.  **Flagged for security review.**

7. **Exact counts end the trailing empty fetch.**  Page responses are the
   existing compact shape PLUS `row_count`, `snapshot_id`, `expires_at`;
   `has_more` is EXACT (`offset + len(rows) < row_count`), and
   `offset >= row_count` is a 200 with `rows: []`, not an error.

8. **Value fidelity.**  Rows are NUL-padding-cleaned exactly like the MCP path
   (`rstrip('\x00')` on strings incl. nested) and encoded with FastAPI
   `jsonable_encoder` semantics (datetime/date → ISO strings, Decimal →
   number, tuples → arrays) so snapshot rows are value-identical to what REST
   `/query` returns today.  CSV carries canonical values ("" for null,
   `true`/`false` for booleans, compact JSON for composites); display
   formatting stays UI-side.

9. **Storage + SDK.**  On-prem S3-compatible targets (Dell ECS / NetApp
   StorageGRID): pinned `boto3==1.43.33`, path-style addressing (mandatory on
   those appliances), SigV4 presigning (botocore presigns legacy SigV2 unless
   pinned), credentials from env or Vault (`EXPORT_S3_CREDS_PATH`, keys
   `EXPORT_S3_ACCESS_KEY` / `EXPORT_S3_SECRET_KEY` — the SCRATCH_CH vault
   group pattern; least-privilege PUT/GET on the prefix).  boto3's default
   managed checksum behavior is left untouched (vendor-supported default).  With
   `EXPORT_ENABLED=true`, an incomplete S3 identity fails startup LOUD in the
   lifespan (`export_s3_missing_fields()`), mirroring the auth fail-closed
   pattern.

10. **Streaming execution.**  `execute_query_rows_stream` keeps the
    clickhouse-connect `QueryResult` (column names + types) while iterating
    rows lazily — the public `query_rows_stream` discards it — and reuses
    `_append_settings_clause` with the exact same tenant/CHProxy flag logic as
    `execute_query` (RLS `rls_*` binds + caps merge into one SQL-body SETTINGS
    clause; `readonly` never enters the SQL body; literal-`%` escaping
    unchanged).

---

## Consequences

**Positive:**
- UI and export always agree (same frozen bytes); refresh is the only data
  change vector; exact totals from first paint.
- One expensive scan per answer instead of one per scroll page.
- No silent truncation anywhere: over-budget mints fail with a caller-fixable
  400; downloads always carry the full result set.
- Zero regression risk to the interactive path: new routes, new caps, new
  config — all off by default (`EXPORT_ENABLED=false`).

**Negative / risks:**
- Snapshots are server-side state with a TTL: storage cost lives on the
  bucket until lifecycle reaps; an abandoned snapshot set is bounded by the
  prefix + lifecycle rule, and app-side cleanup on abort is best-effort.
- RLS freshness frozen at mint (decision 6) means a permissions change during
  the TTL window does not affect an existing snapshot — flagged for security
  review; TTL bounds the window.
- Paging a row at offset N costs O(N) stream-decompressed reads of
  `rows.jsonl.gz` (no index file); the 1M-row budget bounds this, and offsets
  arrive in increasing scroll order in practice.
- The export path's CH `max_result_rows = EXPORT_TOTAL_MAX_ROWS + 1` coupling
  (decision 3) is load-bearing for the error contract: keep the one-row
  headroom if the bundle is ever retuned.

---

## Alternatives Considered

**Interactive-path reuse with bigger caps (rewrite `/query` limits):** Rejected.
Weakens the interactive guardrails for a non-interactive use case and still
re-executes the query per page (no UI/export consistency).

**Server-side cursor / ClickHouse named sessions:** Rejected. Stateful
ClickHouse sessions preclude horizontal scaling and leak between deploys;
object storage is the cheap, HA home for frozen result sets.

**TOO_LARGE detection via ClickHouse error text:** Rejected. Message strings
are not a stable contract across CH versions; the budget must be enforced
app-side with deterministic row/byte counters.

---

## Addendum (V4-0101010 follow-up): pluggable snapshot store — Couchbase default

**Date:** 2026-10-23 · Amends decisions 5 (object layout) and 9 (storage + SDK)
and §1's download transport.  S3 mode is RETAINED unchanged behind
`EXPORT_STORE=s3`; everything else in this ADR still holds.

1. **Pluggable store, Couchbase the default.**  `EXPORT_STORE=couchbase|s3`
   (default `couchbase`) selects where snapshot artifacts live.  The layout,
   split math, caps, budgets, tenant-hash binding, app-side TTL, and guardrail
   chain are IDENTICAL in both modes; only the storage mechanics and the
   download transport differ.  Both SDKs are LAZY-loaded — an s3-mode deployment
   never imports the couchbase SDK and a couchbase-mode deployment never
   imports boto3.

2. **Couchbase doc layout** (dedicated bucket, doc ids shown for
   `snap::{tenant_hash}::{snapshot_id}::`):
   - `meta` — JSON object, same fields as `meta.json` (+ `chunk_rows`);
     written LAST (commit point).
   - `rows.{chunk:04d}` — JSON array of wire rows, 0-indexed,
     `EXPORT_CB_CHUNK_ROWS` (default 10_000) rows per chunk; written DURING
     streaming.  Paging fetches chunk doc(s) by `offset // chunk_rows`
     ceil-math and slices in memory — O(1) doc reads, no stream-scan.
   - `csv.{part:04d}` — binary CSV part bytes, 1-indexed, same part-split math
     as S3 mode.
   Every write carries a per-doc `expiry` of `EXPORT_TTL_SECONDS`; the app-side
   TTL check stays authoritative.

3. **Download transport in Couchbase mode.**  There is nothing to presign, so
   `GET /query/snapshots/{id}/download-urls` returns RELATIVE app paths
   (`query/snapshots/{id}/download?part=N`) with `expires_in: null`, and a new
   route `GET /query/snapshots/{id}/download?part=N` streams the stored part
   bytes as `text/csv` with a `Content-Disposition` attachment filename.
   Per-request JWT authz (plus the usual tenant-hash/expiry checks) replaces
   the presign window; an out-of-range part is 404 `SNAPSHOT_NOT_FOUND`.

4. **Startup gate.**  Only the ACTIVE store's identity is gated:
   `EXPORT_ENABLED=true` + `couchbase` requires conn string + bucket + username
   + password; + `s3` keeps the existing four-field gate.  Credentials follow
   the Vault group pattern (`EXPORT_CB_CREDS_PATH` → `EXPORT_CB_USERNAME` /
   `EXPORT_CB_PASSWORD`), mirroring `EXPORT_S3_CREDS_PATH`.

5. **Ops note (Couchbase provisioning).**  Provision a DEDICATED bucket for
   snapshot docs (e.g. `agent_snapshots`): `replicas=0` (ephemeral cache-class
   data), bucket **maxTTL = 14400s** (= `EXPORT_TTL_SECONDS`) as the
   datastore-level TTL backstop under the per-doc expiry.  No query index is
   required — all access is KV (get/upsert/remove + prefix scan for cleanup).

**Why Couchbase by default:** the snapshot set is ephemeral KV-shaped state
(id → bounded bytes) with a hard TTL; a KV bucket with per-doc expiry is
operationally lighter than presigned object-store plumbing (no signature
machinery on the hop chain) and keeps the download under per-request authz
end-to-end.  S3 mode remains available where an object store is preferred.

**Addendum (2026-10-23):** the populated Vault record at `EXPORT_CB_CREDS_PATH`
carries the ops key names `COUCHBASE_USERNAME` / `COUCHBASE_PASSWORD` /
`COUCHBASE_CONNECTION_STRING` / `COUCHBASE_BUCKET_EXPORT`, so the loader reads
those (bucket always from `COUCHBASE_BUCKET_EXPORT`, never `COUCHBASE_BUCKET`);
the `EXPORT_CB_*` names remain the env-var surface only.

## Trino integration in this branch

The endpoints are available on both the REST app and the MCP HTTP server when
`EXPORT_ENABLED=true`. Requests require the existing bearer token,
`X-Session-Id`, and `X-Catalog-Scope`; the verified session also authorizes any
scratch references. These are HTTP endpoints, not additional model tools.

Minting follows the same routing as `runQuery`: known ClickHouse-only queries
use the native client; MySQL, mixed-source, and owned scratch queries stream
through Trino. Trino retains service authentication, caller impersonation,
shard resolution, MySQL access predicates, and ClickHouse row policies. Export
budgets replace interactive result/time caps; scan limits remain unchanged.
No default LIMIT is added, but explicit SQL LIMITs are honored. Aborting an
export closes/cancels its query. Trino diagnostics cover the full stream.

Snapshot ownership includes the Trino caller identity as well as ClickHouse
identity settings. Paging/downloads read stored artifacts without re-running
SQL. Decimal values retain their exact string representation, matching REST
query responses; NaN/Infinity are rejected. Couchbase CSV documents explicitly
use binary encoding for both writes and reads. Its physical TTL may remove the
metadata entirely, in which case the response is 404; 410 is returned when
metadata remains and the application expiry has passed.

The agent/UI must explicitly call these endpoints to opt into frozen paging;
this change does not replace its existing query-paging flow automatically.


## Concurrency review

Creation is admitted before taking a shared HTTP worker. The per-worker limit
is `EXPORT_MAX_CONCURRENT_CREATES` (default 4, maximum 32); excess requests get
429 `EXPORT_BUSY` with `Retry-After`. Capacity multiplies by worker and replica
count; this is not a distributed admission limit. Store initialization is
serialized on cache misses, and S3 uploads do not start nested transfer pools.

The UI must retain one snapshot ID per answer-table version and use GET paging
for scrolls. Identical creation requests automatically reuse their snapshot
within the same verified identity, session and access scope.

Couchbase supports direct chunk lookup. The current S3 format scans gzip JSONL
from the beginning of each page; it needs indexed/chunked row objects for heavy
interactive paging. Fixed-row Couchbase chunks also need byte-aware sizing for
wide results to stay within its document-size limit. Neither limitation is
resolved by the creation admission limit.


## Idempotent creation

Creation is automatically idempotent; no caller-provided key is accepted by the
API contract. The claim address hashes the verified query identity, subject,
session, exact SQL text, and schema/column grants. SQL is currently the entire
request body; future result-affecting options must also enter this fingerprint.
Neither raw JWT nor SQL is stored in the claim. Token expiry/signature changes
alone do not affect reuse, but row-access `jti` changes must isolate results.
Whitespace changes in SQL produce a separate request; SQL is not rewritten for
fingerprinting. Data changes alone do not invalidate a frozen snapshot, even
for relative-time SQL (`now()`/`today()`). Explicit refresh supplies `refresh_of` (previous snapshot UUID), also included
in the request fingerprint. Concurrent/retried refreshes of the same snapshot
reuse one replacement. Each next refresh references the current replacement.

All replicas use the selected snapshot store: Couchbase atomic insert/CAS or
S3 conditional PUT (`If-None-Match: *` / `If-Match`). There is no unconditional
write fallback if an S3-compatible appliance lacks conditional-write support.
See [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
and [Couchbase CAS](https://docs.couchbase.com/python-sdk/current/howtos/concurrent-document-mutations.html).

- Completed retry: the same 201 response and snapshot ID, without SQL or live
  schema queries. Metadata is the commit point, checked before claim status.
- Pending retry: 409 `SNAPSHOT_IN_PROGRESS`, `Retry-After: 2`.
- Changed SQL, identity, session or grants: a separate snapshot.
- Known failed creation: 409 `SNAPSHOT_CREATION_FAILED` until expiry. A new
  session has a separate fingerprint. No automatic re-execution of failed requests.
- Admission overload can still return 429 `EXPORT_BUSY`; retry the same request.

Claims expire after `EXPORT_TTL_SECONDS` measured from initial admission.
Replays/status updates do not extend expiry. After the retention window a key
can start a new generation. Configure
Couchbase maxTTL and S3 lifecycle retention to retain claims for at least this
window. Snapshot expiry never exceeds its claim deadline.

An unfinished claim is never taken over before expiry: a crashed creator may
already have executed SQL. A request can therefore remain pending after a
worker crash until expiry (or a new session). This favors preventing
unintended repeated queries over automatic recovery of uncertain executions.
A delayed old worker cannot overwrite a newer generation because updates use
CAS/ETag, and each generation has a distinct snapshot ID.

If publication acknowledgement is lost, artifacts are retained: a retry can
recover committed metadata even when claim status is pending or failed.
Failures before metadata publication clean up normally; uncertain publication
artifacts without metadata remain unreadable and expire through store retention.
The claim mechanism assumes the store preserves acknowledged writes and honors
conditional-write semantics; it is not a guarantee across datastore data loss.


## Ember integration (2026-10-24)

Creation remains synchronous (201). No asynchronous worker/status endpoint was
introduced. The agent proxies the four snapshot routes using the explicit
`CLICKHOUSE_API_REST_BASE_URL`, JWT, session ID, and a server-obtained
`X-Catalog-Scope` from its SessionCatalog. Missing configuration returns 501;
there is no unscoped fallback. Retry-After is preserved for 409/429. JSON bytes
and statuses are preserved; CSV downloads stream without buffering the file.

Ownership includes subject, verified session, schema/column grants and effective
query identity. Page, presign, stream, and refresh all check it in clickhouse-api;
Ember's ownership check is not the security boundary. A foreign session gets the
same 404 as an unknown ID. Old snapshots minted under caller-only ownership are
not served through a weaker legacy fallback; recreate them after deployment.
Already-issued S3 URLs remain usable until their signed expiry; authorization is
checked when issuing the URL, not on each browser-to-S3 download.

Ember refresh must call POST /query/snapshots with
`{"sql":"...","refresh_of":"<currently displayed snapshot UUID>"}` and retain
that source ID for retries. Switch the displayed snapshot only on success.
Repeating SQL alone is ordinary creation and may reuse the old snapshot.
Expired source metadata can authorize refresh while retained. If storage has
already removed it (404), discard the stale reference and perform ordinary
creation under the current authorized session. Changed grants trigger the scope recovery described below; old rows are never
served under different grants.

The proxy create read timeout is 150 seconds, reads 60, and CSV streaming 120.
Gateway/BFF timeouts must allow synchronous creation plus authentication/catalog
setup. A disconnect does not prove execution failed; retry the identical request.
S3/Couchbase store selection is unchanged; this change does not implement hybrid
Couchbase coordination with S3 files or asynchronous jobs.


## Scope-change recovery (2026-10-24)

Storage addresses use a stable owner hash (subject, session and effective query
identity), separate from the saved access hash (including schema/column grants).
Foreign owner/session remains 404. An owned snapshot with different grants
returns 409 SNAPSHOT_SCOPE_CHANGED on page, presign or streaming reads before
old data is served. Changes to query identity itself still produce 404.

POST /query/snapshots/{id}/rebuild loads private source_sql only after ownership
validation, then uses current permissions and normal query guardrails to create
an idempotent replacement. It accepts no caller SQL and never returns source_sql.
Explicit refresh_of also permits changed grants only to re-execute the query;
it cannot read the old rows. Stored SQL may contain sensitive literals and must
have the same restricted storage access/retention as snapshot data. No metadata
URLs are presigned. Old metadata without this ownership layout cannot be migrated
implicitly; recreate those snapshots.

On SNAPSHOT_SCOPE_CHANGED the agent forces SessionCatalog.ensure(force=True)
and makes one rebuild request with the new signed catalog scope. It does not
retry loops or serve an old offset from the new result. Success is returned as:
409 {detail: {code: SNAPSHOT_REPLACED, error: ...}, snapshot: <create response>,
reset_table: true}. Ember must clear old rows, persist the replacement metadata,
and reload from offset 0. Download actions must restart against the replacement.
Failed rebuilds preserve the backend error/status and include reset_table: true;
pending rebuilds preserve Retry-After. Ember must clear stale data on these too.
This consumer change is documented but not implemented in the absent Ember repo.

This detects changes in the currently verified grants, not upstream revocation
that has not reached the session. No permission-version invalidation mechanism
or revocation of already-issued S3 URLs is introduced. A recovery can require
catalog-refresh time plus the synchronous rebuild time; configure gateway/BFF
request deadlines accordingly.
