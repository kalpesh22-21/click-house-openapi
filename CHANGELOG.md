# Changelog

## Unreleased

- Added optional answer snapshots with Couchbase (default) or S3-compatible storage, selected by `EXPORT_STORE` and enabled by `EXPORT_ENABLED`.
- Added authenticated snapshot creation, frozen paging, CSV downloads and presigned S3 download URLs on both REST and MCP HTTP deployments.
- Added streaming export execution through native ClickHouse or Trino, retaining caller scope, shard routing and source row policies. Export budgets are independent of interactive result limits.
- Added snapshot expiry, caller isolation, multipart CSV output and cleanup of incomplete snapshots.
- Bound verified REST session identity for owned scratch-table access. Preserved exact Decimal output and sanitized query diagnostics.
