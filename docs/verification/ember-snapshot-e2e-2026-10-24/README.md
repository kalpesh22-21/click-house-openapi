# Local mock Ember verification harness

These are the exact mock BFF and scenario runner used for the 24 October live
HTTP verification. They are test-only artifacts, not production routes or a PHP
implementation. See ../ember-snapshot-e2e-2026-10-24.json for results and limits.

The mock listens on 127.0.0.1:18893 and calls the agent on :18892. The runner also
uses the dedicated backend on :18891 and its fixture-only `_test/scope`,
`_test/counts`, and `_test/expire` endpoints. It requires the existing synthetic
HR fixture indicated by /tmp/data-agent-source-e2e-path (generated private/public
keys and registry); no key material is included here. The agent uses real JWT
verification/session catalogs, with an in-memory session store. The backend uses
real Trino/ClickHouse and Couchbase or RustFS. Its fixture adapter can change
grants and instrument result-query starts. Do not expose fixture control routes.

For the original local fixture, start the dedicated backend/agent as described
in the execution report, then run the mock with the backend Python environment:

    .venv/bin/python docs/verification/ember-snapshot-e2e-2026-10-24/mock_ember.py
    .venv/bin/python docs/verification/ember-snapshot-e2e-2026-10-24/scenarios.py couchbase

Switch the backend store and repeat with `s3`. The runner writes its results to
/tmp/ember-snapshot-e2e/report-<store>.json. This is not a standalone environment
provisioner; the original fixture launch files remain in /tmp/ember-snapshot-e2e.
