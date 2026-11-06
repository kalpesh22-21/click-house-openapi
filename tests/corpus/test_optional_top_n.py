"""Optional display limits must not change a measure or silently cap full reports."""
import sqlite3
from pathlib import Path

import pytest
import yaml
from sqlglot import exp

from tests.corpus.native_sort import bind_sort
from tools.format_blueprints import parse

ROOT = Path(__file__).parents[2] / 'app/corpus/data/blueprints'
BLUEPRINTS = [yaml.safe_load(p.read_text()) for p in ROOT.glob('*.yaml')]
OPTIONAL = [b for b in BLUEPRINTS if any(
    s['name'] == 'top_n' and s.get('optional_pattern') == 'TRUE'
    for s in b.get('slots', [])
)]


def test_optional_top_n_coverage():
    assert len(OPTIONAL) == 42
    ids = {b['id'] for b in OPTIONAL}
    assert {'bp-approved-timeoff-hours', 'bp-active-headcount-by-department',
            'bp-expense-per-capita', 'bp-overtime-by-department',
            'bp-supervision-anomalies'} <= ids
    # Full distributions, trends and scalar totals are not ranked subsets.
    assert not ids & {'bp-expense-category-distribution', 'bp-expense-monthly-trend',
                      'bp-workforce-kpi-bundle', 'bp-timeoff-funnel'}


@pytest.mark.parametrize('bp', OPTIONAL, ids=lambda b: b['id'])
def test_optional_top_n_is_bounded_outer_display_limit(bp):
    slot = next(s for s in bp['slots'] if s['name'] == 'top_n')
    assert slot['type'] == 'positive_integer'
    assert slot['required'] is False
    assert 'default' not in slot
    assert (slot['min_value'], slot['max_value']) == (1, 1000)
    assert 'top_n' in bp['slots_summary'].split(', ')
    tree = parse(bp['sql_template'])
    assert tree.args['order']
    limit = tree.args['limit']
    assert isinstance(limit.expression, exp.Placeholder)
    assert limit.expression.name == 'top_n'
    assert len(list(tree.find_all(exp.Limit))) == 1


@pytest.mark.parametrize('direction,expected', [('DESC', [('A', 7), ('B', 6)]),
                                               ('ASC', [('D', 0), ('B', 6)])])
def test_timeoff_top_n_preserves_measure_date_boundary_without_implied_tie_order(direction, expected):
    bp = yaml.safe_load((ROOT / 'bp-approved-timeoff-hours.yaml').read_text())
    sql = bind_sort(bp, direction=direction).replace('e.department_name IN ({dept})', 'TRUE').format(
        window_start="'2026-03-31'", window_end="'2026-04-01'",
        order_direction=repr(direction), top_n=2,
    )
    with sqlite3.connect(':memory:') as db:
        db.execute("ATTACH DATABASE ':memory:' AS dbpcm_warehouse")
        db.create_function('toDate32', 1, lambda s: s[:10])
        db.execute('CREATE TABLE dbpcm_warehouse.employee(employee_code TEXT, department_name TEXT)')
        db.executemany('INSERT INTO dbpcm_warehouse.employee VALUES (?,?)', [(n,n) for n in 'DCBA'])
        db.execute('CREATE TABLE dbpcm_warehouse.time_off_request(employee_code TEXT, hours REAL, status TEXT, event_type TEXT, request_date TEXT)')
        db.executemany('INSERT INTO dbpcm_warehouse.time_off_request VALUES (?,?,?,?,?)', [
            (n, hours, 'Approved', 'Time Off Request', '2026-03-31 23:59:59.999999')
            for n, hours in [('D',0), ('C',6), ('B',6), ('A',2), ('A',5)]
        ])
        db.executemany('INSERT INTO dbpcm_warehouse.time_off_request VALUES (?,?,?,?,?)', [
            ('D', 1000, 'Approved', 'Payout Request', '2026-03-31'),
            ('D', 1000, 'Pending', 'Time Off Request', '2026-03-31'),
            ('D', 1000, 'Approved', 'Time Off Request', '2026-04-01'),
        ])
        rows = db.execute(sql).fetchall()
        assert [row[1] for row in rows] == [row[1] for row in expected]
        assert rows[0][0] == expected[0][0]
        assert rows[1][0] in {"B", "C"}
