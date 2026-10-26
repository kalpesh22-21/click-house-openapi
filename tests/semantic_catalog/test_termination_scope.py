"""Recorded termination status/date must not imply a rehire exclusion."""
from pathlib import Path
import yaml
from app.semantic_catalog.export import build_catalog_export


def test_termination_rules_are_separate_and_exported():
    entry = yaml.safe_load(Path('app/semantic_catalog/data/employee.yaml').read_text())
    rules = {r['id']:r for r in entry['rules']}
    assert 'rehired_not_terminated' not in rules
    assert rules['terminated_employee_status']['predicate'] == "employee_status = 'Terminated'"
    assert 'most_recent_hire_date' not in rules['terminations_in_period']['predicate']
    exported = str(build_catalog_export())
    assert 'terminated_employee_status' in exported
    assert 'terminations_in_period' in exported
    assert 'rehired_not_terminated' not in exported


def test_recorded_status_and_event_window_keep_same_day_and_later_rehires():
    import sqlite3
    entry = yaml.safe_load(Path('app/semantic_catalog/data/employee.yaml').read_text())
    rules = {r['id']:r for r in entry['rules']}
    rows = [
        ('before', 'Terminated', '2026-05-01', '2025-01-01'),
        ('same_day', 'Terminated', '2026-05-01', '2026-05-01'),
        ('later', 'Terminated', '2026-05-01', '2026-06-01'),
        ('no_date', 'Terminated', None, None),
        ('active_now', 'Active', '2026-05-01', '2026-06-01'),
    ]
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE employee(employee_code,employee_status,termination_date,most_recent_hire_date)')
        db.executemany('INSERT INTO employee VALUES (?,?,?,?)', rows)
        status = rules['terminated_employee_status']['predicate']
        window = rules['terminations_in_period']['predicate'].replace('<period_start>', "'2026-01-01'").replace('<period_end>', "'2027-01-01'")
        assert {r[0] for r in db.execute('SELECT employee_code FROM employee WHERE '+status)} == {'before','same_day','later','no_date'}
        assert {r[0] for r in db.execute('SELECT employee_code FROM employee WHERE '+window)} == {'before','same_day','later','active_now'}
