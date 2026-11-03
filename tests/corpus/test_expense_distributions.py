"""Execute the expense distribution templates: one category axis, never a cross-tab."""
import math
import sqlite3
from pathlib import Path

import yaml

BLUEPRINTS = Path(__file__).parents[2] / 'app/corpus/data/blueprints'
ROWS = [
    # account code, account description, status, paid date, approved amount
    ('M1', 'Meals', 'Approved', '2026-09-05', 5.0),
    ('M1', 'Meals', 'Paid', '2026-09-06', 99.99),
    ('T1', 'Travel', 'Marked as Paid', '2026-09-07', 100.0),
    ('T1', 'Travel', 'Paid', '2026-09-08', 250.0),
    ('T1', 'Travel', 'Paid', '2026-09-09', None),
    ('T1', 'Travel', 'Denied', '2026-09-10', 900.0),
    ('M1', 'Meals', 'Paid', '2026-10-15', 400.0),
]


class _Any:
    def __init__(self):
        self.value = None

    def step(self, value):
        if self.value is None:
            self.value = value

    def finalize(self):
        return self.value


def _run(blueprint_id, replacements=(), **binds):
    blueprint = yaml.safe_load((BLUEPRINTS / f'{blueprint_id}.yaml').read_text())
    sql = blueprint['sql_template']
    for old, new in replacements:
        assert sql.count(old) == 1, old
        sql = sql.replace(old, new)
    for name, value in binds.items():
        sql = sql.replace('{' + name + '}', value)
    with sqlite3.connect(':memory:') as db:
        db.execute("ATTACH DATABASE ':memory:' AS dbpcm_warehouse")
        db.create_function('toFloat64', 1, lambda v: None if v is None else float(v))
        db.create_function('toInt32', 1, lambda v: None if v is None else int(v))
        db.create_function('toString', 1, lambda v: None if v is None else str(v))
        db.create_function('floor', 1, lambda v: None if v is None else math.floor(v))
        db.create_aggregate('any', 1, _Any)
        db.execute('CREATE TABLE dbpcm_warehouse.expense(expense_account_code TEXT, expense_account_description TEXT,'
                   ' expense_status TEXT, expense_paid_date TEXT, amount_approved REAL)')
        db.executemany('INSERT INTO dbpcm_warehouse.expense VALUES (?,?,?,?,?)', ROWS)
        return blueprint, db.execute(sql).fetchall()


WINDOW = {'expense_window_start': "'2026-09-01'", 'expense_window_end': "'2026-09-30'"}


def test_line_size_distribution_is_one_row_per_labelled_band():
    # The omitted optional `account` slot: its optional_pattern replaces the whole predicate.
    omitted = [('expense_account_description = {account}', 'TRUE')]
    blueprint, rows = _run('bp-expense-line-size-distribution', omitted, band='100', order_direction="'ASC'", **WINDOW)
    assert blueprint['result_grain'] == ['Amount Band']
    assert rows == [('$0 to under $100', 2), ('$100 to under $200', 1), ('$200 to under $300', 1)]


def test_line_size_distribution_can_narrow_to_one_account():
    _, rows = _run('bp-expense-line-size-distribution', band='100', order_direction="'DESC'", account="'Travel'", **WINDOW)
    assert rows == [('$200 to under $300', 1), ('$100 to under $200', 1)]


def test_category_distribution_covers_every_account_with_shares_of_the_total():
    blueprint, rows = _run('bp-expense-category-distribution', order_direction="'DESC'", **WINDOW)
    assert 'top_n' not in blueprint['slots_summary'] and 'LIMIT' not in blueprint['sql_template']
    # Spend and share skip an expense without an approved amount.
    assert rows == [('T1', 'Travel', 350.0, 76.92), ('M1', 'Meals', 104.99, 23.08)]
    assert math.isclose(sum(row[3] for row in rows), 100.0)
