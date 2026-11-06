# Full blueprint audit — 2026-11-05

Audited all 188 canonical blueprints. Reference expansion produces 192 executable SQL nodes. Changed 30 blueprints; some needed more than one fix.

## Confirmed defects and cleanup

- Six blueprints used simple CASE with row-dependent return expressions. ClickHouse 24.8 rejected these with ILLEGAL_COLUMN (44). Replaced them with equivalent searched CASE expressions.
- Ten blueprints applied dateTrunc directly to Date32 columns. ClickHouse 24.8 rejected these with ILLEGAL_TYPE_OF_ARGUMENT (43). Month buckets now use addDays(date, 1 - toDayOfMonth(date)). Week buckets subtract the day distance modulo seven from the known Monday 1900-01-01, preserving Date32 range. Both forms pass the Trino mapping checks.
- Nineteen blueprints redundantly requested FINAL. Removed it because MCP owns storage-version deduplication. This cleanup assumes execution through MCP, not ungoverned direct production queries.

The first timestamp-conversion candidate was rejected during review: ClickHouse truncation wrapped dates in 1960 and 2200. Final date arithmetic was checked against 1960-03-15, 2026-03-31 and 2200-03-15, including Monday week starts.

## Coverage and limits

- Corpus load validation after real reference expansion; all declared enum choices; every selectable sort column in both directions; declared defaults; optional-slot omission; populated categorical-list inputs.
- 2,482 rendered SQL cases executed against an isolated ClickHouse 24.8 container with all catalog tables/column types. The tables were empty, so this checks planning, type compatibility and execution paths—not real population totals, RLS, production mirror parity or every data-dependent expression.
- DAG intermediate table schemas were materialized. Scalar intermediate/rule bindings used explicit synthetic values; business-rule resolution was not tested live.
- Synthetic placeholder values were shortened to fit FixedString columns; initial oversize-value errors were harness artifacts and were not treated as blueprint defects.
- 921 presentation checks passed across all 188 blueprints (synthetic cells).
- API corpus/population suite: 1,204 passed. Agent blueprint/retrieval/generalization suite: 2,821 passed, 2 skipped.
- Regression coverage added in tests/corpus/test_blueprint_legacy_sql.py. Corpus manifests and agent snapshot regenerated.
- No legacy order_direction slots, experimental binding keys, or type:list slots remain.
- Not a new DeepSeek/MCP conversation battery or full live Trino execution battery. No deployment, Neo4j hydration, commit or push.

Final BLUEPRINTS_SHA: `737a56d63564acd0372035b861fa56de9fea8b08`.

## Per-blueprint checklist

| Blueprint | SQL nodes | Binding/execution cases | Changes |
|---|---:|---:|---|
| bp-accrual-balance-summary | 1 | 31 | row-dependent CASE fixed |
| bp-accrual-code-dictionary | 1 | 9 | No change needed in audited paths |
| bp-accrual-event-summary | 1 | 39 | row-dependent CASE fixed |
| bp-active-headcount-by-department | 1 | 25 | No change needed in audited paths |
| bp-adjustment-line-volume | 1 | 9 | Date32 bucket arithmetic fixed |
| bp-age-band-distribution | 1 | 14 | No change needed in audited paths |
| bp-approved-timeoff-hours | 1 | 9 | No change needed in audited paths |
| bp-approved-timeoff-ranking | 1 | 2 | No change needed in audited paths |
| bp-average-annual-salary-by-status | 1 | 22 | No change needed in audited paths |
| bp-average-salary-by-department | 1 | 28 | No change needed in audited paths |
| bp-avg-request-size | 1 | 15 | No change needed in audited paths |
| bp-avg-shift-length | 1 | 9 | No change needed in audited paths |
| bp-break-gap-median | 1 | 11 | No change needed in audited paths |
| bp-card-vs-personal-split | 1 | 7 | No change needed in audited paths |
| bp-category-by-department | 1 | 8 | redundant FINAL removed |
| bp-category-earnings-per-employee | 1 | 11 | redundant FINAL removed |
| bp-category-hours-monthly | 1 | 7 | Date32 bucket arithmetic fixed, redundant FINAL removed |
| bp-category-mom-volatility | 1 | 16 | Date32 bucket arithmetic fixed, redundant FINAL removed |
| bp-category-spend-over-threshold | 1 | 12 | redundant FINAL removed |
| bp-change-actor-mix | 1 | 9 | No change needed in audited paths |
| bp-change-velocity-spikes | 1 | 11 | No change needed in audited paths |
| bp-clock-time-profile | 1 | 11 | No change needed in audited paths |
| bp-commission-by-employee | 1 | 9 | redundant FINAL removed |
| bp-comp-range-exceptions | 1 | 19 | No change needed in audited paths |
| bp-compare-employee-check-detail-two-periods | 3 | 57 | No change needed in audited paths |
| bp-compare-employee-paychecks-two-periods | 1 | 21 | No change needed in audited paths |
| bp-compensation-ranking | 1 | 14 | No change needed in audited paths |
| bp-consecutive-day-streaks | 1 | 13 | No change needed in audited paths |
| bp-deduction-breakdown | 1 | 8 | No change needed in audited paths |
| bp-denied-expenses | 1 | 13 | No change needed in audited paths |
| bp-department-directory | 1 | 11 | No change needed in audited paths |
| bp-department-label-audit | 1 | 8 | No change needed in audited paths |
| bp-department-ot-share | 1 | 12 | redundant FINAL removed |
| bp-department-scorecard | 1 | 20 | No change needed in audited paths |
| bp-department-turnover-rate | 1 | 10 | No change needed in audited paths |
| bp-departments-above-company-average-salary | 2 | 48 | No change needed in audited paths |
| bp-dol-status-per-work-location | 1 | 17 | No change needed in audited paths |
| bp-duplicate-expense-flag-list | 1 | 9 | No change needed in audited paths |
| bp-earning-category-dictionary | 1 | 7 | redundant FINAL removed |
| bp-earnings-by-category | 1 | 7 | redundant FINAL removed |
| bp-earnings-by-department-via-scratch-join | 2 | 14 | No change needed in audited paths |
| bp-earnings-by-dol-status | 1 | 8 | No change needed in audited paths |
| bp-earnings-by-pay-basis | 1 | 8 | No change needed in audited paths |
| bp-earnings-by-work-state | 1 | 10 | No change needed in audited paths |
| bp-earnings-category-mix | 1 | 15 | redundant FINAL removed |
| bp-earnings-category-trend | 1 | 7 | Date32 bucket arithmetic fixed, redundant FINAL removed |
| bp-earnings-monthly-trend | 1 | 9 | Date32 bucket arithmetic fixed |
| bp-earnings-quarter-to-date | 1 | 7 | No change needed in audited paths |
| bp-effective-mileage-rate | 1 | 11 | No change needed in audited paths |
| bp-employee-benefits-summary | 1 | 8 | No change needed in audited paths |
| bp-employee-change-timeline | 1 | 15 | No change needed in audited paths |
| bp-employee-change-volume | 1 | 9 | No change needed in audited paths |
| bp-employee-check-detail-for-period | 1 | 17 | No change needed in audited paths |
| bp-employee-coverage-matrix | 1 | 24 | No change needed in audited paths |
| bp-employee-date-summary | 1 | 26 | No change needed in audited paths |
| bp-employee-day-clock-span | 1 | 34 | No change needed in audited paths |
| bp-employee-directory | 1 | 25 | No change needed in audited paths |
| bp-employee-record-dq-flags | 1 | 20 | No change needed in audited paths |
| bp-employee-summary | 1 | 39 | No change needed in audited paths |
| bp-employee-tax-withheld | 1 | 8 | No change needed in audited paths |
| bp-employee-tenure-summary | 1 | 31 | row-dependent CASE fixed |
| bp-employees-in-inactive-seats | 1 | 16 | No change needed in audited paths |
| bp-employer-tax-summary | 1 | 8 | No change needed in audited paths |
| bp-expense-approval-backlog | 1 | 2 | No change needed in audited paths |
| bp-expense-approval-trim | 1 | 11 | No change needed in audited paths |
| bp-expense-by-account-category | 1 | 9 | No change needed in audited paths |
| bp-expense-by-policy | 1 | 7 | No change needed in audited paths |
| bp-expense-category-distribution | 1 | 11 | No change needed in audited paths |
| bp-expense-concept-spend | 1 | 7 | No change needed in audited paths |
| bp-expense-cycle-time | 1 | 11 | No change needed in audited paths |
| bp-expense-line-size-distribution | 1 | 10 | No change needed in audited paths |
| bp-expense-monthly-trend | 1 | 7 | No change needed in audited paths |
| bp-expense-payroll-recon | 1 | 16 | No change needed in audited paths |
| bp-expense-per-capita | 1 | 12 | No change needed in audited paths |
| bp-expense-senders-no-punches | 1 | 10 | No change needed in audited paths |
| bp-expenses-unreconciled-to-payroll | 1 | 9 | No change needed in audited paths |
| bp-fica-medicare-totals | 1 | 10 | No change needed in audited paths |
| bp-future-dated-position-changes | 1 | 16 | No change needed in audited paths |
| bp-future-hire-roster | 1 | 14 | No change needed in audited paths |
| bp-future-termination-roster | 1 | 14 | No change needed in audited paths |
| bp-gross-earnings-for-pay-period | 1 | 5 | No change needed in audited paths |
| bp-gross-earnings-period-compare | 1 | 8 | No change needed in audited paths |
| bp-gross-earnings-summary | 1 | 5 | No change needed in audited paths |
| bp-gross-wages-date-window | 1 | 5 | No change needed in audited paths |
| bp-headcount-by-dol-status | 1 | 8 | No change needed in audited paths |
| bp-headcount-by-hire-era | 1 | 9 | No change needed in audited paths |
| bp-headcount-by-pay-frequency | 1 | 8 | No change needed in audited paths |
| bp-headcount-by-union | 1 | 8 | No change needed in audited paths |
| bp-headcount-by-work-city-country | 1 | 13 | No change needed in audited paths |
| bp-headcount-by-works-in-state | 1 | 11 | No change needed in audited paths |
| bp-headcount-growth-rate | 1 | 11 | No change needed in audited paths |
| bp-headcount-monthly-trend | 1 | 9 | No change needed in audited paths |
| bp-hires-in-range | 1 | 7 | No change needed in audited paths |
| bp-hires-per-month | 1 | 7 | No change needed in audited paths |
| bp-hires-projection | 1 | 9 | No change needed in audited paths |
| bp-holiday-work-audit | 1 | 9 | No change needed in audited paths |
| bp-inactive-supervisor-audit | 1 | 16 | No change needed in audited paths |
| bp-inactive-without-leave-start | 1 | 14 | No change needed in audited paths |
| bp-labor-allocation-coverage | 1 | 7 | No change needed in audited paths |
| bp-labor-allocation-dictionary | 1 | 9 | row-dependent CASE fixed |
| bp-labor-allocation-resolution | 1 | 22 | No change needed in audited paths |
| bp-labor-allocation-rollup | 1 | 11 | row-dependent CASE fixed |
| bp-latest-hired-employees | 1 | 14 | No change needed in audited paths |
| bp-long-shift-days | 1 | 11 | No change needed in audited paths |
| bp-longest-tenured-employees | 1 | 2 | No change needed in audited paths |
| bp-manager-span-of-control | 1 | 2 | No change needed in audited paths |
| bp-median-annual-salary | 1 | 8 | No change needed in audited paths |
| bp-median-hourly-rate-by-state-union | 1 | 12 | No change needed in audited paths |
| bp-median-salary-by-position-level | 1 | 9 | No change needed in audited paths |
| bp-mileage-summary | 1 | 9 | No change needed in audited paths |
| bp-missing-emergency-contacts | 1 | 12 | No change needed in audited paths |
| bp-missing-receipts | 1 | 11 | No change needed in audited paths |
| bp-missing-supervisor-exceptions | 1 | 16 | No change needed in audited paths |
| bp-name-identity-audit | 1 | 12 | No change needed in audited paths |
| bp-negative-paycheck-employees | 1 | 16 | No change needed in audited paths |
| bp-negative-pto-balances | 1 | 2 | No change needed in audited paths |
| bp-net-pay-summary | 1 | 6 | No change needed in audited paths |
| bp-new-hire-90d-attrition | 1 | 9 | No change needed in audited paths |
| bp-night-shift-share | 1 | 11 | No change needed in audited paths |
| bp-orphan-register-codes | 1 | 9 | No change needed in audited paths |
| bp-ot-two-basis-tieout | 1 | 19 | redundant FINAL removed |
| bp-overtime-by-department | 1 | 8 | No change needed in audited paths |
| bp-overtime-hours-dollars | 1 | 9 | redundant FINAL removed |
| bp-paid-hours-summary | 1 | 7 | No change needed in audited paths |
| bp-pairs-per-day-distribution | 1 | 7 | No change needed in audited paths |
| bp-pay-timing-exceptions | 1 | 16 | No change needed in audited paths |
| bp-payout-request-summary | 1 | 9 | No change needed in audited paths |
| bp-payroll-codes-unmapped | 1 | 7 | redundant FINAL removed |
| bp-payroll-duplicate-line-audit | 1 | 17 | No change needed in audited paths |
| bp-payroll-line-counts | 1 | 8 | No change needed in audited paths |
| bp-payroll-setup-gap-scorecard | 1 | 26 | No change needed in audited paths |
| bp-payroll-summary | 1 | 31 | No change needed in audited paths |
| bp-pending-timeoff-queue | 1 | 21 | No change needed in audited paths |
| bp-placeholder-category-audit | 1 | 9 | redundant FINAL removed |
| bp-position-change-activity | 1 | 10 | No change needed in audited paths |
| bp-post-termination-accrual-audit | 1 | 15 | No change needed in audited paths |
| bp-post-termination-changes | 1 | 11 | No change needed in audited paths |
| bp-post-termination-pay-audit | 1 | 13 | No change needed in audited paths |
| bp-punch-anomalies | 1 | 11 | No change needed in audited paths |
| bp-punch-coverage-gap | 1 | 11 | No change needed in audited paths |
| bp-punch-pair-summary | 1 | 39 | row-dependent CASE fixed |
| bp-punch-plus-timeoff-overlap | 1 | 14 | No change needed in audited paths |
| bp-punch-source-audit | 1 | 9 | Date32 bucket arithmetic fixed |
| bp-punch-type-mix | 1 | 9 | No change needed in audited paths |
| bp-quarterly-terminations-with-top-reasons | 1 | 2 | No change needed in audited paths |
| bp-records-per-status | 1 | 8 | No change needed in audited paths |
| bp-register-type-breakdown | 1 | 8 | No change needed in audited paths |
| bp-regular-vs-premium-hours-ratio | 1 | 11 | Date32 bucket arithmetic fixed, redundant FINAL removed |
| bp-rehired-employees | 1 | 16 | No change needed in audited paths |
| bp-retroactive-change-share | 1 | 9 | No change needed in audited paths |
| bp-salary-exact-median | 1 | 39 | No change needed in audited paths |
| bp-salary-threshold-count | 1 | 26 | No change needed in audited paths |
| bp-salary-threshold-directory | 1 | 34 | No change needed in audited paths |
| bp-shift-differential-spend | 1 | 7 | redundant FINAL removed |
| bp-supervision-anomalies | 1 | 16 | No change needed in audited paths |
| bp-supervisor-chain-depth | 1 | 9 | No change needed in audited paths |
| bp-tenure-band-distribution | 1 | 10 | No change needed in audited paths |
| bp-termination-family | 1 | 27 | No change needed in audited paths |
| bp-timecard-earnings-all-history | 1 | 11 | No change needed in audited paths |
| bp-timecard-earnings-summary | 1 | 29 | No change needed in audited paths |
| bp-timecard-vs-register-recon | 1 | 19 | No change needed in audited paths |
| bp-timeoff-by-leave-type | 1 | 14 | No change needed in audited paths |
| bp-timeoff-denial-rate | 1 | 15 | No change needed in audited paths |
| bp-timeoff-funnel | 1 | 10 | No change needed in audited paths |
| bp-timeoff-lead-time | 1 | 15 | No change needed in audited paths |
| bp-timeoff-seasonality | 1 | 10 | No change needed in audited paths |
| bp-timeoff-vs-paid-leave-tieout | 1 | 22 | Date32 bucket arithmetic fixed, redundant FINAL removed |
| bp-timeoff-withdrawal-rate | 1 | 15 | No change needed in audited paths |
| bp-top-changed-fields | 1 | 2 | No change needed in audited paths |
| bp-top-codes-in-category | 1 | 2 | redundant FINAL removed |
| bp-top-expense-spenders | 1 | 2 | No change needed in audited paths |
| bp-top-home-zip-city | 1 | 2 | No change needed in audited paths |
| bp-top-payroll-codes | 1 | 9 | No change needed in audited paths |
| bp-top-timeoff-users | 1 | 5 | No change needed in audited paths |
| bp-total-earnings-by-department | 1 | 8 | No change needed in audited paths |
| bp-unpaid-active-employees | 1 | 11 | No change needed in audited paths |
| bp-unused-leave-roster | 1 | 15 | No change needed in audited paths |
| bp-upcoming-anniversaries | 1 | 16 | No change needed in audited paths |
| bp-upcoming-timeoff-roster | 1 | 20 | No change needed in audited paths |
| bp-weekend-work-share | 1 | 13 | Date32 bucket arithmetic fixed |
| bp-weekly-overtime-hours | 1 | 13 | Date32 bucket arithmetic fixed |
| bp-worked-hours-daily | 1 | 7 | No change needed in audited paths |
| bp-workforce-composition | 1 | 12 | No change needed in audited paths |
| bp-workforce-diversity-profile | 1 | 18 | No change needed in audited paths |
| bp-workforce-kpi-bundle | 1 | 25 | No change needed in audited paths |
| bp-workforce-movements | 1 | 17 | No change needed in audited paths |
| bp-ytd-earnings-per-employee | 1 | 15 | No change needed in audited paths |
| bp-zero-net-pay-employees | 1 | 7 | No change needed in audited paths |

## Local evidence

Artifacts under `/tmp/all-blueprint-audit/`: `audit.py`, `execute.py`, `results.json`, `rendered.json`, `execution.json`, `execution-done.log`, `changes.json`, `date-boundaries.json`, `presentations.json`, `api-final.log`, `agent-final.log`.


## Dual-engine slot matrix follow-up — 2026-11-05

The original historical matrix script could not be located. A reconstructed renderer and runner are preserved in [the artifact directory](artifacts/blueprint-slot-matrix-2026-11-05/). This follow-up supersedes the earlier section's “no full live Trino execution battery” limitation for SQL execution only; it is still not a live LLM/MCP conversation test.

Tested the current uncommitted working trees, with the corpus SHA above. All 188 blueprints expand to 192 SQL nodes and 2,482 rendered node/case combinations. Coverage includes required inputs/defaults, optional-slot omission, explicit inputs, enum choices, and every allowed sort column in both directions. It is not the Cartesian product of all slot combinations.

| Lane | Passed cases | Failed cases | Blueprints with no failures |
|---|---:|---:|---:|
| Native ClickHouse 24.8 | 2,482 | 0 | 188 / 188 |
| Forced Trino 483 | 2,402 | 80 | 181 / 188 |

All cases pass through current `service.run_query` validation and execution. Trino's direct-ClickHouse shortcut was disabled in the harness, so a Trino pass cannot actually be a native-only pass. Final serial Trino execution took 1,653 seconds. These times describe local synthetic fixtures, not production latency.

### Confirmed failures

| Blueprint | Failing Trino cases | Cause |
|---|---:|---|
| `bp-adjustment-line-volume` | 9 | Empty payroll produces a pre-1970 relative-window boundary; JDBC rejects `1969-11-01`. |
| `bp-earnings-monthly-trend` | 9 | Same empty-payroll date-boundary failure. |
| `bp-hires-per-month` | 7 | Generated Trino SQL retains `INTERVAL (N) MONTH`; syntax rejected. |
| `bp-hires-projection` | 9 | Same parenthesized MONTH interval translation. |
| `bp-pay-timing-exceptions` | 16 | Generated Trino SQL retains `INTERVAL (N) DAY`; syntax rejected. |
| `bp-timeoff-seasonality` | 10 | Same parenthesized MONTH interval translation. |
| `bp-upcoming-timeoff-roster` | 20 | Translator rejects parenthesized integer DAY interval before coordinator submission. |

Independent reproductions and coordinator query IDs are in [confirmed-failures.json](artifacts/blueprint-slot-matrix-2026-11-05/confirmed-failures.json). The upcoming-time-off failure has no coordinator query ID because translation rejects it first. JDBC boundary errors are surfaced as `TRINO_UNAVAILABLE`, obscuring the actual date-range error; this is an additional diagnostic-classification gap.

Two controls isolate the causes without changing the corpus or product code:

- **Populated-date control:** one synthetic payroll row anchored at `2026-03-31`; all 18 previously failing date variants pass. Empty-source behavior remains defective.
- **Interval control:** remove only parentheses around the literal interval size in submitted SQL; all 62 previously failing interval variants pass. The production translator/blueprints remain unchanged by this test.

The next fixes should handle parenthesized integer intervals in translation and empty-source relative-date anchors without fabricating a current-data date or silently clamping the requested period. Retain these failing cases as regression coverage.

### Fixture corrections and limits

Initial attempts were discarded after stale connector configuration, scratch-namespace mistakes, and Docker memory exhaustion. The final complete run used `dbpcm_scratch`, the documented `clickhouse.map-string-as-varchar=true`, MySQL aggregation pushdown disabled, and a serial Trino worker with a 768MB heap and explicit native-memory limits. Initial VARCHAR/VARBINARY join errors disappeared with the correct connector configuration and are not blueprint defects.

ClickHouse tables were empty, catalog-typed ReplacingMergeTree fixtures; Trino also used the existing synthetic MySQL fixture and actual local source definitions. Identity/shard discovery were test fixtures and employee-access enforcement was disabled. DAG table intermediates were materialized into session-owned ClickHouse scratch tables; scalar/rule bindings used synthetic placeholders. This does not establish business totals, data parity, live slot lookups, RLS correctness, or end-to-end agent behavior.

### Saved evidence and replay

- [Summary and per-blueprint counts](artifacts/blueprint-slot-matrix-2026-11-05/summary.json)
- [Run metadata and fixture requirements](artifacts/blueprint-slot-matrix-2026-11-05/run-metadata.json)
- [Renderer](artifacts/blueprint-slot-matrix-2026-11-05/render_cases.py) — run with the agent virtualenv and its `src` on `PYTHONPATH`.
- [Executor](artifacts/blueprint-slot-matrix-2026-11-05/execute_cases.py) — run with the API virtualenv and provisioned local fixtures; `--preflight`, then `--lane ch` and `--lane trino`.
- Compressed exact inputs and complete lane results: `rendered.json.gz`, `ch-results.json.gz`, `trino-results.json.gz` in the same artifact directory. Decompress inputs to `rendered.json` or regenerate with the renderer.
- `populated-control.json` and `interval-control.json` record all 80 control reruns.

The saved renderer was rerun and produced exactly the same 2,482 cases used in this matrix. Both archived scripts pass Ruff. No product fix, commit, push, deployment, or Neo4j change was made in this follow-up.
