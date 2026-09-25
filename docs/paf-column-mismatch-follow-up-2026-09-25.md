# PAF column mismatch: follow-up, 2026-09-25

This is an additive follow-up to the lane5 error review; the original review is unchanged.

- **CA-1 pending physical evidence.** The supplied YAML matches this checkout and declares 18 columns, including `field_id`. It is not the reported 17-column physical/served response. The configured ClickHouse metadata read failed with `OperationalError`. No physical DDL or semantic grain/rule changes have been made without evidence of the intended field identifier.
- **CA-2 implemented as a lint command.** `tools/check_catalog_schema.py` compares semantic column declarations, grain, keys, temporal references, measures, SQL predicates, description columns, join targets, and resolver targets against independent physical/candidate metadata. Missing tables also fail. Legacy free-text descriptions, ambiguities, and prose predicates are not interpreted as machine references. No CI pipeline configuration or independent full deployment schema snapshot exists in this checkout; the command must be supplied metadata by the deployment/CI job.
- **CA-3 implemented.** Failed provenance extraction with a provably absent column now emits `INVALID_COLUMN_REFERENCE` with column/source names and schema-repair guidance. Classification uses per-query-scope sources; ambiguous, virtual-source, correlated, and unclassified extraction failures retain `PARSE_FAILED_CLOSED`. Scratch failures retain their existing code. This only classifies an existing rejection; it does not authorize a query. runQuery and explainQuery share the service path, also used by resolveValues queries.
- **CA-4 unverified.** This checkout builds provenance metadata from `system.columns` in `app/catalog.py`, with a 60-second cache and stale fallback after refresh failures. getTableSchema separately merges physical metadata with semantic YAML, then applies hidden-column, scope, and optional column filters. A served column count alone does not prove physical absence or build skew. Compare the unfiltered physical names, request scope/column filter, deployed image revision, and `catalog_sha`. This checkout and the supplied YAML contain four explicit PAF `resolve_via` rules, versus five stated in the review.

Run a fresh metadata check (no row data):

```sh
python tools/check_catalog_schema.py --live --table dbpcm_warehouse.personnel_action_form_changes
```

For CI, provide an independently generated, unfiltered schema snapshot (including referenced join tables), shaped as `{"database.table": {"column": "ClickHouseType"}}`:

```sh
python tools/check_catalog_schema.py --schema physical-schema.json
```

Do not derive that snapshot from the semantic YAML: doing so would hide the mismatch. Both commands exit nonzero on mismatches. Keep the existing catalog-sidecar parity check as a separate check.
