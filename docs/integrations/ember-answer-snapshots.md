# Ember answer snapshot integration

Updated 24 October 2026. For the Ember PHP BFF and React maintainers.

Keep the synchronous snapshot flow. The required Ember changes are to send
`refresh_of` on explicit refresh, handle `SNAPSHOT_REPLACED`, clear stale data
on access changes, and preserve retry metadata. No job polling or asynchronous
creation is required.

## What stays the same

The existing four Data Agent routes remain the consumer interface:

| Operation | Agent route |
|---|---|
| Create or refresh | `POST /query/snapshots` |
| Page | `GET /query/snapshots/{id}/page?offset=0&limit=100` |
| Download locations | `GET /query/snapshots/{id}/download-urls` |
| Stream CSV part | `GET /query/snapshots/{id}/download?part=1` |

PHP sends the existing `Authorization: Bearer …` and `X-Session-Id` headers.
The agent obtains and forwards the server-issued `X-Catalog-Scope`; Ember must
not manufacture it. Set `CLICKHOUSE_API_REST_BASE_URL` to the clickhouse-api
host root in the agent deployment. It is not derived from the MCP URL.

Continue lazy creation on first paging, exact `has_more` handling, session
ownership checks, conversation snapshot persistence, and the generation guard.
The backend now also enforces ownership; PHP is not the only authorization boundary.

## 1 Change explicit refresh requests

Ordinary creation remains:

```json
{"sql":"SELECT ..."}
```

Identical SQL in the same verified identity, session and scope reuses a frozen
snapshot until expiry. Sending this body again does not refresh data.

Update `Lane5RuntimeClient::createSnapshot` to accept an optional `refreshOf`
argument. Update `POST /api/data-agent/query/snapshot/refresh` to pass the currently
saved snapshot ID:

```json
{"sql":"SELECT ...","refresh_of":"11111111-1111-4111-8111-111111111111"}
```

Capture that source ID at the start of a refresh operation. Keep both SQL and
`refresh_of` unchanged across transport retries. On success, the next user
refresh references the replacement ID. Do not generate an idempotency key or
append a nonce to SQL.

Success remains HTTP 201 with `snapshot_id`, `columns`, `row_count`,
`created_at`, and `expires_at`. Only after success should PHP atomically replace
the saved snapshot. Require that the table still references the source snapshot
(or the same active generation); a late refresh must not replace newer data.
React then clears the old rows and reloads offset zero.

During an ordinary refresh, the previous valid table may remain visible with a
refresh indicator. A permission denial must clear it rather than leave an old,
now-inaccessible result on screen.

## 2 Handle automatic scope recovery

When current grants differ from a snapshot's grants, clickhouse-api blocks its
old rows and download URLs. The agent refreshes the session catalog once and
asks clickhouse-api to rebuild from privately stored SQL. That SQL is revalidated
against the current grants. Ember does not call the internal rebuild endpoint.

A successful recovery returns HTTP 409, deliberately not a normal page or CSV:

```json
{
  "detail": {
    "code": "SNAPSHOT_REPLACED",
    "error": "Your access scope changed. Replace the displayed snapshot."
  },
  "snapshot": {
    "snapshot_id": "22222222-2222-4222-8222-222222222222",
    "columns": ["employee_code", "employee_name"],
    "row_count": 24,
    "created_at": "2026-10-24T18:00:00Z",
    "expires_at": "2026-10-24T22:00:00Z"
  },
  "reset_table": true
}
```

This can arrive while paging, requesting download URLs, or requesting a streamed
CSV part. Update both `QueryPageHandler` and the download handler to recognize it
before generic error handling. Preserve the top-level `snapshot` and
`reset_table` fields in `Lane5RuntimeClient`; retaining only `detail.error` loses
the replacement information.

For an applicable response (the source ID/generation still matches):

1. Cancel outstanding page/download requests and increment the UI generation.
2. Clear old rows, offsets, total counts, `has_more`, download links and selection.
3. Atomically save the replacement snapshot against that answer table.
4. Update the timestamp and row count, then fetch offset zero using the new ID.
5. For a download action, request the new snapshot's download locations again.

Do not append replacement rows to existing rows or reuse the old offset. Do not
use the replacement's ID with an old part list. Generation checks must also
prevent delayed responses for the old snapshot from appending data afterward.

Repeated recovery requests may return the same replacement. Applying that
reference again must be harmless; it must not start another refresh.

## 3 Preserve errors and clear inaccessible results

Normalize backend `detail.code` and agent `error_code` without throwing away the
original status, `Retry-After`, `snapshot`, or `reset_table`.

| Response | Required Ember behavior |
|---|---|
| `201` creation | Persist snapshot metadata and load its first page. |
| `409 SNAPSHOT_REPLACED` | Apply the replacement procedure above. |
| Any `reset_table: true` | Immediately clear stale rows/downloads, even when no replacement exists. Keep any old reference only as an invalid recovery reference, not as displayable data. |
| `403`, including `COLUMN_SCOPE_VIOLATION` | Show an access error; clear inaccessible data and stop automatic retries. The original SQL may no longer be allowed. |
| `409 SNAPSHOT_IN_PROGRESS` | Respect `Retry-After` and retry the identical operation with a bounded retry policy. If `reset_table` is present, keep the old table cleared. |
| `429 EXPORT_BUSY` | Respect `Retry-After`; retry the identical operation. |
| `409 SNAPSHOT_CREATION_FAILED` | Show failure and stop automatic retries. This request fingerprint is not automatically re-executed before expiry. |
| `410 SNAPSHOT_EXPIRED` | Disable the expired snapshot and offer Refresh. Retained metadata can still authorize `refresh_of`. |
| `404 SNAPSHOT_NOT_FOUND` | Clear the stale reference. For a locally owned answer table, ordinary creation from its saved SQL can replace a removed/legacy snapshot. Do not use this as a fallback for arbitrary foreign IDs. |
| `401` | Reauthenticate. Do not reuse cached display data as an authorization fallback. |
| `501 EXPORT_NOT_CONFIGURED` | Show feature unavailable; check the agent REST-base configuration. |
| `502/503 EXPORT_UNAVAILABLE` or transport timeout | Show a recoverable backend error. Any retry must preserve the original request, including the refresh source. Never infer that timeout means SQL did not execute. |

A failed automatic rebuild can return an access error or an in-progress error
with `reset_table: true`. It is not safe to keep displaying the original table.
The same rule applies to access-denied explicit refreshes.

## 4 Persist the replacement consistently

Map the API response into the existing conversation record:

| API field | Existing Ember snapshot field |
|---|---|
| `snapshot_id` | `id` |
| `created_at` | `createdAt` |
| `expires_at` | `expiresAt` |
| `row_count` | `rowCount` |

Apply this mapping for initial creation, explicit refresh and automatic recovery.
Persist against the originating answer-table identity, not merely the newest
message. Preserve the original SQL needed for ordinary recreation. Respect the
current session and atomic update/generation protections in the MR.

## 5 Downloads and deadlines

Keep direct browser downloads for absolute S3 URLs. Keep the authenticated,
unbuffered PHP stream for relative URLs. For a part request, inspect status and
content type before sending CSV headers: an error/replacement JSON response must
not become a downloaded CSV file.

Do not cache or persist presigned URLs as durable download references. They
expire independently; retain the snapshot ID and obtain fresh URLs. Previously
issued S3 URLs cannot be revoked by the new scope check and remain usable until
their signed expiry.

Creation is synchronous. The agent's create read timeout is 150 seconds, normal
reads 60, and streaming reads 120. A scope recovery may additionally refresh the
catalog and wait for a rebuild (150-second read timeout). Configure PHP and
reverse-proxy deadlines for that complete path; a 150-second gateway limit alone
does not cover every recovery. No 202 response or job-status polling is introduced.

## Acceptance checklist

- First table load executes once; subsequent pages and CSV downloads do not.
- SQL-only creation retries return the same snapshot.
- Explicit Refresh sends `refresh_of`, creates a new ID, and a retried refresh
  returns the same replacement without another execution.
- Same user in a different session cannot read or refresh the snapshot.
- An allowed query under changed grants returns a replacement; old rows are
  cleared and offset zero is requested before anything is appended.
- A query denied under changed grants returns an access error, dispatches no SQL,
  and clears the old result.
- Recovery works for page, URL-list and streamed-part actions.
- In-progress/busy handling respects `Retry-After` without changing request identity.
- A late page or refresh response cannot overwrite the current table generation.
- Expiry, removed snapshots and omitted export configuration have explicit UI states.
- Conversation reload restores the latest valid snapshot, not its predecessor.

## End to end verification

The local mock Ember BFF was exercised over real HTTP through the actual agent
application, JWT verification, session-catalog flow and clickhouse-api. Queries
used synthetic HR data in MySQL/Trino and native ClickHouse, with real Couchbase
and an S3-compatible RustFS server. No model call was needed for these UI routes.
The agent session store and Ember conversation store were in-memory test stores.

The current SQL-only refresh and generic recovery-error handling were reproduced
as compatibility findings. The updated mock behavior passed creation, paging,
refresh/retry, downloads, ownership, scope recovery/denial, concurrency and expiry.

See [the execution report](../verification/ember-snapshot-e2e-2026-10-24.json).
This is not a test of the actual PHP/React source or AWS S3. Browser rendering,
conversation Couchbase CAS, multi-tab races, and the deployed gateway configuration
still require Ember's own integration tests.


A subsequent live-model run also passed three questions using DeepSeek V4.1 Flash
on Fireworks: all employees (MySQL), total earnings (ClickHouse), and employee
names with earnings (mixed Trino). The model-produced answer SQL was passed into
the mock Ember BFF and materialized/read through the agent proxy. All answers
matched the fixture. Tool calls were 7, 7 and 13; turn durations were 24.61,
18.01 and 56.08 seconds. The 23 snapshot contract checks were rerun with the real
agent app and MCP service. See [live agent evidence](../verification/ember-agent-live-e2e-2026-10-24.json).
