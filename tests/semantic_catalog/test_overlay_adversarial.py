"""ADVERSARIAL negative tests for app/semantic_catalog/overlay.py (D83/D84).

Complements tests/semantic_catalog/test_overlay.py (which covers the happy-path
/ single-hop drop rules) with hostile inputs targeting the two riskiest parts
of the scope-filter pipeline (mcp-overlay-design.md Section 1.4):

  1. Column-list scope-filter case sensitivity (D70: no case-insensitive
     fallback anywhere in the merge/filter path).
  2. The free-text identifier scan (`_referenced_columns` / `_filter_rules` in
     overlay.py) that decides whether a `rules[].predicate` /
     `ambiguities[].resolves_to` entry references an out-of-scope column.
     This is the highest-value adversarial surface: a false NEGATIVE here
     (failing to recognize a reference) means an out-of-scope column's
     name/predicate/semantics ships to a caller who is not supposed to see
     it — a fail-OPEN leak, not fail-closed.

Two real, reproducible DEFECTS were originally found while writing this file
(cross-table reference in `rules[].predicate`, and ambiguous cross-table
collision in `ambiguities[].resolves_to`). Both are now FIXED in
app/semantic_catalog/overlay.py (FIX 2 / FIX 3 in that module's docstring) and
the tests below assert the fixed (secure) behaviour directly rather than
xfail-documenting the defect.
"""

from __future__ import annotations

import json

from app.semantic_catalog.overlay import build_table_schema_response

# ---------------------------------------------------------------------------
# Fixture catalog for the identifier-scan adversarial suite. Two tables sharing
# a colliding column name ("Amount") on purpose -- this mirrors a REAL
# collision that exists in the production copied-in catalog today (verified
# via `app.semantic_catalog.loader.load_semantic_catalog()`: "EmployeeCode",
# "ClientCode", "ApplicationId", "EmployeeName", and "CreatedBy" all collide
# across >= 2 real table YAMLs, and the real "employee_education_history" /
# "employee_work_history" / "paycheck" ambiguities genuinely reference
# "EmployeeCode" in free-text `resolves_to` prose the same way this fixture's
# "salary" ambiguity references "Amount"). This is not a synthetic-only risk.
# ---------------------------------------------------------------------------

_SCAN_CATALOG = {
    "wh.employee": {
        "database": "wh",
        "table": "employee",
        "description": "d",
        "grain": [],
        "temporal": None,
        "primary_key": [],
        "join_keys": [],
        "columns": {
            "EmployeeCode": {"type": "String"},
            "Salary": {"type": "Decimal(18,2)"},  # OUT of scope in all cases below
            "AnnualSalary": {"type": "Decimal(18,2)"},  # IN scope in all cases below
        },
        "measures": {},
        "rules": [
            {
                "id": "r_substring_neighbor_safe",
                "predicate": "AnnualSalary > 0",
                "applies_when": "x",
                "description": "References only the in-scope column; must survive.",
            },
            {
                "id": "r_leak_direct",
                "predicate": "Salary > 0",
                "applies_when": "x",
                "description": "Plain reference to the out-of-scope column.",
            },
            {
                "id": "r_leak_quoted",
                "predicate": "`Salary` > 0",
                "applies_when": "x",
                "description": "Backtick-quoted reference to the out-of-scope column.",
            },
            {
                "id": "r_leak_func_wrapped",
                "predicate": "SUM(Salary) > 1000",
                "applies_when": "x",
                "description": "Function-wrapped reference to the out-of-scope column.",
            },
            {
                "id": "r_leak_cross_table",
                # "Amount" is NOT one of employee's own columns -- it belongs
                # to wh.payroll only, and wh.payroll.Amount is out of scope.
                "predicate": "Amount > 0",
                "applies_when": "x",
                "description": "Cross-table reference smuggled into employee's own rules[].",
            },
        ],
        "ambiguities": [
            {
                "term": "single_candidate_cross_table",
                # "Amount" resolves unambiguously to wh.payroll (only one other
                # catalogued table has a column literally named "Amount" in
                # THIS fixture) -> the cross-table resolver in
                # _filter_ambiguities (global_index) should catch it.
                "resolves_to": ["payroll gross = SUM(Amount) WHERE RegisterType = 'EARN'"],
                "default": "d",
                "clarify_if": "c",
            },
            {
                "term": "collision_across_two_tables",
                # "ClientCode" exists (in this fixture) on BOTH wh.payroll and
                # wh.accrual -- an out-of-scope reference to ClientCode here
                # cannot be resolved to a single candidate table by
                # _build_global_column_index/_referenced_columns.
                "resolves_to": ["accrual client code = ClientCode"],
                "default": "d",
                "clarify_if": "c",
            },
        ],
    },
    "wh.payroll": {
        "database": "wh",
        "table": "payroll",
        "description": "d",
        "grain": [],
        "temporal": None,
        "primary_key": [],
        "join_keys": [],
        "columns": {
            "Amount": {"type": "Decimal(18,2)"},  # OUT of scope
            "ClientCode": {"type": "String"},  # OUT of scope; collides w/ accrual
            "RegisterType": {"type": "String"},
        },
        "measures": {},
        "rules": [],
        "ambiguities": [],
    },
    "wh.accrual": {
        "database": "wh",
        "table": "accrual",
        "description": "d",
        "grain": [],
        "temporal": None,
        "primary_key": [],
        "join_keys": [],
        "columns": {
            "ClientCode": {"type": "String"},  # OUT of scope; collides w/ payroll
        },
        "measures": {},
        "rules": [],
        "ambiguities": [],
    },
}

_INTROSPECTED_EMPLOYEE = [
    {"name": "EmployeeCode", "type": "String", "comment": ""},
    {"name": "Salary", "type": "Decimal(18,2)", "comment": ""},
    {"name": "AnnualSalary", "type": "Decimal(18,2)", "comment": ""},
]

# Scope excludes: employee.Salary, payroll.Amount, payroll.ClientCode,
# accrual.ClientCode. Includes employee.EmployeeCode + employee.AnnualSalary
# (and payroll.RegisterType, harmlessly, so payroll's own schema calls in
# other tests aren't accidentally starved).
_SCAN_SCOPE = frozenset(
    {
        "wh.employee.EmployeeCode",
        "wh.employee.AnnualSalary",
        "wh.payroll.RegisterType",
    }
)


def _build(scope=_SCAN_SCOPE):
    return build_table_schema_response(
        database="wh",
        table="employee",
        introspected_columns=_INTROSPECTED_EMPLOYEE,
        catalog=_SCAN_CATALOG,
        scope=scope,
    )


def _rule_ids(result):
    return {r["id"] for r in result["rules"]}


def _dump_no_leak(result, forbidden_substring):
    """Assert *forbidden_substring* does not appear anywhere in the JSON-serialized response."""
    blob = json.dumps(result)
    assert forbidden_substring not in blob, (
        f"{forbidden_substring!r} leaked into the getTableSchema response despite "
        "being out of scope."
    )


# ===========================================================================
# 1. Case sensitivity (D70) -- a scope entry with the wrong case must NOT
#    match; the column stays dropped.
# ===========================================================================


class TestColumnDropCaseSensitivity:
    def test_wrong_case_scope_entry_does_not_resurrect_dropped_column(self):
        """Scope names the column as 'employeecode' (all lowercase) instead of
        the catalog's authored 'EmployeeCode'. Per D70, no case-insensitive
        fallback exists anywhere in this module -- the column must stay
        dropped, exactly as if the scope hadn't listed it at all."""
        wrong_case_scope = frozenset(
            {"wh.employee.employeecode", "wh.employee.AnnualSalary"}
        )
        result = _build(scope=wrong_case_scope)
        names = {c["name"] for c in result["columns"]}
        assert "EmployeeCode" not in names
        assert names == {"AnnualSalary"}

    def test_correct_case_scope_entry_keeps_column(self):
        """Sanity check / control for the above: the *correctly*-cased entry
        does resurrect the column, proving the drop above is a case-mismatch
        effect and not some unrelated bug."""
        correct_case_scope = frozenset(
            {"wh.employee.EmployeeCode", "wh.employee.AnnualSalary"}
        )
        result = _build(scope=correct_case_scope)
        names = {c["name"] for c in result["columns"]}
        assert "EmployeeCode" in names


# ===========================================================================
# 2. rules[] identifier-scan adversarial coverage
# ===========================================================================


class TestRulesPredicateScanAdversarial:
    def test_own_table_column_reference_is_never_confused_with_a_similarly_named_one(self):
        """(b) substring-collision control, false-negative direction: a rule
        that references only the IN-scope 'AnnualSalary' must survive even
        though 'Salary' (a substring / prefix-overlapping different column) is
        out of scope. The identifier regex tokenizes whole identifiers, not
        substrings, so this must NOT be dropped."""
        result = _build()
        assert "r_substring_neighbor_safe" in _rule_ids(result)

    def test_plain_reference_to_out_of_scope_column_is_dropped(self):
        """(a) baseline: plain identifier reference to an out-of-scope column."""
        result = _build()
        assert "r_leak_direct" not in _rule_ids(result)
        _dump_no_leak(result, "r_leak_direct")

    def test_backtick_quoted_reference_to_out_of_scope_column_is_dropped(self):
        """(c) a backtick-quoted identifier must still be recognized by the
        scan -- backticks are not identifier characters, so the regex should
        still extract 'Salary' as a bare token."""
        result = _build()
        assert "r_leak_quoted" not in _rule_ids(result)

    def test_function_wrapped_reference_to_out_of_scope_column_is_dropped(self):
        """(e) SUM(Salary) -- the column name is still a bare token inside the
        function call and must be recognized."""
        result = _build()
        assert "r_leak_func_wrapped" not in _rule_ids(result)

    def test_cross_table_reference_in_rules_predicate_is_dropped(self):
        """(d) cross-table column reference smuggled into a rule predicate.

        FIX 2: _filter_rules now parses the predicate with sqlglot and
        resolves identifiers against BOTH the rule's own table AND every
        other catalogued/introspected table (global_index), matching
        ambiguities[]'s cross-table resolution. 'Amount' is not one of
        employee's own columns but IS wh.payroll.Amount, out of scope -- the
        rule must be dropped.
        """
        result = _build()
        rule_ids = _rule_ids(result)
        assert "r_leak_cross_table" not in rule_ids, (
            "LEAK: r_leak_cross_table's predicate 'Amount > 0' references "
            "wh.payroll.Amount (out of scope) but was kept in the response."
        )
        _dump_no_leak(result, "r_leak_cross_table")


# ===========================================================================
# 3. ambiguities[].resolves_to identifier-scan adversarial coverage
# ===========================================================================


class TestAmbiguitiesResolvesToScanAdversarial:
    def test_single_candidate_cross_table_reference_is_correctly_dropped(self):
        """(a)/(d) control: when a resolves_to token names a column that
        exists in exactly ONE other catalogued table (unambiguous cross-table
        resolution), the scan correctly identifies it and drops the entry.
        This confirms the general cross-table mechanism works when there's no
        name collision -- isolates the *next* test's failure to the collision
        case specifically, not to cross-table resolution being broken
        outright."""
        result = _build()
        terms = {a["term"] for a in result["ambiguities"]}
        assert "single_candidate_cross_table" not in terms

    def test_ambiguous_cross_table_collision_is_dropped_not_silently_kept(self):
        """(d) THE key predicate-scan gap, now fixed: a resolves_to entry
        references a column name that collides across >= 2 catalogued
        tables, all out-of-scope copies of that name. FIX 3:
        _resolve_token/_referenced_columns now resolve a colliding token to
        ALL candidate tables at once (fail-closed on the collision) instead
        of silently treating it as no reference -- per design Section 1.4
        this must be dropped."""
        result = _build()
        by_term = {a["term"]: a for a in result["ambiguities"]}
        assert "collision_across_two_tables" not in by_term, (
            "LEAK: 'collision_across_two_tables' resolves_to "
            f"{by_term.get('collision_across_two_tables', {}).get('resolves_to')!r} "
            "references ClientCode (out of scope on both candidate tables) "
            "but was kept."
        )
        _dump_no_leak(result, "collision_across_two_tables")


# ===========================================================================
# 4. Uncatalogued table: scope-filter still applies even with zero catalog
#    entries (design Section 1.3's last line: "not exempt from column scope").
# ===========================================================================


class TestUncataloguedTableAdversarial:
    def test_all_columns_excluded_yields_empty_columns_list_not_a_crash(self):
        introspected = [
            {"name": "secret_a", "type": "String", "comment": ""},
            {"name": "secret_b", "type": "String", "comment": ""},
        ]
        scope = frozenset({"wh.other_table.unrelated_col"})
        result = build_table_schema_response(
            database="wh",
            table="mystery",
            introspected_columns=introspected,
            catalog=_SCAN_CATALOG,
            scope=scope,
        )
        assert result["catalogued"] is False
        assert result["columns"] == []


# ===========================================================================
# 5. Real-catalog collision inventory -- a lightweight canary, not an
#    assertion about overlay.py behavior. If this ever starts failing, the
#    xfail(strict) tests above may have been written against a fixture that
#    no longer mirrors production risk; re-verify against the real data before
#    touching src/.
# ===========================================================================


class TestRealCatalogHasNameCollisions:
    def test_production_catalog_has_cross_table_column_name_collisions(self):
        """Confirms the xfail tests above are not purely synthetic: the real
        copied-in catalog under app/semantic_catalog/data/ has column names
        (e.g. EmployeeCode, ClientCode) authored identically across multiple
        tables, which is exactly the precondition that defeats the
        single-candidate cross-table resolver in overlay.py."""
        from app.semantic_catalog.loader import load_semantic_catalog

        catalog = load_semantic_catalog()
        index: dict[str, set[str]] = {}
        for key, entry in catalog.items():
            for col_name in (entry.get("columns") or {}):
                index.setdefault(col_name, set()).add(key)
        collisions = {name: tables for name, tables in index.items() if len(tables) > 1}
        assert "EmployeeCode" in collisions
        assert len(collisions["EmployeeCode"]) >= 2


# ===========================================================================
# 6. FIX 1 -- undocumented-but-real (introspected-only) column referenced in
#    free text (rules[].predicate / ambiguities[].resolves_to). Before the
#    fix, own_column_names was built from the catalog's `columns:` block
#    only -- a column absent from that block but present in the live
#    database (e.g. SSN, design §1.2's normal "undocumented column" case)
#    was invisible to the scan and could leak via a rule/ambiguity
#    predicate/resolution string even when out of scope.
# ===========================================================================

_UNDOCUMENTED_CATALOG = {
    "wh.employee": {
        "database": "wh",
        "table": "employee",
        "description": "d",
        "grain": [],
        "temporal": None,
        "primary_key": [],
        "join_keys": [],
        "columns": {
            # "SSN" is intentionally NOT documented here -- it is a real
            # column (present in introspection) but absent from the
            # catalog's columns: block.
            "EmployeeCode": {"type": "String"},
        },
        "measures": {},
        "rules": [
            {
                "id": "r_ssn_leak",
                "predicate": "SSN IS NOT NULL",
                "applies_when": "x",
                "description": "References the undocumented-but-real SSN column.",
            },
        ],
        "ambiguities": [
            {
                "term": "identity",
                "resolves_to": ["taxpayer identity = SSN"],
                "default": "d",
                "clarify_if": "c",
            },
        ],
    },
}

_UNDOCUMENTED_INTROSPECTED = [
    {"name": "EmployeeCode", "type": "String", "comment": ""},
    {"name": "SSN", "type": "String", "comment": ""},
]


def _build_undocumented(scope):
    return build_table_schema_response(
        database="wh",
        table="employee",
        introspected_columns=_UNDOCUMENTED_INTROSPECTED,
        catalog=_UNDOCUMENTED_CATALOG,
        scope=scope,
    )


class TestUndocumentedIntrospectedColumnScan:
    def test_rule_referencing_undocumented_column_dropped_when_out_of_scope(self):
        """SSN is present in introspection but NOT in the catalog's columns:
        block. A rule predicating on it must still be recognized as an SSN
        reference and dropped when SSN is out of scope (FIX 1)."""
        scope = frozenset({"wh.employee.EmployeeCode"})  # SSN excluded
        result = _build_undocumented(scope)
        rule_ids = {r["id"] for r in result["rules"]}
        assert "r_ssn_leak" not in rule_ids
        _dump_no_leak(result, "SSN")

    def test_rule_referencing_undocumented_column_kept_when_in_scope(self):
        scope = frozenset({"wh.employee.EmployeeCode", "wh.employee.SSN"})
        result = _build_undocumented(scope)
        rule_ids = {r["id"] for r in result["rules"]}
        assert "r_ssn_leak" in rule_ids

    def test_ambiguity_referencing_undocumented_column_dropped_when_out_of_scope(self):
        scope = frozenset({"wh.employee.EmployeeCode"})
        result = _build_undocumented(scope)
        terms = {a["term"] for a in result["ambiguities"]}
        assert "identity" not in terms

    def test_ambiguity_referencing_undocumented_column_kept_when_in_scope(self):
        scope = frozenset({"wh.employee.EmployeeCode", "wh.employee.SSN"})
        result = _build_undocumented(scope)
        terms = {a["term"] for a in result["ambiguities"]}
        assert "identity" in terms

    def test_cross_table_undocumented_column_resolved_via_introspected_schema_param(self):
        """When `introspected_schema` (the real system.columns universe, as
        supplied by app.service.get_table_schema via app.catalog.get_catalog_
        schema) is provided, an undocumented-but-real column on ANOTHER
        table is also recognized via the global index, not just the
        catalog's columns: block."""
        catalog = {
            "wh.employee": {
                "database": "wh",
                "table": "employee",
                "description": "d",
                "grain": [],
                "temporal": None,
                "primary_key": [],
                "join_keys": [],
                "columns": {"EmployeeCode": {"type": "String"}},
                "measures": {},
                "rules": [],
                "ambiguities": [
                    {
                        "term": "tax_id",
                        "resolves_to": ["payroll tax id = TaxID"],
                        "default": "d",
                        "clarify_if": "c",
                    },
                ],
            },
            "wh.payroll": {
                "database": "wh",
                "table": "payroll",
                "description": "d",
                "grain": [],
                "temporal": None,
                "primary_key": [],
                "join_keys": [],
                # "TaxID" is undocumented here on purpose.
                "columns": {"Amount": {"type": "Decimal(18,2)"}},
                "measures": {},
                "rules": [],
                "ambiguities": [],
            },
        }
        introspected_schema = {
            "wh.employee": {"EmployeeCode": "String"},
            "wh.payroll": {"Amount": "Decimal(18,2)", "TaxID": "String"},
        }
        scope = frozenset({"wh.employee.EmployeeCode", "wh.payroll.Amount"})  # TaxID excluded
        result = build_table_schema_response(
            database="wh",
            table="employee",
            introspected_columns=[{"name": "EmployeeCode", "type": "String", "comment": ""}],
            catalog=catalog,
            scope=scope,
            introspected_schema=introspected_schema,
        )
        terms = {a["term"] for a in result["ambiguities"]}
        assert "tax_id" not in terms


# ===========================================================================
# 7. FIX 2 -- rules[].predicate sqlglot fail-closed behavior: unparseable
#    predicates and unresolvable identifiers are dropped; legitimate
#    in-scope references (including cross-table) are NOT over-dropped.
# ===========================================================================


class TestRulesPredicateFailClosedOnUnparseable:
    def test_unparseable_predicate_is_dropped(self):
        """A predicate that isn't valid standalone SQL cannot be verified --
        fail closed (FIX 2): drop the rule, never keep on uncertainty."""
        catalog = {
            "wh.t": {
                "database": "wh",
                "table": "t",
                "description": "d",
                "grain": [],
                "temporal": None,
                "primary_key": [],
                "join_keys": [],
                "columns": {"A": {"type": "String"}},
                "measures": {},
                "rules": [
                    {
                        "id": "r_bad",
                        "predicate": "this is not && valid SQL ###",
                        "applies_when": "x",
                        "description": "d",
                    },
                    {
                        "id": "r_good",
                        "predicate": "A = 'x'",
                        "applies_when": "x",
                        "description": "d",
                    },
                ],
                "ambiguities": [],
            },
        }
        introspected = [{"name": "A", "type": "String", "comment": ""}]
        scope = frozenset({"wh.t.A"})
        result = build_table_schema_response(
            database="wh",
            table="t",
            introspected_columns=introspected,
            catalog=catalog,
            scope=scope,
        )
        rule_ids = {r["id"] for r in result["rules"]}
        assert "r_bad" not in rule_ids
        assert "r_good" in rule_ids

    def test_predicate_referencing_unknown_identifier_is_dropped(self):
        """A syntactically valid predicate whose column identifier matches NO
        known column anywhere in the introspected universe is unresolvable
        -- fail closed (FIX 2), never silently ignored."""
        catalog = {
            "wh.t": {
                "database": "wh",
                "table": "t",
                "description": "d",
                "grain": [],
                "temporal": None,
                "primary_key": [],
                "join_keys": [],
                "columns": {"A": {"type": "String"}},
                "measures": {},
                "rules": [
                    {
                        "id": "r_unknown",
                        "predicate": "TotallyMadeUpColumn = 1",
                        "applies_when": "x",
                        "description": "d",
                    },
                ],
                "ambiguities": [],
            },
        }
        introspected = [{"name": "A", "type": "String", "comment": ""}]
        scope = frozenset({"wh.t.A"})
        result = build_table_schema_response(
            database="wh",
            table="t",
            introspected_columns=introspected,
            catalog=catalog,
            scope=scope,
        )
        assert "r_unknown" not in {r["id"] for r in result["rules"]}


class TestRulesLegitimateInScopeReferencesNotOverDropped:
    def test_cross_table_in_scope_reference_is_kept(self):
        """A rule predicating on another table's column that IS in scope
        must survive -- FIX 2 must not over-drop legitimate cross-table
        references."""
        catalog = {
            "wh.employee": {
                "database": "wh",
                "table": "employee",
                "description": "d",
                "grain": [],
                "temporal": None,
                "primary_key": [],
                "join_keys": [],
                "columns": {"EmployeeCode": {"type": "String"}},
                "measures": {},
                "rules": [
                    {
                        "id": "r_cross_in_scope",
                        "predicate": "Amount > 0",
                        "applies_when": "x",
                        "description": "d",
                    },
                ],
                "ambiguities": [],
            },
            "wh.payroll": {
                "database": "wh",
                "table": "payroll",
                "description": "d",
                "grain": [],
                "temporal": None,
                "primary_key": [],
                "join_keys": [],
                "columns": {"Amount": {"type": "Decimal(18,2)"}},
                "measures": {},
                "rules": [],
                "ambiguities": [],
            },
        }
        introspected = [{"name": "EmployeeCode", "type": "String", "comment": ""}]
        scope = frozenset({"wh.employee.EmployeeCode", "wh.payroll.Amount"})
        result = build_table_schema_response(
            database="wh",
            table="employee",
            introspected_columns=introspected,
            catalog=catalog,
            scope=scope,
        )
        rule_ids = {r["id"] for r in result["rules"]}
        assert "r_cross_in_scope" in rule_ids
