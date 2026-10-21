"""Public Trino errors contain only allowlisted names and authored hints.

Coordinator text can contain physical names, SQL literals and data values.
Never read or forward it, including for unknown error codes. Query IDs remain
in the execution log for operator correlation.
"""

from __future__ import annotations

# Symbolic coordinator error_name -> concrete rewrite advice. Kept to one
# sentence each; the goal is a first-retry fix, not a tutorial. Names come
# from io.trino.spi.StandardErrorCode (errorName on the wire).
_HINTS: dict[str, str] = {
    "FUNCTION_NOT_FOUND": (
        "Write ClickHouse SQL and check the function signature and catalog input types with getTableSchema. "
        "Check the submitted expression; do not use Trino-only function names or substitute estimates for exact results."
    ),
    "COLUMN_NOT_FOUND": (
        "The column does not exist on that logical table; call getTableSchema for "
        "the exact column names."
    ),
    "AMBIGUOUS_COLUMN": "Qualify the column with its table alias.",
    "AMBIGUOUS_COLUMN_NAME": "Qualify the column with its table alias.",
    "TABLE_NOT_FOUND": (
        "Call listTables for the exact logical table name and qualify it as "
        "database.table; if the table is listed there, the reporting view may be "
        "missing on the coordinator — ask the operator to re-run the view-apply "
        "step (views vanish on coordinator restart)."
    ),
    "SCHEMA_NOT_FOUND": (
        "Physical routing or catalog misconfiguration; this is an operator problem "
        "— report it, do not keep retrying with renamed identifiers."
    ),
    "CATALOG_NOT_FOUND": (
        "Physical routing or catalog misconfiguration; this is an operator problem "
        "— report it, do not keep retrying with renamed identifiers."
    ),
    "SYNTAX_ERROR": (
        "Check the SQL syntax; keep the logical SQL "
        "ClickHouse-compatible — the translation layer emits the Trino SQL."
    ),
    "EXPRESSION_NOT_IN_GROUP_BY": (
        "Every non-aggregated SELECT expression must be a GROUP BY key; GROUP BY "
        "the exact expression you SELECT."
    ),
    "NOT_AN_AGGREGATE": (
        "Every non-aggregated SELECT expression must be a GROUP BY key; GROUP BY "
        "the exact expression you SELECT."
    ),
    "TYPE_MISMATCH": (
        "The operand types are incompatible; cast explicitly and compare a "
        "date/timestamp column to a matching ClickHouse literal (toDate('YYYY-MM-DD') / "
        "toDateTime('YYYY-MM-DD hh:mm:ss'))."
    ),
    "INVALID_CAST_ARGUMENT": (
        "The value cannot be cast to the target type; cast explicitly and compare "
        "a date/timestamp column to a matching ClickHouse literal (toDate('YYYY-MM-DD') / "
        "toDateTime('YYYY-MM-DD hh:mm:ss'))."
    ),
    "INVALID_FUNCTION_ARGUMENT": (
        "Check the ClickHouse function signature, argument types, and supported overloads. "
        "For exact medians use quantileExactLow(0.5)(salary) and quantileExactHigh(0.5)(salary)."
    ),
    "DIVISION_BY_ZERO": (
        "Guard the divisor with NULLIF(divisor, 0) so the row reads NULL instead "
        "of failing."
    ),
    "EXCEEDED_LOCAL_MEMORY_LIMIT": (
        "Narrow the date range or tenant scope, aggregate earlier, and avoid huge "
        "GROUP BY/DISTINCT cardinality; the coordinator-level caps are "
        "deployment-sized (see docs/trino.md memory section), so do not retry the "
        "same shape."
    ),
    "GENERIC_INSUFFICIENT_RESOURCES": (
        "Narrow the date range or tenant scope, aggregate earlier, and avoid huge "
        "GROUP BY/DISTINCT cardinality; the coordinator-level caps are "
        "deployment-sized (see docs/trino.md memory section), so do not retry the "
        "same shape."
    ),
    "QUERY_TIMEOUT": (
        "Narrow the date range or tenant scope, aggregate earlier, and avoid huge "
        "GROUP BY/DISTINCT cardinality; the coordinator-level caps are "
        "deployment-sized (see docs/trino.md memory section), so do not retry the "
        "same shape."
    ),
    "EXCEEDED_TIME_LIMIT": (
        "Narrow the date range or tenant scope, aggregate earlier, and avoid huge "
        "GROUP BY/DISTINCT cardinality; the coordinator-level caps are "
        "deployment-sized (see docs/trino.md memory section), so do not retry the "
        "same shape."
    ),
    "PERMISSION_DENIED": (
        "The effective identity lacks access to that object; stay within the "
        "tables returned by listTables and check with the operator about view/pin "
        "provisioning."
    ),
    "ACCESS_DENIED": (
        "The effective identity lacks access to that object; stay within the "
        "tables returned by listTables and check with the operator about view/pin "
        "provisioning."
    ),
}

# Trino 483 StandardErrorCode names. Retain older aliases above for compatible
# coordinators, but route the actual current names to the same safe guidance.
_HINTS.update(
    {
        "AMBIGUOUS_NAME": _HINTS["AMBIGUOUS_COLUMN"],
        "EXPRESSION_NOT_AGGREGATE": _HINTS["NOT_AN_AGGREGATE"],
        "OPERATOR_NOT_FOUND": _HINTS["TYPE_MISMATCH"],
        "INVALID_LITERAL": _HINTS["INVALID_CAST_ARGUMENT"],
        "NUMERIC_VALUE_OUT_OF_RANGE": "A numeric calculation overflowed; use an appropriate wider numeric type and check the input range.",
        "NOT_SUPPORTED": "This query shape is unsupported on the Trino path; simplify it or use supported SQL constructs.",
        "SUBQUERY_MULTIPLE_ROWS": "A scalar subquery returned multiple rows; make its result single-valued without discarding meaningful matches.",
        "VIEW_IS_STALE": "The reporting view no longer matches its source schema; ask the operator to refresh its definition.",
        "INVALID_VIEW": "The reporting view definition is invalid; ask the operator to repair it.",
        "INVALID_ROW_FILTER": "The source authorization policy is invalid; ask the operator to repair it. Do not remove permission checks.",
        "INVALID_COLUMN_MASK": "The source column policy is invalid; ask the operator to repair it.",
        "USER_CANCELED": "The query was canceled; it was not automatically retried.",
        "ADMINISTRATIVELY_KILLED": "The operator stopped the query; check with the operator before retrying.",
        "ABANDONED_QUERY": "The coordinator abandoned the query after losing contact with the client; check connectivity before retrying.",
        "QUERY_QUEUE_FULL": "The query queue is full; wait for capacity before retrying.",
        "CLUSTER_OUT_OF_MEMORY": "The cluster has insufficient memory; ask the operator to check capacity and reduce the query's data volume.",
        "CATALOG_UNAVAILABLE": "A source catalog is unavailable; ask the operator to check source connectivity and configuration.",
    }
)
for _name in (
    "EXCEEDED_GLOBAL_MEMORY_LIMIT",
    "EXCEEDED_CPU_LIMIT",
    "EXCEEDED_SPILL_LIMIT",
    "EXCEEDED_SCAN_LIMIT",
    "EXCEEDED_FUNCTION_MEMORY_LIMIT",
):
    _HINTS[_name] = _HINTS["EXCEEDED_LOCAL_MEMORY_LIMIT"]

def rewrite_trino_error(exc) -> str:
    """Return safe guidance without relying on downstream redaction."""
    try:
        name = getattr(exc, "error_name", None)
        hint = _HINTS.get(name) if isinstance(name, str) else None
    except Exception:  # noqa: BLE001 — error reporting must never raise
        hint = None
    if hint is None:
        return "Trino rejected the query. Contact the operator if the problem persists."
    return f"{name}. Hint: {hint}"
