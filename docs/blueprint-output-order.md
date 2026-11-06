# Blueprint ordering

Sorting is an authored capability, not a slot added indiscriminately to every blueprint.

## Fixed rankings

These 14 blueprints expose neither `order_by` nor `order_direction`. Their SQL fixes
the metric/date and direction that define their purpose. Existing row/rank limits,
filters, ties and per-group ranking remain intact:

- `bp-approved-timeoff-ranking`
- `bp-compensation-ranking`
- `bp-expense-approval-backlog`
- `bp-latest-hired-employees`
- `bp-longest-tenured-employees`
- `bp-manager-span-of-control`
- `bp-negative-pto-balances`
- `bp-quarterly-terminations-with-top-reasons`
- `bp-top-changed-fields`
- `bp-top-codes-in-category`
- `bp-top-expense-spenders`
- `bp-top-home-zip-city`
- `bp-top-payroll-codes`
- `bp-top-timeoff-users`

For example, `bp-top-expense-spenders` accepts `top_n` and `expense_window`. It
always selects approved spend descending, with employee code as a deterministic
tie-break. It cannot be turned into a lowest-spender or employee-name ranking.
Longest tenure keeps original hire date ascending; negative balances keep the most
overdrawn first. Other named highest/latest rankings keep their corresponding
metric/date descending.

The executor rejects undeclared sorting controls before resolving slots or executing
SQL. Old calls must remove those controls rather than silently ignoring them. Slot
notes explain that a different selection or display ordering needs a separate query.

## General reports

The other 174 blueprints expose a single `order_by` slot of type `sort_order`,
restricted to exact final-result aliases. It is a list of one to three objects, each
with `column` and `direction` (`ASC` or `DESC`). Duplicate columns are rejected.
There is no separate `order_direction` slot. This includes directories and general
breakdowns such as headcount by department and approved time-off hours by department.
An optional row limit alone does not turn a general report into a fixed ranking.

Example: Highest headcount first, with department name breaking ties:

```json
{
  "id": "bp-active-headcount-by-department",
  "slot_bindings": {
    "employee_status": "Active",
    "order_by": [{"column": "Headcount", "direction": "DESC"},
                 {"column": "Department", "direction": "ASC"}]
  }
}
```

The slot has an authored default list. The template contains exactly:

```sql
ORDER BY {order_by}
```


Blueprint declaration:

```yaml
- name: order_by
  type: sort_order
  required: false
  allowed_columns: [Department Code, Department, Headcount]
  default:
    - column: Headcount
      direction: DESC
```

getBlueprint exposes allowed_columns, the default list, and a value_schema showing
the list/object shape and three-column maximum. Old scalar order_by values and
separate order_direction arguments must be replaced; they are not silently coerced.

Any row limit follows this clause. Lane5 validates the selected output alias and
ASC/DESC enum for every entry, binds quoted SQL identifiers and directions in list order, and adds no
wrapper, CASE branch, implicit secondary sort, or explicit null-placement clause. Ties follow only the
additional columns explicitly supplied; otherwise equal values have no promised order. Fixed-ranking SQL keeps its own authored rules.

Examples with order_by omitted:

- Accrual code dictionary: `"Holder Count" DESC`.
- Active headcount by department: `"Headcount" DESC`.
- Pending time-off queue: `"Days Waiting" DESC`.
- Monthly trends: `"Month" ASC`.
- Employee directory: `"Name" ASC`.

Band distributions expose a numeric `Band Start` column; anniversary listings expose
`Anniversary Day of Year`; reconciliation and volatility reports expose absolute
variance columns. These are ordinary output columns with formats and are selectable
like every other output column. They preserve the report's purpose without hidden
sort expressions: sorting a display label alphabetically or an original hire date
would not give numeric band order or upcoming-anniversary order.

Referenced detail blueprints use their own declared sorting defaults when the parent
does not map those controls. The parent's selected final-result column cannot be
applied to a child with different output columns.

## Repair and learning boundaries

Malformed sort_order arguments return retryable SLOT_INVALID with the allowed columns;
they do not pause the turn or ask the user to format tool arguments. No warehouse call
is made for the invalid sort.

The learning extractor currently parameterizes predicate literals and selected structural
arguments, not complete ORDER BY lists. It therefore does not advertise sort_order as a
mined predicate type. Learned SQL retains its accepted ordering; runtime/canonical
blueprints support sort_order as documented above. Automatic sort-list extraction needs
a dedicated structural locator and reconstruction/replay support before it can be enabled.

## Updating a running environment

Deploy sort_order parsing, validation and AST binding with the updated
corpus together. Refresh Neo4j from the current MCP corpus export; an old graph still
advertises the removed slots. Regenerate corpus manifests and the agent fixture
with `tools/check_corpus_parity.py --write` and `scripts/regen_corpus_snapshot.py`.

## Reviewed configurable defaults

| Blueprint | Default column | Direction |
|---|---|---|
| `bp-accrual-balance-summary` | Group | ASC |
| `bp-accrual-code-dictionary` | Holder Count | DESC |
| `bp-accrual-event-summary` | Group | ASC |
| `bp-active-headcount-by-department` | Headcount | DESC |
| `bp-adjustment-line-volume` | Month | ASC |
| `bp-age-band-distribution` | Band Start | ASC |
| `bp-approved-timeoff-hours` | Approved Hours | DESC |
| `bp-average-annual-salary-by-status` | Pay Type | ASC |
| `bp-average-salary-by-department` | Average Annual Compensation | DESC |
| `bp-avg-request-size` | Average Hours | DESC |
| `bp-avg-shift-length` | Avg Shift Hours | DESC |
| `bp-break-gap-median` | Median Break Gap Hours | DESC |
| `bp-card-vs-personal-split` | Approved Amount | DESC |
| `bp-category-by-department` | Total Dollars | DESC |
| `bp-category-earnings-per-employee` | Total Dollars | DESC |
| `bp-category-hours-monthly` | Month | ASC |
| `bp-category-mom-volatility` | Absolute Pct Change | DESC |
| `bp-category-spend-over-threshold` | Total Dollars | DESC |
| `bp-change-actor-mix` | Month | ASC |
| `bp-change-velocity-spikes` | Change Events | DESC |
| `bp-clock-time-profile` | Median Clock-In Min | ASC |
| `bp-commission-by-employee` | Total Commission | DESC |
| `bp-comp-range-exceptions` | Compa Ratio | ASC |
| `bp-compare-employee-check-detail-two-periods` | Register Type | ASC |
| `bp-compare-employee-paychecks-two-periods` | Pay Category | ASC |
| `bp-consecutive-day-streaks` | Streak Length | DESC |
| `bp-deduction-breakdown` | Total Deductions | DESC |
| `bp-denied-expenses` | Denied Amount | DESC |
| `bp-department-directory` | Department | ASC |
| `bp-department-label-audit` | Affected Employees | DESC |
| `bp-department-ot-share` | OT Share % | DESC |
| `bp-department-scorecard` | Department | ASC |
| `bp-department-turnover-rate` | Turnover Rate Pct | DESC |
| `bp-departments-above-company-average-salary` | Department Average Annual Compensation | DESC |
| `bp-dol-status-per-work-location` | Total | DESC |
| `bp-duplicate-expense-flag-list` | Flagged Amount | DESC |
| `bp-earning-category-dictionary` | Category | ASC |
| `bp-earnings-by-category` | Total Earnings | ASC |
| `bp-earnings-by-department-via-scratch-join` | Total Earnings | DESC |
| `bp-earnings-by-dol-status` | Gross Earnings | DESC |
| `bp-earnings-by-pay-basis` | Gross Earnings | DESC |
| `bp-earnings-by-work-state` | Gross Earnings | DESC |
| `bp-earnings-category-mix` | Dollar Pct | DESC |
| `bp-earnings-category-trend` | Month | ASC |
| `bp-earnings-monthly-trend` | Month | ASC |
| `bp-earnings-quarter-to-date` | Gross Earnings | ASC |
| `bp-effective-mileage-rate` | Total Approved | DESC |
| `bp-employee-benefits-summary` | Total Benefit Contributions | DESC |
| `bp-employee-change-timeline` | Record Date | DESC |
| `bp-employee-change-volume` | Month | ASC |
| `bp-employee-check-detail-for-period` | Register Type | ASC |
| `bp-employee-coverage-matrix` | Gap Count | DESC |
| `bp-employee-date-summary` | Group | ASC |
| `bp-employee-day-clock-span` | Punch Date | ASC |
| `bp-employee-directory` | Name | ASC |
| `bp-employee-record-dq-flags` | Employee Code | ASC |
| `bp-employee-summary` | Group | ASC |
| `bp-employee-tax-withheld` | Total Tax Withheld | DESC |
| `bp-employee-tenure-summary` | Group | ASC |
| `bp-employees-in-inactive-seats` | Employee Code | ASC |
| `bp-employer-tax-summary` | Total Employer Tax | DESC |
| `bp-expense-approval-trim` | Trimmed Total | DESC |
| `bp-expense-by-account-category` | Approved Spend | DESC |
| `bp-expense-by-policy` | Approved Spend | DESC |
| `bp-expense-category-distribution` | Approved Spend | DESC |
| `bp-expense-concept-spend` | Approved Spend | DESC |
| `bp-expense-cycle-time` | Month | ASC |
| `bp-expense-line-size-distribution` | Band Start | ASC |
| `bp-expense-monthly-trend` | Month | ASC |
| `bp-expense-payroll-recon` | Absolute Difference | DESC |
| `bp-expense-per-capita` | Per Capita Spend | DESC |
| `bp-expense-senders-no-punches` | Employee Code | ASC |
| `bp-expenses-unreconciled-to-payroll` | Total Amount | DESC |
| `bp-fica-medicare-totals` | Total Tax | DESC |
| `bp-future-dated-position-changes` | Position Effective | ASC |
| `bp-future-hire-roster` | Hire Date | ASC |
| `bp-future-termination-roster` | Termination Date | ASC |
| `bp-gross-earnings-for-pay-period` | Total Gross Earnings | ASC |
| `bp-gross-earnings-period-compare` | Period | ASC |
| `bp-gross-earnings-summary` | Total Gross Earnings | ASC |
| `bp-gross-wages-date-window` | Total Gross Wages | ASC |
| `bp-headcount-by-dol-status` | Headcount | DESC |
| `bp-headcount-by-hire-era` | Hire Era | ASC |
| `bp-headcount-by-pay-frequency` | Headcount | DESC |
| `bp-headcount-by-union` | Headcount | DESC |
| `bp-headcount-by-work-city-country` | Headcount | DESC |
| `bp-headcount-by-works-in-state` | Headcount | DESC |
| `bp-headcount-growth-rate` | Month | ASC |
| `bp-headcount-monthly-trend` | Month | ASC |
| `bp-hires-in-range` | Month | ASC |
| `bp-hires-per-month` | Month | ASC |
| `bp-hires-projection` | Hires In Window | ASC |
| `bp-holiday-work-audit` | Total Hours | DESC |
| `bp-inactive-supervisor-audit` | Employee Code | ASC |
| `bp-inactive-without-leave-start` | Employee Code | ASC |
| `bp-labor-allocation-coverage` | Headcount | DESC |
| `bp-labor-allocation-dictionary` | Usage Count | DESC |
| `bp-labor-allocation-resolution` | Employee Code | ASC |
| `bp-labor-allocation-rollup` | Active Headcount | DESC |
| `bp-long-shift-days` | Daily Hours | DESC |
| `bp-median-annual-salary` | Median Annual Salary | ASC |
| `bp-median-hourly-rate-by-state-union` | Works In State | ASC |
| `bp-median-salary-by-position-level` | Position Level | ASC |
| `bp-mileage-summary` | Approved Amount | ASC |
| `bp-missing-emergency-contacts` | Employee Code | ASC |
| `bp-missing-receipts` | Missing Receipts | DESC |
| `bp-missing-supervisor-exceptions` | Employee Code | ASC |
| `bp-name-identity-audit` | Employee Code | ASC |
| `bp-negative-paycheck-employees` | Pay Period End | DESC |
| `bp-net-pay-summary` | Total Net Pay | ASC |
| `bp-new-hire-90d-attrition` | Hires In Window | ASC |
| `bp-night-shift-share` | Night Share Pct | DESC |
| `bp-orphan-register-codes` | Pay Items | DESC |
| `bp-ot-two-basis-tieout` | Absolute Difference | DESC |
| `bp-overtime-by-department` | Earnings | DESC |
| `bp-overtime-hours-dollars` | Overtime Dollars | DESC |
| `bp-paid-hours-summary` | Total Paid Hours | ASC |
| `bp-pairs-per-day-distribution` | Pairs Per Day | ASC |
| `bp-pay-timing-exceptions` | Exception Type | ASC |
| `bp-payout-request-summary` | Payout Requests | ASC |
| `bp-payroll-codes-unmapped` | Type Code | ASC |
| `bp-payroll-duplicate-line-audit` | Duplicate Count | DESC |
| `bp-payroll-line-counts` | Pay Items | ASC |
| `bp-payroll-setup-gap-scorecard` | Gap Count | DESC |
| `bp-payroll-summary` | Group | ASC |
| `bp-pending-timeoff-queue` | Days Waiting | DESC |
| `bp-placeholder-category-audit` | Distinct Earn Codes | ASC |
| `bp-position-change-activity` | Change Events | DESC |
| `bp-post-termination-accrual-audit` | Accrual Date | DESC |
| `bp-post-termination-changes` | Post-Termination Changes | DESC |
| `bp-post-termination-pay-audit` | Total Earnings | DESC |
| `bp-punch-anomalies` | Punch Date | DESC |
| `bp-punch-coverage-gap` | Active Employees | ASC |
| `bp-punch-pair-summary` | Group | ASC |
| `bp-punch-plus-timeoff-overlap` | Overlap Days | DESC |
| `bp-punch-source-audit` | Month | ASC |
| `bp-punch-type-mix` | Pair Count | DESC |
| `bp-records-per-status` | Employee Records | DESC |
| `bp-register-type-breakdown` | Total Amount | DESC |
| `bp-regular-vs-premium-hours-ratio` | Month | ASC |
| `bp-rehired-employees` | Rehire Date | DESC |
| `bp-retroactive-change-share` | Retroactive Changes | DESC |
| `bp-salary-exact-median` | Group | ASC |
| `bp-salary-threshold-count` | Pay Type | ASC |
| `bp-salary-threshold-directory` | Annual Compensation | DESC |
| `bp-shift-differential-spend` | Shift Differential Dollars | ASC |
| `bp-supervision-anomalies` | Anomaly Type | ASC |
| `bp-supervisor-chain-depth` | Chain Depth | ASC |
| `bp-tenure-band-distribution` | Band Start | ASC |
| `bp-termination-family` | Termination Date | DESC |
| `bp-timecard-earnings-all-history` | Total Earnings | DESC |
| `bp-timecard-earnings-summary` | Group | ASC |
| `bp-timecard-vs-register-recon` | Absolute Difference | DESC |
| `bp-timeoff-by-leave-type` | Approved Hours | DESC |
| `bp-timeoff-denial-rate` | Denial Rate Pct | DESC |
| `bp-timeoff-funnel` | Request Count | DESC |
| `bp-timeoff-lead-time` | Median Lead Days | DESC |
| `bp-timeoff-seasonality` | Month | ASC |
| `bp-timeoff-vs-paid-leave-tieout` | Absolute Difference | DESC |
| `bp-timeoff-withdrawal-rate` | Withdrawal Rate Pct | DESC |
| `bp-total-earnings-by-department` | Total Earnings | DESC |
| `bp-unpaid-active-employees` | Employee Code | ASC |
| `bp-unused-leave-roster` | Balance Hours | DESC |
| `bp-upcoming-anniversaries` | Anniversary Day of Year | ASC |
| `bp-upcoming-timeoff-roster` | Request Date | ASC |
| `bp-weekend-work-share` | Month | ASC |
| `bp-weekly-overtime-hours` | Overtime Hours | DESC |
| `bp-worked-hours-daily` | Punch Date | ASC |
| `bp-workforce-composition` | Headcount | DESC |
| `bp-workforce-diversity-profile` | Headcount | DESC |
| `bp-workforce-kpi-bundle` | Active Headcount | ASC |
| `bp-workforce-movements` | Movement Date | DESC |
| `bp-ytd-earnings-per-employee` | Gross Earnings | DESC |
| `bp-zero-net-pay-employees` | Employee Code | ASC |
