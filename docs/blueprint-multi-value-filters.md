# Multi-value blueprint filters

Categorical slots explicitly marked `multiple: true` accept a JSON list. Values within one slot are ORed; different filter predicates retain the blueprint's authored AND/OR structure. Single text values remain accepted and normalize to a one-element list.

```yaml
- name: employee_status
  type: enum
  multiple: true
  required: false
  default: [Active, On Leave]
  enum_values: [Active, On Leave, Terminated, All]
```

For example, `slot_bindings: {employee_status: [Active, On Leave], department: [Sales, Engineering]}` selects the two statuses within the two departments. Omitting `employee_status` on a current-workforce blueprint selects Active and On Leave. Explicit values replace the default; they are not appended to it. `All`, where declared, must be supplied alone. Other employee-status filters in payroll and historical/event reports retain their authored omission behavior.

The blueprint authors the IN predicate explicitly. Lane5 validates and binds the list at that site, without changing the SQL operator:

```sql
-- Authored
WHERE e.employee_status IN ({employee_status})
  AND e.department_name IN ({department})

-- Bound
WHERE e.employee_status IN ('Active', 'On Leave')
  AND e.department_name IN ('Sales', 'Engineering')
```

The unfiltered branch is also explicit in the blueprint: `'All' IN ({employee_status})`. A list supplied to an equality placeholder is rejected; there is no implicit equality-to-membership conversion. Values are typed SQL literals, never SQL fragments; source joins, aggregation, ranking and date predicates are unchanged.

The rollout covers 121 slots in 80 blueprints. Eleven current-workforce blueprints now have the explicit two-status default. Supported filters include departments, employee/event statuses, event types, earning/register codes, location/state/country, pay class/basis, employment type, supervisor and category filters with authored IN predicates. Fixed business filters remain fixed. Grouping, metric, comparison and date-basis controls, temporal slots, substring searches, and single-employee comparison inputs remain scalar.

`searchBlueprints` cards and retrieval hints identify list-capable slots. `getBlueprint` exposes the list value schema, defaults and allowed enum values. Enum values are validated individually. Entity/string values use a scoped, targeted lookup for the requested set; an unmatched value never results in a silently shortened filter. Malformed lists return a retryable argument error before warehouse execution, rather than asking the user to format a slot. Lists accept 1–100 nonempty strings; duplicate resolved values are deduplicated. Nested containers are rejected.

This contract requires the matching Lane5 slot parser, resolver, binder and schema changes alongside the corpus release. Execution receipts retain the resolved list and resulting logical SQL.
