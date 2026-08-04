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


# ===========================================================================
# mcp_projection.hidden_columns — catalog-authored, UNCONDITIONAL projection
# (drops RLS-enforced / internal physical columns regardless of column_scope)
# ===========================================================================

# A catalog entry that physically exposes client_code / proc_center / version but
# authors an mcp_projection hiding them (mirrors the real labor_allocation shape:
# hidden columns also appear defensively in grain + a join_key own-side).
_HIDDEN_CATALOG = {
    "wh.labor_allocation": {
        "database": "wh",
        "table": "labor_allocation",
        "description": "Labor allocation reference.",
        "grain": ["code", "join_key", "client_code", "proc_center"],
        "primary_key": ["code", "client_code"],
        "join_keys": [
            {"column": "join_key", "joins": "employee.join_key", "cardinality": "N:1"},
            {"column": "client_code", "joins": "employee.client_code", "cardinality": "N:1"},
        ],
        "columns": {
            "code": {"type": "String", "description": "Allocation code."},
            "join_key": {"type": "String", "description": "Join key."},
        },
        "mcp_projection": {
            "hidden_columns": ["client_code", "proc_center", "version"],
        },
    },
}

# Introspection surfaces the physical RLS/engine columns the catalog hides.
_INTROSPECTED_HIDDEN = [
    {"name": "code", "type": "String", "comment": ""},
    {"name": "join_key", "type": "String", "comment": ""},
    {"name": "client_code", "type": "String", "comment": ""},
    {"name": "proc_center", "type": "String", "comment": ""},
    {"name": "version", "type": "UInt64", "comment": ""},
]


class TestHiddenColumnsProjection:
    def test_hidden_columns_dropped_when_scope_none(self):
        """stdio/local-trust (scope None, no enforcement) still drops hidden columns."""
        result = build_table_schema_response(
            database="wh",
            table="labor_allocation",
            introspected_columns=_INTROSPECTED_HIDDEN,
            catalog=_HIDDEN_CATALOG,
            scope=None,
        )
        # The RLS/engine columns are gone; the ordinary columns are untouched.
        assert _col_names(result) == {"code", "join_key"}

    def test_hidden_columns_dropped_when_scope_empty_allow_all(self):
        """Allow-all (empty frozenset, no scope enforcement) still drops hidden columns."""
        result = build_table_schema_response(
            database="wh",
            table="labor_allocation",
            introspected_columns=_INTROSPECTED_HIDDEN,
            catalog=_HIDDEN_CATALOG,
            scope=frozenset(),
        )
        assert _col_names(result) == {"code", "join_key"}

    def test_hidden_columns_dropped_when_scope_enforced(self):
        """Even a scope that WOULD permit the hidden columns cannot surface them."""
        scope = frozenset(
            {
                "wh.labor_allocation.code",
                "wh.labor_allocation.join_key",
                "wh.labor_allocation.client_code",
                "wh.labor_allocation.proc_center",
                "wh.labor_allocation.version",
            }
        )
        result = build_table_schema_response(
            database="wh",
            table="labor_allocation",
            introspected_columns=_INTROSPECTED_HIDDEN,
            catalog=_HIDDEN_CATALOG,
            scope=scope,
        )
        assert _col_names(result) == {"code", "join_key"}

    def test_non_hidden_column_unaffected(self):
        """A non-hidden column with a catalog overlay is preserved verbatim."""
        result = build_table_schema_response(
            database="wh",
            table="labor_allocation",
            introspected_columns=_INTROSPECTED_HIDDEN,
            catalog=_HIDDEN_CATALOG,
            scope=None,
        )
        by_name = {c["name"]: c for c in result["columns"]}
        assert by_name["code"]["description"] == "Allocation code."
        assert by_name["join_key"]["type"] == "String"

    def test_hidden_names_swept_from_grain_pk_and_join_keys(self):
        """Defensive: a hidden name must not leak via grain / primary_key / join_keys."""
        result = build_table_schema_response(
            database="wh",
            table="labor_allocation",
            introspected_columns=_INTROSPECTED_HIDDEN,
            catalog=_HIDDEN_CATALOG,
            scope=None,
        )
        assert result["grain"] == ["code", "join_key"]
        assert result["primary_key"] == ["code"]
        assert [jk["column"] for jk in result["join_keys"]] == ["join_key"]

    def test_no_mcp_projection_leaves_columns_intact(self):
        """A catalog entry without mcp_projection drops nothing (regression guard)."""
        catalog = {
            "wh.plain": {
                "database": "wh",
                "table": "plain",
                "columns": {"code": {"type": "String"}},
            }
        }
        introspected = [
            {"name": "code", "type": "String", "comment": ""},
            {"name": "client_code", "type": "String", "comment": ""},
        ]
        result = build_table_schema_response(
            database="wh",
            table="plain",
            introspected_columns=introspected,
            catalog=catalog,
            scope=None,
        )
        assert _col_names(result) == {"code", "client_code"}


# ===========================================================================
# Templated (column_pattern) join_keys entry — the labor_allocation collapse.
# ONE join_key entry stands in for labor_allocation_key_1..N (all joining
# labor_allocation.join_key identically). It must be emitted COMPACTLY (one
# entry, not expanded to N), coexist with singular-`column` join_keys, and be
# scope-filtered on "any underlying slot in scope" without ever throwing on the
# absent singular `column`.
# ===========================================================================

# index_range kept small ([1, 3]) for readable assertions; the real catalog
# uses [1, 20]. A singular DepartmentCode join_key coexists in the same table.
_PATTERN_CATALOG = {
    "wh.employee": {
        "database": "wh",
        "table": "employee",
        "description": "Employee with a templated labor-allocation join.",
        "grain": ["EmployeeCode"],
        "primary_key": ["EmployeeCode"],
        "join_keys": [
            {
                "column": "DepartmentCode",
                "joins": "department.DepartmentCode",
                "cardinality": "N:1",
            },
            {
                "column_pattern": "labor_allocation_key_{n}",
                "index_range": [1, 3],
                "joins": "labor_allocation.join_key",
                "join_on": "labor_allocation_key_{n} = join_key",
                "cardinality": "N:1",
                # implicit_join_scope must still be stripped from a pattern entry.
                "implicit_join_scope": ["client_code", "proc_center"],
                "description": "Each labor_allocation_key_{n} (n=1..3) is a distinct slot.",
            },
        ],
        "columns": {
            "EmployeeCode": {"type": "String", "description": "Employee id."},
            "DepartmentCode": {"type": "Nullable(String)", "description": "Dept code."},
            "labor_allocation_key_1": {"type": "Nullable(String)", "description": "Slot 1."},
            "labor_allocation_key_2": {"type": "Nullable(String)", "description": "Slot 2."},
            "labor_allocation_key_3": {"type": "Nullable(String)", "description": "Slot 3."},
        },
    },
}

_INTROSPECTED_PATTERN = [
    {"name": "EmployeeCode", "type": "String", "comment": ""},
    {"name": "DepartmentCode", "type": "Nullable(String)", "comment": ""},
    {"name": "labor_allocation_key_1", "type": "Nullable(String)", "comment": ""},
    {"name": "labor_allocation_key_2", "type": "Nullable(String)", "comment": ""},
    {"name": "labor_allocation_key_3", "type": "Nullable(String)", "comment": ""},
]

_PATTERN_FULL_SCOPE = frozenset(
    f"wh.employee.{c}" for c in _PATTERN_CATALOG["wh.employee"]["columns"]
)


def _pattern_entry(join_keys):
    """The single templated join_key entry from an emitted join_keys list."""
    patterns = [jk for jk in join_keys if jk.get("column_pattern")]
    assert len(patterns) == 1, patterns
    return patterns[0]


def _build_pattern(scope):
    return build_table_schema_response(
        database="wh",
        table="employee",
        introspected_columns=_INTROSPECTED_PATTERN,
        catalog=_PATTERN_CATALOG,
        scope=scope,
    )


class TestPatternJoinKeyEmittedCompactly:
    def test_emitted_as_one_entry_not_expanded(self):
        """The pattern is ONE join_key carrying the range + join_on template —
        never expanded to labor_allocation_key_1..3 as separate entries."""
        result = _build_pattern(None)
        jks = result["join_keys"]
        # Two entries total: the singular DepartmentCode + the ONE pattern entry.
        assert len(jks) == 2
        entry = _pattern_entry(jks)
        assert entry["column_pattern"] == "labor_allocation_key_{n}"
        assert entry["index_range"] == [1, 3]
        assert entry["join_on"] == "labor_allocation_key_{n} = join_key"
        assert entry["joins"] == "labor_allocation.join_key"
        assert entry["cardinality"] == "N:1"
        # No underlying slot leaked as its own entry, and no singular `column` key.
        assert "column" not in entry
        assert not any(jk.get("column", "").startswith("labor_allocation_key_") for jk in jks)

    def test_implicit_join_scope_stripped_from_pattern_entry(self):
        """A-H2: the RLS tenant columns must be stripped from the pattern entry too,
        and the scalar join_on template must not throw the hidden-name sweep."""
        entry = _pattern_entry(_build_pattern(None)["join_keys"])
        assert "implicit_join_scope" not in entry
        # Scalar join_on template survives intact (not blanked / not listified).
        assert entry["join_on"] == "labor_allocation_key_{n} = join_key"

    def test_singular_entry_coexists_untouched(self):
        result = _build_pattern(None)
        singular = [jk for jk in result["join_keys"] if jk.get("column") == "DepartmentCode"]
        assert len(singular) == 1
        assert singular[0]["joins"] == "department.DepartmentCode"


class TestPatternJoinKeyScopeSurvival:
    def test_survives_scope_none(self):
        assert _pattern_entry(_build_pattern(None)["join_keys"])

    def test_survives_scope_allow_all_empty(self):
        assert _pattern_entry(_build_pattern(frozenset())["join_keys"])

    def test_survives_enforced_full_scope(self):
        result = _build_pattern(_PATTERN_FULL_SCOPE)
        assert len(result["join_keys"]) == 2
        assert _pattern_entry(result["join_keys"])

    def test_survives_when_only_one_slot_in_scope(self):
        """Pattern kept when AT LEAST ONE underlying slot is in scope (here only
        labor_allocation_key_2); the singular DepartmentCode (out of scope) drops."""
        scope = frozenset(
            {"wh.employee.EmployeeCode", "wh.employee.labor_allocation_key_2"}
        )
        result = _build_pattern(scope)
        cols = [jk.get("column") for jk in result["join_keys"]]
        assert "DepartmentCode" not in cols  # singular own-side out of scope -> dropped
        assert _pattern_entry(result["join_keys"])  # pattern survives on one in-scope slot

    def test_dropped_only_when_all_slots_out_of_scope(self):
        """Every labor_allocation_key_* out of scope -> pattern dropped, while the
        in-scope singular DepartmentCode entry survives (independent filtering)."""
        scope = frozenset(
            {"wh.employee.EmployeeCode", "wh.employee.DepartmentCode"}
        )
        result = _build_pattern(scope)
        assert not any(jk.get("column_pattern") for jk in result["join_keys"])
        assert [jk.get("column") for jk in result["join_keys"]] == ["DepartmentCode"]
