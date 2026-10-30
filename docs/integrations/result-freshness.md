# Source-aware result freshness

`runQuery` and `sampleRows` (MCP and REST) attach freshness after successful execution. The execution engine does not determine freshness: a Trino join can read both live MySQL data and an ingested ClickHouse table.

```json
{
  "retrieved_at": "2026-10-30T12:00:00.000Z",
  "data_as_of": "2026-10-12T05:30:00.000Z",
  "freshness": {
    "mode": "mixed",
    "sources": [
      {"table": "dbpcm_warehouse.employee", "kind": "live"},
      {"table": "dbpcm_warehouse.payroll", "kind": "ingested", "data_as_of": "2026-10-12T05:30:00.000Z"}
    ]
  }
}
```

Existing columns, rows, row_count, truncated, and MCP provenance are unchanged. The timestamps above are examples.

- MySQL-backed sources in the server's semantic catalog are live when federation is enabled. Live-only results omit `data_as_of`; display “Live data · retrieved at …”. “Live” describes the source access, not a guarantee of zero replication lag or transaction consistency across sources.
- ClickHouse-backed warehouse sources use `dbpcm_warehouse.etl_product_refresh_status FINAL`. The fixed read-only query binds the authenticated principal's `proc_center` and `clientcode`, and the selected product names. It reads each product's maximum `last_sync_date` and converts to UTC.
- Mixed results use the oldest ingested source timestamp, ignoring live sources when taking that minimum. Example: “Employee data is live; payroll updated through …”. Never label the employee source with payroll's timestamp.
- Every ingested source must have a valid receipt before a top-level `data_as_of` is emitted. Missing receipts, unknown sources, scratch dependencies, unsupported source expressions, or lookup failures produce `freshness.mode: "unknown"` and omit the overall date. Known per-source dates remain available without claiming complete coverage.
- With federation disabled, execution goes directly to ClickHouse; a MySQL catalog declaration alone does not make those results live.
- `retrieved_at` is captured after the data query, before the metadata lookup. It is not an ingestion date. This metadata describes the execution's observed source refresh receipts, not an atomic cross-system snapshot.

`DATA_AS_OF_ENABLED` defaults to `true`. Set it to `false` to omit freshness metadata entirely. `DATA_AS_OF_TIMEOUT_SECONDS` defaults to 2. The metadata lookup uses the existing ClickHouse host, port, TLS, and credentials; the account needs SELECT on the refresh-status table. No metadata migration or synthetic refresh timestamps are introduced. Unknown metadata never fails the data query. EXPLAIN remains a query plan and has no data freshness.

Lane5 integration: Lane5 consumes the API's `freshness`, `retrieved_at`, and optional `data_as_of` as one receipt. Its independent table-name-based lookup has been removed. The receipt is retained in agent context, judge evidence, blueprint checkpoints, scoped history, and the approved answer document. Snapshot creation/rebuild and document refresh are separate operations; do not relabel saved results with a new retrieval timestamp.
