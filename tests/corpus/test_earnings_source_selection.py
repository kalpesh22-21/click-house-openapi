"""Execute earnings templates with opposing payment/work dates and register types."""
import sqlite3
from pathlib import Path

import yaml

from tests.corpus.native_sort import bind_sort

ROOT = Path(__file__).parents[2] / 'app/corpus/data/blueprints'


def test_default_gross_uses_payment_year_without_status_or_timecard_substitution():
    bp = yaml.safe_load((ROOT / 'bp-gross-earnings-summary.yaml').read_text())
    sql = bind_sort(bp).replace('{period_start}', "'2026-01-01'").replace('{period_end}', "'2027-01-01'")
    with sqlite3.connect(':memory:') as db:
        db.execute("ATTACH DATABASE ':memory:' AS dbpcm_warehouse")
        db.create_function('toDate', 1, lambda value: value[:10])
        db.execute('CREATE TABLE dbpcm_warehouse.payroll(amount REAL, register_type TEXT, pay_date TEXT, pay_period_end_date TEXT, employee_status TEXT)')
        db.executemany('INSERT INTO dbpcm_warehouse.payroll VALUES (?,?,?,?,?)', [
            (100, 'EARN', '2026-01-02', '2025-12-28', 'Terminated'),
            (200, 'EARN', '2027-01-02', '2026-12-28', 'Active'),
            (30, 'EARN', '2026-06-01', '2026-05-28', 'Active'),
            (-10, 'EARN', '2026-06-01', '2026-05-28', 'Active'),
            (500, 'NETPAYDIST', '2026-06-01', '2026-05-28', 'Active'),
        ])
        assert db.execute(sql).fetchone()[0] == 120
        explicit = yaml.safe_load((ROOT / 'bp-gross-earnings-for-pay-period.yaml').read_text())
        assert db.execute(bind_sort(explicit).replace('{period}', "'2026-12-28'")).fetchone()[0] == 200
        assert explicit['slots'][0]['required'] is True


def test_timecard_grouping_uses_searched_case_and_leaves_dedup_to_mcp():
    # ClickHouse 24.8 lowers simple CASE to transform and rejects its variable
    # result branches (ILLEGAL_COLUMN). Searched CASE keeps the supported path.
    import sqlglot
    from sqlglot import exp
    bp = yaml.safe_load((ROOT / 'bp-timecard-earnings-summary.yaml').read_text())
    sql = bind_sort(bp)
    import re
    sql = re.sub(r'\{[a-z_]+\}', "'day'", sql)
    tree = sqlglot.parse_one(sql, dialect='clickhouse')
    assert all(case.this is None for case in tree.find_all(exp.Case))
    assert 'FROM dbpcm_warehouse.total_earning FINAL' not in sql
