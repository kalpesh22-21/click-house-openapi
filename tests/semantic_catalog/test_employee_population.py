"""Different populations must stay distinct even when a blueprint defaults to current staff."""
import sqlite3
from pathlib import Path

import yaml
from sqlglot import exp

from app.semantic_catalog.loader import load_semantic_catalog
from tools.format_blueprints import parse


def test_active_and_current_populations_with_leave_and_terminated_employees():
    rules = {r["id"]: r for r in load_semantic_catalog()["dbpcm_warehouse.employee"]["rules"]}
    blueprint = yaml.safe_load((
        Path(__file__).parents[2]
        / "app/corpus/data/blueprints/bp-active-headcount-by-department.yaml"
    ).read_text())
    status_slot = next(s for s in blueprint["slots"] if s["name"] == "employee_status")
    assert "Active" in status_slot["enum_values"]
    assert status_slot["multiple"] is True
    assert status_slot["default"] == ["Active", "On Leave"]
    assert "optional_pattern" not in status_slot
    assert "active employees" in rules["active_employee"]["applies_when"]
    assert "current employees" in rules["default_employee_population"]["applies_when"]

    # Exercise the actual blueprint status predicate, not a copied implementation.
    sql = blueprint["sql_template"]
    predicate = next(node for node in parse(sql).find_all(exp.Or)
                     if any(p.name == "employee_status" for p in node.find_all(exp.Placeholder)))
    for placeholder in predicate.find_all(exp.Placeholder):
        if placeholder.name == "employee_status":
            placeholder.set("this", "status")
    explicit_predicate = predicate.sql(dialect="sqlite")
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE employee(employee_status TEXT)")
        db.executemany("INSERT INTO employee VALUES (?)", [
            ("Active",), ("Active",), ("On Leave",), ("Terminated",),
        ])
        def count(predicate, params=()):
            return db.execute("SELECT count(*) FROM employee WHERE " + predicate, params).fetchone()[0]

        assert count(rules["active_employee"]["predicate"]) == 2
        assert count(explicit_predicate, {"status": "Active"}) == 2
        assert count(rules["default_employee_population"]["predicate"]) == 3
        assert count(explicit_predicate, {"status": "All"}) == 4
