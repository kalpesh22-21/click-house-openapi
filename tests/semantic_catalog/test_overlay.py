"""Unit tests for app/semantic_catalog/overlay.py (D83/D84).

Pure unit tests: feed a fake introspection result + a fake (small,
hand-authored) semantic catalog dict — no live ClickHouse, no dependency on
the real copied-in YAML files under app/semantic_catalog/data/. Mirrors the
shape of the real catalog (grain, temporal, primary_key, join_keys, measures,
rules, ambiguities) closely enough to exercise every scope-filter rule in
mcp-overlay-design.md §1.4.
"""

from __future__ import annotations

from app.semantic_catalog.overlay import build_table_schema_response

# ---------------------------------------------------------------------------
# Fake catalog — two tables, mirroring employee/payroll's real shape closely
# enough to exercise every §1.4 filtering rule, including a cross-table
# ambiguity reference (the "salary" example from mcp-overlay-design.md).
# ---------------------------------------------------------------------------

_CATALOG = {
    "wh.employee": {
        "database": "wh",
        "table": "employee",
        "description": "Employee record, one row per employee.",
        "grain": ["EmployeeCode"],
        "temporal": {"dimensions": [], "default_pin": None},
        "primary_key": ["EmployeeCode"],
        "join_keys": [
            {"column": "EmployeeCode", "joins": "payroll.EmployeeCode", "cardinality": "1:N"},
        ],
        "columns": {
            "EmployeeCode": {
                "type": "String",
                "description": "Stable unique employee identifier.",
                "synonyms": ["emp_id"],
            },
            "AnnualSalary": {
                "type": "Nullable(Decimal(18, 6))",
                "unit": "USD",
                "description": "Annual salary amount.",
                "sensitive": True,
            },
            "EmployeeStatus": {
                "type": "Nullable(String)",
                "description": "Current employee status code.",
                "values": {"A": "active", "T": "terminated"},
            },
        },
        "measures": {
            "avg_salary": {"column": "AnnualSalary", "agg": "avg", "defined_over": "n/a"},
        },
        "rules": [
            {
                "id": "active_employee",
                "predicate": "EmployeeStatus = 'A'",
                "applies_when": "active employees",
                "description": "Restrict to active employees.",
            },
            {
                "id": "salary_gate",
                "predicate": "AnnualSalary > 0",
                "applies_when": "salary questions",
                "description": "Exclude zero/blank salary rows.",
            },
        ],
        "ambiguities": [
            {
                "term": "headcount",
                "resolves_to": ["COUNT(DISTINCT EmployeeCode)"],
                "default": "active_only",
                "clarify_if": "user asks for historical headcount",
            },
            {
                "term": "salary",
                "resolves_to": [
                    "AnnualSalary",
                    "payroll gross earnings = SUM(Amount) WHERE RegisterType = 'EARN'",
                ],
                "default": "planned_annual_salary",
                "clarify_if": "planned vs actual pay",
            },
        ],
    },
    "wh.payroll": {
        "database": "wh",
        "table": "payroll",
        "description": "Payroll register line items.",
        "grain": [],
        "temporal": {
            "dimensions": [
                {
                    "name": "pay_period",
                    "role": "period",
                    "range": {"start_col": "PayPeriodStartDate", "end_col": "PayPeriodEndDate"},
                },
                {"name": "pay_event", "role": "event", "date_col": "PayDate"},
            ],
            "default_pin": "pay_period",
        },
        "primary_key": [],
        "join_keys": [
            {"column": "EmployeeCode", "joins": "employee.EmployeeCode", "cardinality": "N:1"},
        ],
        "columns": {
            "EmployeeCode": {"type": "String", "description": "Employee join key."},
            "Amount": {"type": "Nullable(Decimal(18, 6))", "unit": "USD", "description": "Line amount."},
            "RegisterType": {"type": "String", "description": "Register category."},
            "PayPeriodStartDate": {"type": "DateTime", "description": "Period start."},
            "PayPeriodEndDate": {"type": "DateTime", "description": "Period end."},
            "PayDate": {"type": "DateTime", "description": "Pay/check date."},
        },
        "measures": {
            "gross_earnings": {"column": "Amount", "agg": "sum", "defined_over": "RegisterType = 'EARN'"},
        },
        "rules": [],
        "ambiguities": [],
    },
}

_FULL_SCOPE = frozenset(
    f"{key}.{col}" for key, entry in _CATALOG.items() for col in entry["columns"]
)

# Excludes wh.employee.AnnualSalary and wh.payroll.Amount only.
_RESTRICTED_SCOPE = frozenset(
    s for s in _FULL_SCOPE if not s.endswith(".AnnualSalary") and not s.endswith(".payroll.Amount")
)

# Excludes wh.employee.EmployeeCode (to test wholesale grain/primary_key/join_key drop).
_NO_EMPLOYEE_CODE_SCOPE = frozenset(s for s in _FULL_SCOPE if s != "wh.employee.EmployeeCode")

_INTROSPECTED_EMPLOYEE = [
    {"name": "EmployeeCode", "type": "String", "comment": ""},
    {"name": "AnnualSalary", "type": "Nullable(Decimal(18, 6))", "comment": ""},
    {"name": "EmployeeStatus", "type": "Nullable(String)", "comment": ""},
]

_INTROSPECTED_PAYROLL = [
    {"name": "EmployeeCode", "type": "String", "comment": ""},
    {"name": "Amount", "type": "Nullable(Decimal(18, 6))", "comment": ""},
    {"name": "RegisterType", "type": "String", "comment": ""},
    {"name": "PayPeriodStartDate", "type": "DateTime", "comment": ""},
    {"name": "PayPeriodEndDate", "type": "DateTime", "comment": ""},
    {"name": "PayDate", "type": "DateTime", "comment": ""},
]


def _col_names(response: dict) -> set[str]:
    return {c["name"] for c in response["columns"]}


# ===========================================================================
# (b) In-scope columns carry the full catalog overlay
# ===========================================================================

class TestColumnOverlayCarriesCatalogFields:
    def test_full_scope_carries_overlay_fields(self):
        result = build_table_schema_response(
            database="wh",
            table="employee",
            introspected_columns=_INTROSPECTED_EMPLOYEE,
            catalog=_CATALOG,
            scope=_FULL_SCOPE,
        )
        by_name = {c["name"]: c for c in result["columns"]}
        assert by_name["AnnualSalary"]["description"] == "Annual salary amount."
        assert by_name["AnnualSalary"]["unit"] == "USD"
        assert by_name["AnnualSalary"]["sensitive"] is True
        assert by_name["EmployeeCode"]["synonyms"] == ["emp_id"]
        assert by_name["EmployeeStatus"]["values"] == {"A": "active", "T": "terminated"}
        # client_defined defaults to False when the YAML entry doesn't declare it.
        assert by_name["EmployeeCode"]["client_defined"] is False


# ===========================================================================
# (c) Empty scope == allow-all: nothing dropped
# ===========================================================================

class TestEmptyScopeAllowsAll:
    def test_empty_scope_keeps_everything(self):
        result = build_table_schema_response(
            database="wh",
            table="employee",
            introspected_columns=_INTROSPECTED_EMPLOYEE,
            catalog=_CATALOG,
            scope=frozenset(),
        )
        assert _col_names(result) == {"EmployeeCode", "AnnualSalary", "EmployeeStatus"}
        assert result["grain"] == ["EmployeeCode"]
        assert result["primary_key"] == ["EmployeeCode"]
        assert len(result["rules"]) == 2
        assert len(result["ambiguities"]) == 2
        assert result["ambiguities"][1]["resolves_to"] == [
            "AnnualSalary",
            "payroll gross earnings = SUM(Amount) WHERE RegisterType = 'EARN'",
        ]

    def test_none_scope_keeps_everything(self):
        """current_scope is None -> stdio/local-trust -> no filtering at all."""
        result = build_table_schema_response(
            database="wh",
            table="employee",
            introspected_columns=_INTROSPECTED_EMPLOYEE,
            catalog=_CATALOG,
            scope=None,
        )
        assert _col_names(result) == {"EmployeeCode", "AnnualSalary", "EmployeeStatus"}
        assert result["grain"] == ["EmployeeCode"]


# ===========================================================================
# (a) Narrow scope: out-of-scope columns dropped + rule/ambiguity dropped
#     wholesale (never partially redacted)
# ===========================================================================

class TestNarrowScopeDropsColumnsAndBlocks:
    def test_out_of_scope_column_dropped(self):
        result = build_table_schema_response(
            database="wh",
            table="employee",
            introspected_columns=_INTROSPECTED_EMPLOYEE,
            catalog=_CATALOG,
            scope=_RESTRICTED_SCOPE,
        )
        assert _col_names(result) == {"EmployeeCode", "EmployeeStatus"}

    def test_rule_referencing_out_of_scope_column_dropped_wholesale(self):
        result = build_table_schema_response(
            database="wh",
            table="employee",
            introspected_columns=_INTROSPECTED_EMPLOYEE,
            catalog=_CATALOG,
            scope=_RESTRICTED_SCOPE,
        )
        rule_ids = {r["id"] for r in result["rules"]}
        # salary_gate predicates on AnnualSalary (out of scope) -> dropped entirely.
        assert "salary_gate" not in rule_ids
        # active_employee predicates only on EmployeeStatus (in scope) -> survives,
        # and its predicate string is NEVER partially redacted.
        assert "active_employee" in rule_ids
        active_rule = next(r for r in result["rules"] if r["id"] == "active_employee")
        assert active_rule["predicate"] == "EmployeeStatus = 'A'"

    def test_measure_referencing_out_of_scope_column_dropped(self):
        result = build_table_schema_response(
            database="wh",
            table="employee",
            introspected_columns=_INTROSPECTED_EMPLOYEE,
            catalog=_CATALOG,
            scope=_RESTRICTED_SCOPE,
        )
        assert result["measures"] == []

    def test_ambiguity_dropped_wholesale_when_all_resolves_to_out_of_scope(self):
        """'salary' resolves_to both AnnualSalary (this table) and a cross-table
        reference to payroll.Amount (via SUM(Amount) prose) — both out of scope
        under _RESTRICTED_SCOPE, so the whole ambiguity entry is dropped."""
        result = build_table_schema_response(
            database="wh",
            table="employee",
            introspected_columns=_INTROSPECTED_EMPLOYEE,
            catalog=_CATALOG,
            scope=_RESTRICTED_SCOPE,
        )
        terms = {a["term"] for a in result["ambiguities"]}
        assert "salary" not in terms
        # headcount only references EmployeeCode (in scope) -> survives untouched.
        assert "headcount" in terms

    def test_grain_and_primary_key_and_join_keys_dropped_when_key_column_out_of_scope(self):
        result = build_table_schema_response(
            database="wh",
            table="employee",
            introspected_columns=_INTROSPECTED_EMPLOYEE,
            catalog=_CATALOG,
            scope=_NO_EMPLOYEE_CODE_SCOPE,
        )
        # A partial grain/PK vector would misrepresent uniqueness -> drop wholesale.
        assert result["grain"] is None
        assert result["primary_key"] is None
        assert result["join_keys"] == []

    def test_grain_survives_when_its_column_is_in_scope(self):
        result = build_table_schema_response(
            database="wh",
            table="employee",
            introspected_columns=_INTROSPECTED_EMPLOYEE,
            catalog=_CATALOG,
            scope=_RESTRICTED_SCOPE,
        )
        assert result["grain"] == ["EmployeeCode"]
        assert result["primary_key"] == ["EmployeeCode"]
        assert result["join_keys"] == [
            {"column": "EmployeeCode", "joins": "payroll.EmployeeCode", "cardinality": "1:N"}
        ]


# ===========================================================================
# temporal.dimensions[] filtering + default_pin invalidation
# ===========================================================================

class TestTemporalDimensionFiltering:
    def test_dimension_dropped_when_its_columns_out_of_scope(self):
        scope = frozenset(
            s for s in _FULL_SCOPE if "PayPeriodStartDate" not in s and "PayPeriodEndDate" not in s
        )
        result = build_table_schema_response(
            database="wh",
            table="payroll",
            introspected_columns=_INTROSPECTED_PAYROLL,
            catalog=_CATALOG,
            scope=scope,
        )
        dim_names = {d["name"] for d in result["temporal"]["dimensions"]}
        assert "pay_period" not in dim_names
        assert "pay_event" in dim_names
        # default_pin named the dropped dimension -> becomes meaningless -> None.
        assert result["temporal"]["default_pin"] is None

    def test_default_pin_survives_when_its_dimension_survives(self):
        result = build_table_schema_response(
            database="wh",
            table="payroll",
            introspected_columns=_INTROSPECTED_PAYROLL,
            catalog=_CATALOG,
            scope=_FULL_SCOPE,
        )
        assert result["temporal"]["default_pin"] == "pay_period"


# ===========================================================================
# (d) Uncatalogued table -> catalogued:false, structural-only, still scope-filtered
# ===========================================================================

class TestUncataloguedTable:
    def test_uncatalogued_table_is_structural_only(self):
        introspected = [
            {"name": "id", "type": "UInt64", "comment": ""},
            {"name": "raw_value", "type": "String", "comment": "legacy"},
        ]
        result = build_table_schema_response(
            database="wh",
            table="mystery_table",
            introspected_columns=introspected,
            catalog=_CATALOG,
            scope=None,
        )
        assert result["catalogued"] is False
        assert result["description"] is None
        assert result["grain"] is None
        assert result["temporal"] is None
        assert result["primary_key"] is None
        assert result["join_keys"] is None
        assert result["measures"] is None
        assert result["rules"] is None
        assert result["ambiguities"] is None
        assert _col_names(result) == {"id", "raw_value"}
        for col in result["columns"]:
            assert col["description"] is None
            assert col["sensitive"] is False

    def test_uncatalogued_table_columns_still_scope_filtered(self):
        introspected = [
            {"name": "id", "type": "UInt64", "comment": ""},
            {"name": "secret", "type": "String", "comment": ""},
        ]
        scope = frozenset({"wh.mystery_table.id"})
        result = build_table_schema_response(
            database="wh",
            table="mystery_table",
            introspected_columns=introspected,
            catalog=_CATALOG,
            scope=scope,
        )
        assert _col_names(result) == {"id"}
