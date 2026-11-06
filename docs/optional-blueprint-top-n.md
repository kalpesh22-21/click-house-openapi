# Optional top_n on ranking and detail blueprints

The 42 blueprints below accept optional `top_n` (integer 1–1000).
Omitting the slot returns the full result. Providing it caps output rows only after
filtering, aggregation, and the existing ordering. It does not change denominators,
measurement filters, or date boundaries. Existing `top_n`, `max_rows`, and
`row_limit` slots on other blueprints are unchanged.

This is an exact row cap, not a dense-rank cutoff: boundary ties are broken by
projected output columns. The limit applies to the whole output, including templates
with multiple pay types; it is not N rows per pay type. Chronological or identifier
ordered detail lists return the first N in their declared order, not the largest
metric. Full distributions, trends, scalar totals, reconciliation reports and
multi-stage summaries retain their original contracts.

## Deployment dependency

Deploy the data-analysis-agent optional LIMIT binder before publishing this corpus.
The corpus uses `required: false`, `optional_pattern: 'TRUE'` and `LIMIT {top_n}`.
The updated binder removes only an exact outer LIMIT placeholder when omitted with
TRUE. It rejects nested limits, offsets, LIMIT BY, WITH TIES, and expressions rather
than silently removing them. Old binders cannot execute the omitted-limit case.
Refresh the indexed corpus after publication; no running service was refreshed by
this source change. Corpus manifests and the agent's generated snapshot are updated.

## Updated blueprints

- `bp-active-headcount-by-department`
- `bp-approved-timeoff-hours`
- `bp-average-salary-by-department`
- `bp-avg-request-size`
- `bp-category-mom-volatility`
- `bp-category-spend-over-threshold`
- `bp-comp-range-exceptions`
- `bp-denied-expenses`
- `bp-department-label-audit`
- `bp-department-turnover-rate`
- `bp-dol-status-per-work-location`
- `bp-earnings-by-work-state`
- `bp-employee-coverage-matrix`
- `bp-employee-day-clock-span`
- `bp-expense-per-capita`
- `bp-expense-senders-no-punches`
- `bp-future-dated-position-changes`
- `bp-future-hire-roster`
- `bp-future-termination-roster`
- `bp-headcount-by-union`
- `bp-headcount-by-work-city-country`
- `bp-headcount-by-works-in-state`
- `bp-inactive-supervisor-audit`
- `bp-negative-paycheck-employees`
- `bp-orphan-register-codes`
- `bp-overtime-by-department`
- `bp-payroll-codes-unmapped`
- `bp-payroll-duplicate-line-audit`
- `bp-pending-timeoff-queue`
- `bp-rehired-employees`
- `bp-supervision-anomalies`
- `bp-timecard-earnings-all-history`
- `bp-timeoff-by-leave-type`
- `bp-timeoff-denial-rate`
- `bp-timeoff-lead-time`
- `bp-timeoff-withdrawal-rate`
- `bp-total-earnings-by-department`
- `bp-unpaid-active-employees`
- `bp-unused-leave-roster`
- `bp-upcoming-anniversaries`
- `bp-upcoming-timeoff-roster`
- `bp-zero-net-pay-employees`
