"""Employee salary-basis guidance pins (owner policy: always assume annual).

Grounded in the 2026-10-09 QA2 view-side probe (tenant-wide 45,589 rows):
``annual_salary > 0`` holds exactly on ``employee_pay_type = 'Salary'``
(7,337 rows); the Hourly (38,229) and Commission Only (23) classes carry
``annual_salary = 0``, never NULL — the deployed view does not annualize
hourly pay. Within the Salary class, annual_salary / hourly_salary lands on
52-multiples of the scheduled weekly hours (2080 dominant), and rate_1 is
positive on every Hourly row and 0 on every Salary row.

These tests pin the SEMANTIC anchors of that guidance (basis default,
absent-marker behavior, discriminator column, per-period conversion fields),
so a later edit cannot silently flip the annual default or drop the
zero-class facts. Tenant counts are deliberately NOT pinned — they are
re-measurable observations, and the schema_notes entry records them as such.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - direct script run
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.semantic_catalog import loader

_TABLE = "dbpcm_warehouse.employee"


def _entry() -> dict:
    return loader.load_semantic_catalog()[_TABLE]


class TestSalaryBasisAmbiguity:
    def test_ambiguity_exists_and_resolves_to_real_salary_columns(self):
        ambiguities = _entry()["ambiguities"]
        salary_basis = next(
            (a for a in ambiguities if a["term"] == "salary_basis"), None
        )
        assert salary_basis is not None, (
            "employee.yaml must carry a salary_basis ambiguity (owner policy: "
            "always assume annual salary for unqualified questions)"
        )
        assert set(salary_basis["resolves_to"]) == {
            "annual_salary",
            "hourly_salary",
            "rate_1",
        }

    def test_default_is_annual(self):
        salary_basis = next(
            a for a in _entry()["ambiguities"] if a["term"] == "salary_basis"
        )
        default = salary_basis["default"]
        # The annual default is owner policy; the words may move but the
        # resolution column and its zero-not-NULL basis semantics may not
        # silently change.
        assert "annual_salary" in default
        assert "never NULL" in default
        assert "'Salary'" in default

    def test_clarify_routes_hourly_and_per_period_asks(self):
        salary_basis = next(
            a for a in _entry()["ambiguities"] if a["term"] == "salary_basis"
        )
        clarify_if = salary_basis["clarify_if"]
        # Explicit hourly-rate asks split between the salaried equivalent rate
        # and the actual rate paid to the Hourly class.
        assert "hourly_salary" in clarify_if
        assert "rate_1" in clarify_if
        # Weekly/per-period asks name only catalogued cadence fields.
        assert "pay_frequency" in clarify_if
        assert "scheduled_pay_period_hours" in clarify_if


class TestSalaryColumnDescriptions:
    def test_annual_salary_description_keeps_basis_semantics(self):
        description = _entry()["columns"]["annual_salary"]["description"]
        # Default-position anchor: annual basis for unqualified questions.
        assert "default" in description
        # Dual-basis facts: recorded/derived annual amount for the Salary
        # class; zero — not NULL, not an empty string — on the hourly and
        # commission classes, because the view does not annualize hourly pay.
        assert "recorded annual amount" in description
        assert "never NULL" in description
        assert "not annualized" in description
        assert "employee_pay_type = 'Salary'" in description
        # The true-median recipe is retained (catalog-wide convention).
        assert "quantileExactLow(0.5)" in description

    def test_hourly_salary_description_names_rate_1_and_the_absent_marker(self):
        description = _entry()["columns"]["hourly_salary"]["description"]
        assert "rate_1" in description
        assert "absent marker" in description

    def test_employee_pay_type_is_named_as_the_discriminator(self):
        description = _entry()["columns"]["employee_pay_type"]["description"]
        assert "discriminator" in description
        assert "annual_salary > 0" in description


class TestAnnualCompensationRule:
    def test_rule_pins_default_aggregation_population(self):
        rules = _entry()["rules"]
        rule = next((r for r in rules if r["id"] == "annual_compensation"), None)
        assert rule is not None
        assert rule["predicate"] == "employee_pay_type = 'Salary'"
        assert "annual_salary > 0 population" in rule["description"]
