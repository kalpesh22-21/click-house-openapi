# Earnings source selection

Unqualified gross earnings for a year/month/range uses payroll register `EARN`
amounts by payment date, across authorized facts without an implicit employee-status
filter. Explicit timecard/in-progress earnings uses `total_earning.earn_amount`
with `FINAL`, by work/punch date unless explicit period coverage overrides it.
These are different measures; missing recent payroll rows do not authorize a switch.
Combining stages requires verified finalization and overlap handling.

The shared knowledge entry is `kn-earnings-source-selection`; both source catalogs
carry the rule so review sees it even if execution selected the wrong relation.

Blueprint migration:

- `bp-gross-earnings-summary` now requires a `period_range` (half-open payment-date
  start/end). Callers that supplied the previous single `period` ending date must
  migrate to `bp-gross-earnings-for-pay-period`, whose ending date is required.
- `bp-gross-earnings-for-pay-period` preserves explicit period-ending semantics.
- `bp-timecard-earnings-summary` explicitly applies the catalog's `FINAL` rule and uses searched
  CASE for column-valued grouping branches (ClickHouse 24.8 rejects the simple CASE).
- `bp-payroll-summary` remains the richer grouped payment-date summary.

Rehydrate from current exports after deployment. Corpus counts: 189 blueprints,
12 knowledge entries, 11 catalog files. Fingerprints are in the tracked sidecars;
do not reuse the older 188-blueprint seed. No MySQL templates changed.

Validation: 481 corpus/parity tests pass, including execution of the gross template
against opposing payment/work dates, reversals, non-EARN lines and terminated
employees. Live DeepSeek/MCP evidence is recorded in lane5's
`docs/verification/earnings-source-selection-2026-11-02.md`.
