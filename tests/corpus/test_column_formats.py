"""Every authored output, including terminal composed outputs, has explicit display semantics."""
import re
import sqlglot
import pytest
from app.corpus.loader import load_blueprints
from app.result_formats import validate_formats

BLUEPRINTS=load_blueprints()


@pytest.mark.parametrize('blueprint',list(BLUEPRINTS.values()),ids=list(BLUEPRINTS))
def test_every_terminal_column_has_valid_authored_format(blueprint):
    sql=blueprint.get('sql_template') or blueprint['composes'][-1]['sql_template']
    tree=sqlglot.parse_one(re.sub(r'\{\w+\}',"'value'",sql),dialect='clickhouse')
    assert set(blueprint['column_formats'])=={e.alias_or_name for e in tree.selects}
    assert validate_formats(blueprint['column_formats'])==blueprint['column_formats']


def test_identifier_and_percentage_regressions():
    assert BLUEPRINTS['bp-top-home-zip-city']['column_formats']['Zip Code']=={'kind':'identifier'}
    assert BLUEPRINTS['bp-denied-expenses']['column_formats']['Denial Rate']['scale']=='ratio'
    assert BLUEPRINTS['bp-timeoff-denial-rate']['column_formats']['Denial Rate Pct']['scale']=='percent'
    assert BLUEPRINTS['bp-gross-earnings-summary']['column_formats']['Total Gross Earnings']['currency']=='USD'
    # Plan-specific units are not established by the catalog.
    assert 'unit' not in BLUEPRINTS['bp-accrual-balance-summary']['column_formats']['Beginning Balance']
