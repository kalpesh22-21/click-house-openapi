"""Unit tests for the M3 `columns` narrowing filter in
app/semantic_catalog/overlay.py:build_table_schema_response.

Purpose of the feature: the runtime's two-tier schema preview NAMES every column
but budgets the detail, so a model that needs an unshown column's documentation
must be able to fetch it narrowly.

Security posture under test — the filter is applied LAST, after the
`mcp_projection.hidden_columns` projection and after scope enforcement, so it can
only ever REMOVE entries from the already-authorized column list. The adversarial
classes below assert that directly: no `columns` argument can surface a column
that the same call without `columns` would have hidden.

Existence-disclosure posture: `columns_not_found` is a single UNDIFFERENTIATED
bucket. A nonexistent name, a projection-hidden name, and an out-of-scope name
are indistinguishable in it. That matches the strictest posture this repo already
takes — `mcp_projection.hidden_columns` NAMES are stripped from every model-facing
surface unconditionally (A-H1/A-H2), so a "hidden" vs "unknown" distinction would
confirm an RLS column's existence that no other surface confirms.
"""

from __future__ import annotations

from app.semantic_catalog.overlay import build_table_schema_response

from tests.semantic_catalog.test_overlay import (
    _CATALOG,
    _FULL_SCOPE,
    _HIDDEN_CATALOG,
    _INTROSPECTED_EMPLOYEE,
    _INTROSPECTED_HIDDEN,
    _RESTRICTED_SCOPE,
    _col_names,
)

_BASE_SECTIONS = (
    "description",
    "grain",
    "temporal",
    "primary_key",
    "join_keys",
    "measures",
    "rules",
    "ambiguities",
)


def _employee(scope=None, columns_filter=None, catalog=None, introspected=None):
    return build_table_schema_response(
        database="wh",
        table="employee",
        introspected_columns=introspected if introspected is not None else _INTROSPECTED_EMPLOYEE,
        catalog=catalog if catalog is not None else _CATALOG,
        scope=scope,
        columns_filter=columns_filter,
    )


def _labor(scope=None, columns_filter=None):
    return build_table_schema_response(
        database="wh",
        table="labor_allocation",
        introspected_columns=_INTROSPECTED_HIDDEN,
        catalog=_HIDDEN_CATALOG,
        scope=scope,
        columns_filter=columns_filter,
    )


# ===========================================================================
# Happy path — the narrowed fetch
# ===========================================================================

class TestNarrowedFetch:
    def test_returns_exactly_the_named_columns(self):
        result = _employee(columns_filter=["AnnualSalary"])
        assert _col_names(result) == {"AnnualSalary"}

    def test_named_column_carries_its_full_documentation(self):
        """The whole point: the narrowed entry is the FULL merged entry, not a stub."""
        result = _employee(columns_filter=["AnnualSalary"])
        (col,) = result["columns"]
        assert col["description"] == "Annual salary amount."
        assert col["unit"] == "USD"
        assert col["sensitive"] is True
        assert col["type"] == "Nullable(Decimal(18, 6))"

    def test_several_columns_at_once(self):
        result = _employee(columns_filter=["EmployeeCode", "EmployeeStatus"])
        assert _col_names(result) == {"EmployeeCode", "EmployeeStatus"}

    def test_narrowed_entries_are_identical_to_the_unfiltered_ones(self):
        """Narrowing must not alter a surviving entry — only drop its neighbours."""
        full = _employee()
        narrowed = _employee(columns_filter=["EmployeeStatus"])
        expected = [c for c in full["columns"] if c["name"] == "EmployeeStatus"]
        assert narrowed["columns"] == expected

    def test_preserves_introspection_order_not_argument_order(self):
        """The narrowed array reads like a slice of the full response."""
        result = _employee(columns_filter=["EmployeeStatus", "EmployeeCode"])
        assert [c["name"] for c in result["columns"]] == ["EmployeeCode", "EmployeeStatus"]

    def test_duplicate_names_yield_one_entry(self):
        result = _employee(columns_filter=["EmployeeCode", "EmployeeCode"])
        assert [c["name"] for c in result["columns"]] == ["EmployeeCode"]

    def test_naming_every_column_equals_the_unfiltered_column_list(self):
        full = _employee()
        named = _employee(columns_filter=[c["name"] for c in full["columns"]])
        assert named["columns"] == full["columns"]
        assert named["columns_not_found"] == []


# ===========================================================================
# Base sections always ride complete
# ===========================================================================

class TestBaseSectionsAlwaysComplete:
    def test_every_base_section_matches_the_unfiltered_response(self):
        full = _employee()
        narrowed = _employee(columns_filter=["EmployeeCode"])
        for section in _BASE_SECTIONS:
            assert narrowed[section] == full[section], section

    def test_base_sections_complete_even_when_they_name_filtered_out_columns(self):
        """Narrowing to EmployeeCode must NOT prune the AnnualSalary measure/rule —
        the table-level semantics are what make the fetched column usable."""
        narrowed = _employee(columns_filter=["EmployeeCode"])
        assert [m["name"] for m in narrowed["measures"]] == ["avg_salary"]
        assert {r["id"] for r in narrowed["rules"]} == {"active_employee", "salary_gate"}
        assert narrowed["grain"] == ["EmployeeCode"]
        assert narrowed["description"] == "Employee record, one row per employee."

    def test_base_sections_complete_under_scope_plus_filter(self):
        """The filter narrows columns only; scope still shapes the base sections
        exactly as it does without a filter."""
        scoped = _employee(scope=_RESTRICTED_SCOPE)
        both = _employee(scope=_RESTRICTED_SCOPE, columns_filter=["EmployeeCode"])
        for section in _BASE_SECTIONS:
            assert both[section] == scoped[section], section


# ===========================================================================
# None / [] equivalence
# ===========================================================================

class TestEmptyListMeansAllColumns:
    def test_empty_list_and_none_produce_identical_responses(self):
        """An empty list is what a confused model sends; reading it as "no columns"
        would return a useless husk that looks like a table with no columns."""
        assert _employee(columns_filter=[]) == _employee(columns_filter=None)

    def test_empty_list_returns_every_column(self):
        assert _col_names(_employee(columns_filter=[])) == {
            "EmployeeCode",
            "AnnualSalary",
            "EmployeeStatus",
        }

    def test_no_columns_not_found_key_when_unfiltered(self):
        """Response shape is byte-identical to pre-M3 for every existing caller."""
        assert "columns_not_found" not in _employee(columns_filter=None)
        assert "columns_not_found" not in _employee(columns_filter=[])

    def test_equivalence_holds_under_scope_enforcement(self):
        assert _employee(scope=_RESTRICTED_SCOPE, columns_filter=[]) == _employee(
            scope=_RESTRICTED_SCOPE, columns_filter=None
        )


# ===========================================================================
# columns_not_found — the echo
# ===========================================================================

class TestColumnsNotFoundEcho:
    def test_unknown_name_is_echoed_not_silently_swallowed(self):
        result = _employee(columns_filter=["NoSuchColumn"])
        assert result["columns"] == []
        assert result["columns_not_found"] == ["NoSuchColumn"]

    def test_known_and_unknown_names_split_correctly(self):
        result = _employee(columns_filter=["EmployeeCode", "Nope"])
        assert _col_names(result) == {"EmployeeCode"}
        assert result["columns_not_found"] == ["Nope"]

    def test_present_and_empty_when_every_name_resolves(self):
        result = _employee(columns_filter=["EmployeeCode"])
        assert result["columns_not_found"] == []

    def test_echo_preserves_the_callers_own_spelling_verbatim(self):
        """Echoing back only what the caller sent tells them nothing new — the key
        property that makes the echo safe."""
        result = _employee(columns_filter=["employeecode"])
        assert result["columns_not_found"] == ["employeecode"]

    def test_echo_deduplicates(self):
        result = _employee(columns_filter=["Nope", "Nope"])
        assert result["columns_not_found"] == ["Nope"]

    def test_echo_carries_names_only_no_other_information(self):
        result = _employee(columns_filter=["Nope"])
        assert all(isinstance(n, str) for n in result["columns_not_found"])


# ===========================================================================
# Case sensitivity (D70 — matches the catalog merge join)
# ===========================================================================

class TestCaseSensitivity:
    def test_wrong_case_does_not_match(self):
        result = _employee(columns_filter=["annualsalary", "ANNUALSALARY"])
        assert result["columns"] == []
        assert result["columns_not_found"] == ["annualsalary", "ANNUALSALARY"]

    def test_exact_case_matches(self):
        assert _col_names(_employee(columns_filter=["AnnualSalary"])) == {"AnnualSalary"}

    def test_surrounding_whitespace_does_not_match(self):
        """Exact match, no normalization — a padded name reads as a miss and the
        model retries with the spelling the skeleton gave it."""
        result = _employee(columns_filter=[" AnnualSalary"])
        assert result["columns"] == []
        assert result["columns_not_found"] == [" AnnualSalary"]


# ===========================================================================
# ADVERSARIAL — the filter can never reach past scope enforcement
# ===========================================================================

class TestAdversarialScope:
    def test_out_of_scope_column_requested_by_name_stays_absent(self):
        """wh.employee.AnnualSalary is outside _RESTRICTED_SCOPE. Naming it must
        not resurrect it."""
        result = _employee(scope=_RESTRICTED_SCOPE, columns_filter=["AnnualSalary"])
        assert result["columns"] == []

    def test_out_of_scope_column_is_reported_exactly_like_a_nonexistent_one(self):
        """POSTURE: `columns_not_found` must not distinguish "exists but you can't
        see it" from "doesn't exist" — the two responses are identical apart from
        the caller's own echoed string."""
        hidden_by_scope = _employee(scope=_RESTRICTED_SCOPE, columns_filter=["AnnualSalary"])
        never_existed = _employee(scope=_RESTRICTED_SCOPE, columns_filter=["NoSuchColumn"])
        assert hidden_by_scope["columns_not_found"] == ["AnnualSalary"]
        assert never_existed["columns_not_found"] == ["NoSuchColumn"]
        assert hidden_by_scope["columns"] == never_existed["columns"] == []
        assert {k: v for k, v in hidden_by_scope.items() if k != "columns_not_found"} == {
            k: v for k, v in never_existed.items() if k != "columns_not_found"
        }

    def test_mixing_in_scope_and_out_of_scope_names_yields_only_the_in_scope_one(self):
        result = _employee(
            scope=_RESTRICTED_SCOPE, columns_filter=["EmployeeCode", "AnnualSalary"]
        )
        assert _col_names(result) == {"EmployeeCode"}
        assert result["columns_not_found"] == ["AnnualSalary"]

    def test_filter_never_widens_the_authorized_column_set(self):
        """Property check: for EVERY column the catalog knows about, a filter naming
        it yields a subset of the unfiltered response's columns — under every scope
        setting. There is no argument that widens the result."""
        every_name = [c["name"] for c in _INTROSPECTED_EMPLOYEE] + ["NoSuchColumn"]
        for scope in (None, frozenset(), _FULL_SCOPE, _RESTRICTED_SCOPE):
            allowed = {c["name"] for c in _employee(scope=scope)["columns"]}
            got = {c["name"] for c in _employee(scope=scope, columns_filter=every_name)["columns"]}
            assert got == allowed, scope


# ===========================================================================
# ADVERSARIAL — the filter can never reach past the tenancy/hidden projection
# ===========================================================================

class TestAdversarialHiddenProjection:
    def test_rls_columns_requested_by_name_stay_absent_with_scope_none(self):
        """stdio/local-trust (no scope enforcement at all) — the projection is the
        only thing standing between the caller and client_code/proc_center, and the
        filter must not step around it."""
        result = _labor(columns_filter=["client_code", "proc_center", "version"])
        assert result["columns"] == []

    def test_rls_columns_requested_by_name_stay_absent_under_scope(self):
        scope = frozenset(
            {
                "wh.labor_allocation.code",
                "wh.labor_allocation.join_key",
                # An adversarially over-broad scope that DOES name the RLS columns:
                # the projection is unconditional, so they still must not appear.
                "wh.labor_allocation.client_code",
                "wh.labor_allocation.proc_center",
                "wh.labor_allocation.version",
            }
        )
        result = _labor(scope=scope, columns_filter=["client_code", "version"])
        assert result["columns"] == []

    def test_hidden_column_is_reported_exactly_like_a_nonexistent_one(self):
        hidden = _labor(columns_filter=["client_code"])
        unknown = _labor(columns_filter=["no_such_column"])
        assert hidden["columns"] == unknown["columns"] == []
        assert {k: v for k, v in hidden.items() if k != "columns_not_found"} == {
            k: v for k, v in unknown.items() if k != "columns_not_found"
        }

    def test_visible_neighbours_still_fetchable_alongside_a_hidden_name(self):
        result = _labor(columns_filter=["code", "client_code"])
        assert _col_names(result) == {"code"}
        assert result["columns_not_found"] == ["client_code"]

    def test_narrowing_does_not_leak_hidden_names_into_base_sections(self):
        """The A-H2 sweep of grain/primary_key/join_keys must be unaffected by the
        filter — a narrowed response is no leakier than a full one."""
        result = _labor(columns_filter=["code"])
        # grain/primary_key keep every NON-hidden member, including join_key,
        # which the filter deliberately does not touch (base sections ride complete).
        assert result["grain"] == ["code", "join_key"]
        assert result["primary_key"] == ["code"]
        assert result["grain"] == _labor()["grain"]
        serialized = repr(result)
        for hidden_name in ("client_code", "proc_center", "version"):
            assert hidden_name not in serialized
