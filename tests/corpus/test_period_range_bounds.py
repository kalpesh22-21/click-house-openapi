"""Calendar windows include the final requested day and exclude the next day."""
import re
import sqlite3
from pathlib import Path

import pytest
import yaml

CORPUS = Path(__file__).parents[2] / 'app/corpus/data/blueprints'
BLUEPRINTS = [(p.name, yaml.safe_load(p.read_text())) for p in sorted(CORPUS.glob('*.yaml'))]
RANGES = [(name, bp, slot) for name, bp in BLUEPRINTS for slot in bp.get('slots', [])
          if slot['type'] == 'period_range']


@pytest.mark.parametrize('name,bp,slot', RANGES, ids=[f'{name}:{slot["name"]}' for name, _, slot in RANGES])
def test_period_range_template_and_description_share_half_open_contract(name, bp, slot):
    end = re.escape('{' + slot['name'] + '_end}')
    sql = bp['sql_template']
    assert 'start is inclusive and end is exclusive' in slot['description'].lower()
    assert not re.search(r'BETWEEN[^\n]*' + end, sql, re.IGNORECASE), name
    assert not re.search(r'<=\s*(?:toDate\()?'+end, sql), name
    assert re.search(r'(?<![<>=])<\s*(?:toDate\()?'+end, sql), name


@pytest.mark.parametrize('start,last,end', [
    ('2026-01-01', '2026-03-31', '2026-04-01'),
    ('2024-02-01', '2024-02-29', '2024-03-01'),
    ('2026-01-01', '2026-12-31', '2027-01-01'),
    ('2026-03-31', '2026-03-31', '2026-04-01'),
])
def test_timeoff_executes_full_last_day_but_not_next_day(start, last, end):
    bp = yaml.safe_load((CORPUS / 'bp-approved-timeoff-hours.yaml').read_text())
    sql = bp['sql_template'].format(window_start=repr(start), window_end=repr(end),
                                    dept="'Sales'", order_direction="'DESC'")
    with sqlite3.connect(':memory:') as db:
        db.execute("ATTACH DATABASE ':memory:' AS dbpcm_warehouse")
        db.create_function('toDate32', 1, lambda value: value[:10])
        db.execute('CREATE TABLE dbpcm_warehouse.employee(employee_code TEXT, department_name TEXT)')
        db.execute("INSERT INTO dbpcm_warehouse.employee VALUES ('E1', 'Sales')")
        db.execute('CREATE TABLE dbpcm_warehouse.time_off_request(employee_code TEXT, hours REAL, status TEXT, event_type TEXT, request_date TEXT)')
        db.executemany('INSERT INTO dbpcm_warehouse.time_off_request VALUES (?,?,?,?,?)', [
            ('E1', 24, 'Approved', 'Time Off Request', start + ' 00:00:00'),
            ('E1', 48, 'Approved', 'Time Off Request', last + ' 23:59:59.999999'),
            ('E1', 900, 'Approved', 'Time Off Request', end + ' 00:00:00'),
            ('E1', 3000, 'Approved', 'Payout Request', last + ' 12:00:00'),
            ('E1', 500, 'Pending', 'Time Off Request', last + ' 12:00:00'),
        ])
        assert db.execute(sql).fetchall() == [('Sales', 72)]


def test_expense_distribution_excludes_next_midnight():
    bp = yaml.safe_load((CORPUS / 'bp-expense-category-distribution.yaml').read_text())
    sql = bp['sql_template'].format(expense_window_start="'2026-01-01'",
                                    expense_window_end="'2026-04-01'", order_direction="'DESC'")
    class AnyValue:
        def step(self, value):
            self.value = value
        def finalize(self):
            return self.value
    with sqlite3.connect(':memory:') as db:
        db.execute("ATTACH DATABASE ':memory:' AS dbpcm_warehouse")
        db.create_function('toFloat64', 1, float)
        db.create_aggregate('any', 1, AnyValue)
        db.execute('CREATE TABLE dbpcm_warehouse.expense(expense_account_code TEXT, expense_account_description TEXT, amount_approved REAL, expense_status TEXT, expense_paid_date TEXT)')
        db.executemany('INSERT INTO dbpcm_warehouse.expense VALUES (?,?,?,?,?)', [
            ('Travel', 'Travel', 24, 'Paid', '2026-01-01 00:00:00'),
            ('Travel', 'Travel', 48, 'Paid', '2026-03-31 23:59:59.999999'),
            ('Travel', 'Travel', 900, 'Paid', '2026-04-01 00:00:00'),
        ])
        assert db.execute(sql).fetchall() == [('Travel', 'Travel', 72, 100)]
