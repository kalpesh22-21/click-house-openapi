"""Payroll's advertised default must agree with the executable blueprint at year boundaries."""
import re
import sqlite3
from pathlib import Path

import yaml

from app.semantic_catalog.loader import load_semantic_catalog


def payroll():
    return load_semantic_catalog()["dbpcm_warehouse.payroll"]


def test_default_timing_matches_payment_date_blueprint():
    entry = payroll()
    temporal = entry["temporal"]
    default_dimension = next(d for d in temporal["dimensions"] if d["name"] == temporal["default_pin"])
    timing = next(a for a in entry["ambiguities"] if a["term"] == "pay_timing")
    assert default_dimension["date_col"] == timing["default"] == "pay_date"

    blueprint_path = Path(__file__).parents[2] / "app/corpus/data/blueprints/bp-payroll-summary.yaml"
    sql = yaml.safe_load(blueprint_path.read_text())["sql_template"]
    clauses = re.findall(r"toDate\(pay_date\)\s*(?:>=|<)\s*toDate\(\{period_(?:start|end)\}\)", sql)
    assert len(clauses) == 2
    predicate = " AND ".join(clauses).replace("{period_start}", "'2026-01-01'").replace("{period_end}", "'2027-01-01'")
    with sqlite3.connect(":memory:") as db:
        db.create_function("toDate", 1, lambda value: value)
        db.execute("CREATE TABLE payroll(pay_date TEXT, pay_period_end_date TEXT, amount INTEGER)")
        db.executemany("INSERT INTO payroll VALUES (?, ?, ?)", [
            ("2026-01-02", "2025-12-27", 100),  # December work paid in January: include.
            ("2026-01-16", "2026-01-10", 200),
            ("2026-12-31", "2026-12-26", 400),
            ("2027-01-01", "2026-12-26", 300),  # 2027 payment: exclude from 2026.
        ])
        assert db.execute("SELECT sum(amount) FROM payroll WHERE " + predicate).fetchone()[0] == 700
        wrong_basis = predicate.replace("pay_date", "pay_period_end_date")
        assert db.execute("SELECT sum(amount) FROM payroll WHERE " + wrong_basis).fetchone()[0] == 900


def test_explicit_coverage_remains_available_and_boundary_allocation_is_not_invented():
    entry = payroll()
    coverage = next(d for d in entry["temporal"]["dimensions"] if d["name"] == "pay_period")
    assert coverage["range"] == {"start_col":"pay_period_start_date", "end_col":"pay_period_end_date"}
    timing = next(a for a in entry["ambiguities"] if a["term"] == "pay_timing")
    assert "Explicit coverage requests override" in timing["clarify_if"]
    assert "boundary-crossing" in timing["clarify_if"]
    assert "do not invent an allocation" in entry["temporal"]["note"]
