"""All executable corpus templates avoid known legacy ClickHouse failure shapes."""
from pathlib import Path

import pytest
import yaml
from sqlglot import exp

from tools.format_blueprints import parse

ROOT = Path(__file__).parents[2] / 'app/corpus/data/blueprints'


@pytest.mark.parametrize('path', sorted(ROOT.glob('*.yaml')), ids=lambda p: p.stem)
def test_no_legacy_sql_shapes(path):
    blueprint = yaml.safe_load(path.read_text())
    for node in [blueprint, *blueprint.get('composes', [])]:
        if not node.get('sql_template'):
            continue
        tree = parse(node['sql_template'])
        assert tree.find(exp.Final) is None, 'MCP owns storage-version deduplication'
        for case in tree.find_all(exp.Case):
            if case.this is not None:
                assert not any(branch.args['true'].find(exp.Column) for branch in case.args['ifs']), (
                    'Use searched CASE: ClickHouse transform requires constant result branches'
                )
        for function in tree.find_all(exp.Anonymous):
            if function.name.lower() == 'datetrunc' and len(function.expressions) == 2:
                value = function.expressions[1]
                assert not (isinstance(value, exp.Column) and value.name in {'punch_date', 'pay_period_end_date'}), (
                    'Date32 must be explicitly converted before dateTrunc on ClickHouse 24.8'
                )


@pytest.mark.parametrize('unit', ['MONTH', 'WEEK'])
def test_date32_bucket_conversion_is_supported_by_trino_mapping(unit):
    from app.trino_client import _map_clickhouse_functions
    from app.trino_semantics import annotate_input_types
    offset = "1 - toDayOfMonth(punch_date)" if unit == 'MONTH' else "-(dateDiff('day',toDate32('1900-01-01'),punch_date) % 7)"
    tree = parse(f"SELECT addDays(punch_date, {offset}) FROM dbpcm_warehouse.total_earning")
    annotate_input_types(tree, {'dbpcm_warehouse.total_earning': {'columns': {'punch_date': {'type': 'Date32'}}}})
    _map_clickhouse_functions(tree)
    sql = tree.sql(dialect='trino')
    assert 'DATE_ADD' in sql or 'INTERVAL' in sql
    assert 'TIMESTAMP' not in sql
