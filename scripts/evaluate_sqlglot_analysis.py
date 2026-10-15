"""Offline, synthetic SQLGlot analysis comparison; no application/DB credentials.

Run under each candidate environment:
    python scripts/evaluate_sqlglot_analysis.py > analysis.jsonl
The output is evidence of inference, not a guarantee of dialect equivalence.
"""
import json

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify

SCHEMA = {
    "warehouse": {"staff": {
        "salary": "Nullable(Decimal(18,6))",
        "amount": "Decimal(18,6)",
        "stamp": "Int64",
    }},
}
QUERIES = [
    "SELECT salary + amount FROM staff",
    "SELECT coalesce(salary,amount) FROM staff",
    "SELECT CASE WHEN stamp>0 THEN salary ELSE amount END FROM staff",
    "SELECT CASE WHEN stamp>0 THEN amount ELSE 0 END FROM staff",
    "SELECT sum(amount) OVER (PARTITION BY stamp) FROM staff",
    "SELECT sum(b.amount) FROM staff a LEFT JOIN staff b ON a.stamp=b.stamp",
    "SELECT sum(x) FROM (SELECT amount x FROM staff UNION ALL SELECT salary x FROM staff)",
    "WITH s AS (SELECT salary AS x FROM staff) SELECT sum(x) FROM s",
    "SELECT round(salary,2) FROM staff",
]


def main():
    for sql in QUERIES:
        tree = qualify(sqlglot.parse_one(sql, read="clickhouse"), schema=SCHEMA, db="warehouse", dialect="clickhouse")
        annotate_types(tree, schema=SCHEMA, dialect="clickhouse")
        nodes = []
        for node in tree.walk():
            if isinstance(node, (exp.Column, exp.Sum, exp.Add, exp.Case, exp.Coalesce, exp.Round, exp.Window)):
                nodes.append({
                    "node": type(node).__name__, "expression": node.sql(),
                    "type": str(node.type),
                    "nullable": node.type.args.get("nullable") if node.type else None,
                })
        print(json.dumps({"version": sqlglot.__version__, "sql": sql, "nodes": nodes}))


if __name__ == "__main__":
    main()
