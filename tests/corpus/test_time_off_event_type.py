"""Every time_off_request blueprint counts one accrual event category by default."""
import re
import sqlite3
from pathlib import Path

import pytest
import yaml

BLUEPRINTS = Path(__file__).parents[2] / 'app/corpus/data/blueprints'
EVENT_TYPE_USE = 'dbpcm_warehouse.time_off_request.event_type'
FIXED = re.compile(r"\bevent_type\s*=\s*'(Time Off Request|Payout Request)'")
# Cross-category summary: its optional event_type filter and event_type grouping are the point.
ALL_EVENT_TYPES = {'bp-accrual-event-summary'}


def _time_off_blueprints():
    for path in sorted(BLUEPRINTS.glob('bp-*.yaml')):
        bp = yaml.safe_load(path.read_text())
        if 'dbpcm_warehouse.time_off_request' in (bp.get('sql_template') or ''):
            yield pytest.param(bp, id=bp['id'])


@pytest.mark.parametrize('bp', _time_off_blueprints())
def test_time_off_blueprint_constrains_event_type(bp):
    assert EVENT_TYPE_USE in bp['uses']
    if bp['id'] in ALL_EVENT_TYPES:
        return
    slot = next((s for s in bp['slots'] if s['name'] == 'event_type'), None)
    if slot is None:
        assert FIXED.search(bp['sql_template']), 'filter event_type or declare an event_type slot'
        return
    assert re.search(r'\bevent_type\s*=\s*\{event_type\}', bp['sql_template'])
    assert slot['type'] == 'enum' and not slot['required']
    assert slot['default'] == 'Time Off Request'
    assert set(slot['enum_values']) == {'Time Off Request', 'Payout Request'}


def _leave_type_hours(event_type):
    bp = yaml.safe_load((BLUEPRINTS / 'bp-timeoff-by-leave-type.yaml').read_text())
    sql = bp['sql_template'].format(window_start="'2026-01-01'", window_end="'2026-12-31'",
                                    concept="'Vacation'", order_direction="'DESC'",
                                    event_type=f"'{event_type}'")
    with sqlite3.connect(':memory:') as db:
        db.execute("ATTACH DATABASE ':memory:' AS dbpcm_warehouse")
        db.create_function('toDate32', 1, lambda value: value)
        db.execute('CREATE TABLE dbpcm_warehouse.time_off_request(employee_code TEXT, earn_code TEXT, '
                   'earn_description TEXT, hours REAL, status TEXT, event_type TEXT, request_date TEXT)')
        db.executemany('INSERT INTO dbpcm_warehouse.time_off_request VALUES (?,?,?,?,?,?,?)', [
            ('E1', 'VAC', 'Vacation', 40, 'Approved', 'Time Off Request', '2026-06-01'),
            ('E1', 'VAC', 'Vacation', 1000, 'Approved', 'Payout Request', '2026-06-01'),
            ('E1', 'VAC', 'Vacation', 500, 'Requested', 'Time Off Request', '2026-06-01'),
            ('E1', 'VAC', 'Vacation', 200, 'Approved', 'Time Off Request', '2025-06-01'),
        ])
        return db.execute(sql).fetchall()


def test_leave_type_hours_default_excludes_payouts():
    bp = yaml.safe_load((BLUEPRINTS / 'bp-timeoff-by-leave-type.yaml').read_text())
    default = next(s['default'] for s in bp['slots'] if s['name'] == 'event_type')
    assert _leave_type_hours(default) == [('Vacation', 'VAC', 40)]
    assert _leave_type_hours('Payout Request') == [('Vacation', 'VAC', 1000)]
