"""Contract pins for the imported blueprint failures (live proof under docs/verification)."""
import sqlite3
from pathlib import Path

import yaml
from sqlglot import exp

from tests.corpus.native_sort import bind_sort
from tools.format_blueprints import parse

ROOT = Path(__file__).resolve().parents[2] / 'app/corpus/data/blueprints'


def test_department_headcount_defaults_and_zero_opt_in():
    blueprint = yaml.safe_load((ROOT / 'bp-active-headcount-by-department.yaml').read_text())
    defaults = {slot['name']: slot.get('default') for slot in blueprint['slots']}
    assert defaults['order_by'][0]['direction'] == 'DESC'
    assert defaults['zero_headcount'] == 'Exclude'
    # Exercise the relational behavior with staffed, inactive-only, and empty departments.
    with sqlite3.connect(':memory:') as db:
        db.execute("ATTACH DATABASE ':memory:' AS dbpcm_warehouse")
        db.execute('CREATE TABLE dbpcm_warehouse.department (department_code TEXT, department_name TEXT)')
        db.execute('CREATE TABLE dbpcm_warehouse.employee (employee_code TEXT, department_code TEXT, employee_status TEXT)')
        db.executemany('INSERT INTO dbpcm_warehouse.department VALUES (?, ?)', [('A', 'Alpha'), ('B', 'Beta'), ('C', 'Empty'), ('D', 'Inactive')])
        db.executemany('INSERT INTO dbpcm_warehouse.employee VALUES (?, ?, ?)', [('1', 'A', 'Active'), ('2', 'B', 'Active'), ('3', 'B', 'On Leave'), ('4', 'D', 'Terminated')])
        query = bind_sort(blueprint).replace('uniqExact(employee_code)', 'COUNT(DISTINCT employee_code)')
        query = query.replace('{employee_status}', ', '.join(exp.Literal.string(v).sql() for v in defaults['employee_status']))
        query = query.replace('d.department_name IN ({department})', 'TRUE')
        query = query.replace('{order_direction}', "'DESC'").replace('{top_n}', '1000')
        assert db.execute(query.replace('{zero_headcount}', "'Exclude'")).fetchall() == [('B', 'Beta', 2), ('A', 'Alpha', 1)]
        assert len(db.execute(query.replace('{zero_headcount}', "'Include'")).fetchall()) == 4
        db.execute('DELETE FROM dbpcm_warehouse.employee')
        assert db.execute(query.replace('{zero_headcount}', "'Exclude'")).fetchall() == []


def sql(name):
    return parse(yaml.safe_load((ROOT / f'bp-{name}.yaml').read_text())['sql_template'])


def test_expense_medians_are_aggregates_not_per_record_durations():
    tree = sql('expense-cycle-time')
    assert len(list(tree.find_all(exp.ParameterizedAgg))) == 4
    assert {n.name.lower() for n in tree.find_all(exp.ParameterizedAgg)} == {'quantileexactlow', 'quantileexacthigh'}


def test_large_employee_reports_scan_employee_once():
    for name in ('workforce-kpi-bundle', 'employee-record-dq-flags'):
        tree = sql(name)
        assert len([t for t in tree.find_all(exp.Table) if t.name == 'employee']) == 1
    for median in sql('workforce-kpi-bundle').find_all(exp.ParameterizedAgg):
        assert median.expressions == [exp.Literal.number('0.5')]
        assert isinstance(median.args['params'][0], (exp.Case, exp.If))


def test_calendar_streaks_do_not_use_implementation_specific_epoch():
    tree = sql('consecutive-day-streaks')
    assert not any(n.name.lower() == 'torelativedaynum' for n in tree.find_all(exp.Anonymous))
    assert any(n.text('unit').upper() == 'DAY' for n in tree.find_all(exp.DateDiff))


def test_optional_sort_is_outside_distinct_projection():
    for name in ('payroll-codes-unmapped', 'expense-senders-no-punches'):
        tree = sql(name)
        assert tree.args.get('order')
        assert not tree.args.get('distinct')
        assert not any(isinstance(item, exp.Star) for item in tree.expressions)
        assert tree.args['from_'].this.this.args.get('distinct')


def test_empty_fact_window_does_not_subtract_from_default_epoch():
    for name in ('category-mom-volatility', 'regular-vs-premium-hours-ratio'):
        tree = sql(name)
        assert any(c.find(exp.Count) and c.find(exp.Null) and c.find(exp.Max)
                   for c in tree.find_all(exp.Case))


def test_executable_blueprints_have_explicit_projection_columns():
    # The agent's hydrator rejects SELECT * even when Trino can execute it.
    for path in ROOT.glob('*.yaml'):
        entry = yaml.safe_load(path.read_text())
        for node in [entry, *entry.get('composes', [])]:
            if not node.get('sql_template'):
                continue
            for select in parse(node['sql_template']).find_all(exp.Select):
                assert not any(isinstance(item, exp.Star) or isinstance(item, exp.Column) and item.is_star
                               for item in select.expressions), path.name


def test_employee_date_selectors_use_searched_case_for_row_values():
    # ClickHouse 24.8 lowers simple CASE to transform(), which rejects the
    # row-dependent date/department branches. This includes nested date selectors.
    tree = sql('employee-date-summary')
    cases = list(tree.find_all(exp.Case))
    assert cases
    assert all(case.this is None for case in cases)


def test_payroll_group_selectors_use_searched_case_for_row_values():
    # Simple CASE lowers to transform() on ClickHouse and rejects row branches.
    cases = list(sql("payroll-summary").find_all(exp.Case))
    assert cases
    assert all(case.this is None for case in cases)


def test_department_metric_sorting_preserves_zero_negative_and_unknown_values():
    metrics = {
        'average-salary-by-department': 'Average Annual Salary',
        'overtime-by-department': 'Earnings',
        'total-earnings-by-department': 'Total Earnings',
        'earnings-by-department-via-scratch-join': 'Total Earnings',
    }
    for name, metric in metrics.items():
        entry = yaml.safe_load((ROOT / f'bp-{name}.yaml').read_text())
        slot = next(s for s in entry['slots'] if s['name'] == 'order_by')
        assert slot['default'][0]['direction'] == 'DESC'
        assert slot['type'] == 'sort_order'
        template = entry['composes'][-1]['sql_template'] if 'composes' in entry else entry['sql_template']
        # Parse the whole template as well as exercising its ORDER BY on aggregate rows.
        assert parse(template).args.get('order')
        order = template[template.rindex('ORDER BY'):]
        with sqlite3.connect(':memory:') as db:
            db.execute(f'CREATE TABLE results (department TEXT, "{metric}" REAL, "Pay Type" TEXT)')
            db.executemany('INSERT INTO results VALUES (?, ?, ?)', [
                ('Zero', 0, 'Salary'), ('Negative', -10, 'Salary'),
                ('Unknown', None, 'Salary'), ('B', 20, 'Salary'), ('A', 20, 'Salary'),
            ])
            for direction, expected in [
                ('DESC', ['A', 'B', 'Zero', 'Negative', 'Unknown']),
                ('ASC', ['Unknown', 'Negative', 'Zero', 'A', 'B']),
            ]:
                rendered = bind_sort(entry, order, direction=direction, column=metric).replace('{top_n}', '1000')
                actual = [r[0] for r in db.execute('SELECT department FROM results ' + rendered)]
                assert [x for x in actual if x not in {'A', 'B'}] == [x for x in expected if x not in {'A', 'B'}]
                assert set(actual[0:2] if direction == 'DESC' else actual[-2:]) == {'A', 'B'}
            if name.startswith('average'):
                db.execute("INSERT INTO results VALUES ('Hourly dept', 1, 'Hourly')")
                rendered = bind_sort(entry, order).replace('{top_n}', '1000')
                assert db.execute('SELECT department FROM results ' + rendered).fetchone()[0] in {'A', 'B'}
