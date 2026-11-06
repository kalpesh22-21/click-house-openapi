"""Execute the authored measure against mixed accrual event categories."""
import sqlite3
from pathlib import Path

import yaml

from tests.corpus.native_sort import bind_sort


def test_approved_hours_excludes_payouts_pending_and_other_periods():
    path = Path(__file__).parents[2] / 'app/corpus/data/blueprints/bp-approved-timeoff-hours.yaml'
    bp = yaml.safe_load(path.read_text())
    assert 'dbpcm_warehouse.time_off_request.event_type' in bp['uses']
    sql = bind_sort(bp).format(top_n=1000, window_start="'2026-01-01'", window_end="'2026-12-31'", dept="'Sales'", order_direction="'DESC'")
    with sqlite3.connect(':memory:') as db:
        db.execute("ATTACH DATABASE ':memory:' AS dbpcm_warehouse")
        db.create_function('toDate32', 1, lambda value: value)
        db.execute('CREATE TABLE dbpcm_warehouse.employee(employee_code TEXT, department_name TEXT)')
        db.execute("INSERT INTO dbpcm_warehouse.employee VALUES ('E1', 'Sales')")
        db.execute('CREATE TABLE dbpcm_warehouse.time_off_request(employee_code TEXT, hours REAL, status TEXT, event_type TEXT, request_date TEXT)')
        db.executemany('INSERT INTO dbpcm_warehouse.time_off_request VALUES (?,?,?,?,?)', [
            ('E1', 40, 'Approved', 'Time Off Request', '2026-06-01'),
            ('E1', 1000, 'Approved', 'Payout Request', '2026-06-01'),
            ('E1', 500, 'Pending', 'Time Off Request', '2026-06-01'),
            ('E1', 200, 'Approved', 'Time Off Request', '2025-06-01'),
            ('E1', None, 'Approved', 'Time Off Request', '2026-06-01'),
        ])
        assert db.execute(sql).fetchall() == [('Sales', 40)]
