"""Different populations must stay distinct even when a blueprint defaults to current staff."""
import sqlite3
from pathlib import Path

import yaml

from app.semantic_catalog.loader import load_semantic_catalog


def test_active_and_current_populations_with_leave_and_terminated_employees():
    rules = {r["id"]: r for r in load_semantic_catalog()["dbpcm_warehouse.employee"]["rules"]}
    blueprint = yaml.safe_load((
        Path(__file__).parents[2]
        / "app/corpus/data/blueprints/bp-active-headcount-by-department.yaml"
    ).read_text())
    status_slot = next(s for s in blueprint["slots"] if s["name"] == "employee_status")
    assert "Active" in status_slot["enum_values"]
    assert status_slot["optional_pattern"] == rules["default_employee_population"]["predicate"]
    assert "active employees" in rules["active_employee"]["applies_when"]
    assert "current employees" in rules["default_employee_population"]["applies_when"]

    # Exercise the actual blueprint status predicate, not a copied implementation.
    sql = blueprint["sql_template"]
    explicit_predicate = sql.split("WHERE (", 1)[1].split(")\n", 1)[0]
    explicit_predicate = explicit_predicate.replace("{employee_status}", ":status")
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE employee(employee_status TEXT)")
        db.executemany("INSERT INTO employee VALUES (?)", [
            ("Active",), ("Active",), ("On Leave",), ("Terminated",),
        ])
        def count(predicate, params=()):
            return db.execute("SELECT count(*) FROM employee WHERE " + predicate, params).fetchone()[0]

        assert count(rules["active_employee"]["predicate"]) == 2
        assert count(explicit_predicate, {"status": "Active"}) == 2
        assert count(status_slot["optional_pattern"]) == 3
        assert count(explicit_predicate, {"status": "All"}) == 4
