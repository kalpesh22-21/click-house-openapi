"""Fixed rankings expose no sorting; general reports expose their output columns."""
from pathlib import Path

import pytest
import yaml

from tools.format_blueprints import parse

ROOT = Path(__file__).parents[2] / 'app/corpus/data/blueprints'
BLUEPRINTS = [yaml.safe_load(p.read_text()) for p in sorted(ROOT.glob('*.yaml'))]


FIXED_RANKINGS = {
    'bp-approved-timeoff-ranking', 'bp-compensation-ranking',
    'bp-expense-approval-backlog', 'bp-latest-hired-employees',
    'bp-longest-tenured-employees', 'bp-manager-span-of-control',
    'bp-negative-pto-balances', 'bp-quarterly-terminations-with-top-reasons',
    'bp-top-changed-fields', 'bp-top-codes-in-category', 'bp-top-expense-spenders',
    'bp-top-home-zip-city', 'bp-top-payroll-codes', 'bp-top-timeoff-users',
}


@pytest.mark.parametrize('blueprint', BLUEPRINTS, ids=lambda b: b['id'])
def test_order_by_exposes_all_and_only_terminal_output_columns(blueprint):
    nodes = blueprint.get('composes') or [blueprint]
    terminal = max((n for n in nodes if n.get('sql_template')), key=lambda n: n.get('order', 0))
    slots = {s['name']: s for s in blueprint['slots']}
    if blueprint['id'] in FIXED_RANKINGS:
        assert not {'order_by', 'order_direction'} & slots.keys()
        assert not {'order_by', 'order_direction'} & set(blueprint['slots_summary'].split(', '))
        assert all('{order_direction}' not in n.get('sql_template', '') and
                   '{order_by}' not in n.get('sql_template', '') for n in nodes)
        assert parse(terminal['sql_template']).args.get('order')
        return
    order = slots['order_by']
    assert order['type'] == 'sort_order'
    assert 'binding' not in order
    assert order['required'] is False
    assert order['default'][0]['column'] in order['allowed_columns']
    assert order['default'][0]['direction'] in {'ASC', 'DESC'}
    assert order['allowed_columns'] == parse(terminal['sql_template']).named_selects
    assert 'order_direction' not in slots
    assert 'order_by' in blueprint['slots_summary'].split(', ')
    assert 'ORDER BY {order_by}' in terminal['sql_template']
    assert len(parse(terminal['sql_template']).args['order'].expressions) == 1
    assert 'NULLS ' not in terminal['sql_template']
    assert '{order_by}' in terminal['sql_template']
    assert 'CASE WHEN {order_by}' not in terminal['sql_template']


def test_fixed_ranking_inventory_is_present():
    assert FIXED_RANKINGS <= {b['id'] for b in BLUEPRINTS}


def test_top_expense_spenders_selects_highest_amounts_with_deterministic_ties():
    import sqlite3

    blueprint = next(b for b in BLUEPRINTS if b['id'] == 'bp-top-expense-spenders')
    sql = blueprint['sql_template'].format(
        top_n=2, expense_window_start="'2026-01-01'", expense_window_end="'2026-04-01'",
    ).replace('any(e.employee_name)', 'min(e.employee_name)')
    with sqlite3.connect(':memory:') as db:
        db.execute("ATTACH DATABASE ':memory:' AS dbpcm_warehouse")
        db.create_function('toFloat64', 1, float)
        db.execute('CREATE TABLE dbpcm_warehouse.employee(employee_code TEXT,employee_name TEXT)')
        db.execute('CREATE TABLE dbpcm_warehouse.expense(employee_code TEXT,amount_approved REAL,expense_status TEXT,expense_paid_date TEXT)')
        db.executemany('INSERT INTO dbpcm_warehouse.employee VALUES (?,?)',
                       [('A','Alice'), ('B','Bob'), ('E','Eve'), ('F','Fred')])
        db.executemany('INSERT INTO dbpcm_warehouse.expense VALUES (?,?,?,?)',
                       [(c,a,'Paid','2026-03-31') for c,a in [('A',600),('B',1200),('E',1800),('F',1200)]])
        assert db.execute(sql).fetchall() == [('E','Eve',1800), ('B','Bob',1200)]


@pytest.mark.parametrize('name,column,direction', [
    ('accrual-code-dictionary', 'Holder Count', 'DESC'),
    ('active-headcount-by-department', 'Headcount', 'DESC'),
    ('pending-timeoff-queue', 'Days Waiting', 'DESC'),
    ('earnings-monthly-trend', 'Month', 'ASC'),
    ('average-salary-by-department', 'Average Annual Salary', 'DESC'),
    ('employee-directory', 'Name', 'ASC'),
    ('expense-line-size-distribution', 'Band Start', 'ASC'),
    ('upcoming-anniversaries', 'Anniversary Day of Year', 'ASC'),
    ('expense-payroll-recon', 'Absolute Difference', 'DESC'),
])
def test_defaults_match_report_purpose(name, column, direction):
    bp = next(b for b in BLUEPRINTS if b['id'] == 'bp-' + name)
    slots = {s['name']: s for s in bp['slots']}
    assert slots['order_by']['default'][0]['column'] == column
    assert slots['order_by']['default'][0]['direction'] == direction


def test_configurable_ordering_has_no_hidden_direction_or_secondary_key():
    for bp in BLUEPRINTS:
        if bp['id'] in FIXED_RANKINGS:
            continue
        nodes = bp.get('composes') or [bp]
        for node in nodes:
            sql = node.get('sql_template', '')
            if '{order_by}' not in sql:
                continue
            assert sql.count('{order_by}') == 1 and '{order_direction}' not in sql
            assert 'ORDER BY {order_by}' in sql
            assert len(parse(sql).args['order'].expressions) == 1
