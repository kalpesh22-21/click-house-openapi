"""Execute the actual template against opposing submission/payment dates."""
import sqlite3
from pathlib import Path
import yaml


def test_expense_submitters_use_submission_window_and_include_unpaid_expenses():
    path=Path(__file__).parents[2]/'app/corpus/data/blueprints/bp-expense-senders-no-punches.yaml'
    blueprint=yaml.safe_load(path.read_text())
    sql=blueprint['sql_template'].replace('{win}','3').replace('{order_direction}',"'ASC'")
    assert 'dbpcm_warehouse.expense.date_time_submitted' in blueprint['uses']
    assert 'dbpcm_warehouse.expense.expense_paid_date' not in blueprint['uses']
    with sqlite3.connect(':memory:') as db:
        db.execute("ATTACH DATABASE ':memory:' AS dbpcm_warehouse")
        db.create_function('today',0,lambda:'2026-10-30')
        db.create_function('addMonths',2,lambda day,offset:'2026-07-30')
        db.create_function('toDate',1,lambda value:value[:10] if value else None)
        db.execute('CREATE TABLE dbpcm_warehouse.employee(employee_code TEXT,employee_name TEXT,department_name TEXT)')
        db.execute('CREATE TABLE dbpcm_warehouse.expense(employee_code TEXT,date_time_submitted TEXT,expense_paid_date TEXT)')
        db.execute('CREATE TABLE dbpcm_warehouse.punch_pair(employee_code TEXT,punch_date TEXT)')
        db.executemany('INSERT INTO dbpcm_warehouse.employee VALUES (?,?,?)',[(str(i),f'Employee {i}','Sales') for i in range(1,5)])
        db.executemany('INSERT INTO dbpcm_warehouse.expense VALUES (?,?,?)',[
            ('1','2026-09-01 10:00:00','2026-07-01'),
            ('2','2026-07-01 10:00:00','2026-09-01'),
            ('3','2026-09-01 10:00:00',None),
            ('4','2026-09-01 10:00:00','2026-09-15'),
        ])
        db.execute("INSERT INTO dbpcm_warehouse.punch_pair VALUES ('4','2026-09-02')")
        assert [row[0] for row in db.execute(sql)]==['1','3']
