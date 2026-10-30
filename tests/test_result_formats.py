import pytest
from app.result_formats import query_formats, validate_formats, export_headers

CATALOG={'db.employee':{'columns':{
    'zip_code':{'type':'String','format':{'kind':'identifier'}},
    'salary':{'type':'Decimal(18,2)','unit':'USD'},
    'hours':{'type':'Float64','unit':'hours'},
    'name':{'type':'String'},
}}}


def formats(sql,names): return query_formats(sql,names,CATALOG)


def test_identifiers_are_not_numeric_measures_even_through_aliases_and_ctes():
    assert formats('WITH t AS (SELECT zip_code AS zip FROM db.employee) SELECT zip AS ZIP, count(*) AS n FROM t GROUP BY zip', ['ZIP','n'])=={'ZIP':{'kind':'identifier'},'n':{'kind':'integer'}}


def test_unit_lineage_for_aggregates_and_numeric_conversion():
    result=formats('SELECT sum(salary) AS total, avg(salary) AS mean, sum(hours) AS hours, toFloat64(sum(salary)) AS dollars FROM db.employee',['total','mean','hours','dollars'])
    for key in ('total','mean','dollars'): assert result[key]=={'kind':'currency','currency':'USD'}
    assert result['hours']['unit']=='hours'


def test_alias_cannot_invent_currency_or_percent():
    assert formats('SELECT count(*) AS Dollars, 0.15 AS Percentage FROM db.employee',['Dollars','Percentage'])=={'Dollars':{'kind':'integer'},'Percentage':{'kind':'number'}}


def test_currency_divided_by_currency_is_a_number_not_money():
    assert formats('SELECT sum(salary)/nullIf(avg(salary),0) AS ratio FROM db.employee',['ratio'])['ratio']=={'kind':'number'}


def test_csv_headers_describe_raw_units_and_scale():
    assert export_headers(['ZIP','Pay','Rate'], {'ZIP':{'kind':'identifier'},'Pay':{'kind':'currency','currency':'USD'},'Rate':{'kind':'percent','scale':'ratio'}})==['ZIP','Pay (USD)','Rate (ratio)']


@pytest.mark.parametrize('fmt',[{'kind':'currency'},{'kind':'percent'},{'kind':'number','precision':True}])
def test_invalid_metadata_rejected(fmt):
    with pytest.raises(ValueError): validate_formats({'x':fmt})


def test_malformed_kind_and_scale_are_validation_errors():
    import pytest
    from app.result_formats import validate_formats
    for fmt in ({"kind": []}, {"kind": "percent", "scale": []}):
        with pytest.raises(ValueError):
            validate_formats({"value": fmt})


@pytest.mark.parametrize('operator', ['UNION ALL', 'UNION DISTINCT', 'INTERSECT', 'EXCEPT'])
def test_set_operations_match_formats_by_position(operator):
    sql = f'SELECT sum(salary) AS Pay, min(zip_code) AS ZIP FROM db.employee {operator} SELECT max(salary) AS Other, max(zip_code) AS OtherZip FROM db.employee'
    assert formats(sql, ['Pay', 'ZIP']) == {
        'Pay': {'kind': 'currency', 'currency': 'USD'}, 'ZIP': {'kind': 'identifier'},
    }


def test_set_operation_conflicting_units_are_not_money():
    assert formats('SELECT sum(salary) AS Value FROM db.employee UNION ALL SELECT sum(hours) AS Other FROM db.employee', ['Value']) == {'Value': {'kind': 'text'}}


@pytest.mark.parametrize('body', [
    'SELECT sum(salary), min(zip_code) FROM db.employee',
    'SELECT sum(salary), min(zip_code) FROM db.employee UNION ALL SELECT max(salary), max(zip_code) FROM db.employee',
])
def test_cte_column_aliases_preserve_position_and_formats(body):
    assert formats(f'WITH t(pay, zip) AS ({body}) SELECT zip AS ZIP, pay AS Pay FROM t', ['ZIP', 'Pay']) == {
        'ZIP': {'kind': 'identifier'}, 'Pay': {'kind': 'currency', 'currency': 'USD'},
    }


def test_nested_cte_and_derived_table_aliases():
    sql = 'WITH t AS (SELECT sum(salary) AS amount FROM db.employee) SELECT v AS Pay FROM (SELECT amount FROM t) AS renamed(v)'
    assert formats(sql, ['Pay']) == {'Pay': {'kind': 'currency', 'currency': 'USD'}}


def test_compatible_formats_widen_precision_without_changing_units():
    from app.result_formats import _common_format
    assert _common_format([{'kind': 'integer'}, {'kind': 'number'}]) == {'kind': 'number'}
    assert _common_format([{'kind': 'currency', 'currency': 'USD'}, {'kind': 'currency', 'currency': 'USD', 'precision': 4}]) == {'kind': 'currency', 'currency': 'USD', 'precision': 4}
    assert _common_format([{'kind': 'currency', 'currency': 'USD'}, {'kind': 'currency', 'currency': 'EUR'}]) == {'kind': 'text'}
    assert _common_format([{'kind': 'percent', 'scale': 'ratio'}, {'kind': 'percent', 'scale': 'percent'}]) == {'kind': 'text'}
