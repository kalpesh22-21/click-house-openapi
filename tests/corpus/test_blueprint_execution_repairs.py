"""Contract pins for the imported blueprint failures (live proof under docs/verification)."""
from pathlib import Path
import yaml
from sqlglot import exp
from tools.format_blueprints import parse

ROOT = Path(__file__).resolve().parents[2] / 'app/corpus/data/blueprints'


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
        assert tree.args['from_'].this.this.args.get('distinct')


def test_empty_fact_window_does_not_subtract_from_default_epoch():
    for name in ('category-mom-volatility', 'regular-vs-premium-hours-ratio'):
        tree = sql(name)
        assert any(c.find(exp.Count) and c.find(exp.Null) and c.find(exp.Max)
                   for c in tree.find_all(exp.Case))
