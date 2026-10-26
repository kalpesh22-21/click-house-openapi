"""Simple CASE arms are values; searched CASE and IF arms are predicates."""
import sqlite3
import pytest
import sqlglot
from app.trino_client import _map_clickhouse_functions

CASES = [
    ("CASE 'company' WHEN 'department' THEN 'Dept' WHEN 'month' THEN 'Month' ELSE 'Company' END", 'Company'),
    ("CASE 'department' WHEN 'department' THEN 'Dept' ELSE 'Company' END", 'Dept'),
    ("CASE 2 WHEN 1 THEN 'one' WHEN 2 THEN 'two' ELSE 'other' END", 'two'),
    ("CASE NULL WHEN NULL THEN 'match' ELSE 'no match' END", 'no match'),
    ("CASE WHEN 0 THEN 'wrong' WHEN 2 THEN 'right' ELSE 'wrong' END", 'right'),
    ("if(2,'right','wrong')", 'right'),
    ("CASE 'x' WHEN 'x' THEN if(0,'wrong','right') ELSE 'wrong' END", 'right'),
    ("CASE WHEN 1 THEN CASE 'x' WHEN 'x' THEN 'right' ELSE 'wrong' END ELSE 'wrong' END", 'right'),
]


@pytest.mark.parametrize('expression,expected', CASES)
def test_case_branch_meaning_survives_conversion(expression, expected):
    tree=sqlglot.parse_one('SELECT '+expression,read='clickhouse')
    _map_clickhouse_functions(tree)
    trino=tree.sql(dialect='trino')
    sqlite=sqlglot.transpile(trino,read='trino',write='sqlite')[0]
    with sqlite3.connect(':memory:') as conn:
        assert conn.execute(sqlite).fetchone()[0]==expected
    if expression.startswith("CASE 'company'"):
        assert "WHEN 'department'" in trino
        assert "WHEN 'month'" in trino
