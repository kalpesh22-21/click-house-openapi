"""Salary guidance must not turn sample observations into domain guarantees.

No private view SQL or live tenant measurements belong in these tests.
"""

from app.semantic_catalog import loader


def _entry():
    return loader.load_semantic_catalog()['dbpcm_warehouse.employee']


def test_salary_basis_names_catalogued_columns_and_annual_default():
    entry = _entry()
    basis = next(a for a in entry['ambiguities'] if a['term'] == 'salary_basis')
    assert set(basis['resolves_to']) == {'annual_salary', 'hourly_salary', 'rate_1'}
    assert set(basis['resolves_to']) <= entry['columns'].keys()
    assert 'annual_salary' in basis['default']
    assert 'zero or NULL' in basis['default']
    for name in ('hourly_salary', 'rate_1', 'pay_frequency', 'scheduled_pay_period_hours'):
        assert name in basis['clarify_if']
        assert name in entry['columns']


def test_salary_values_remain_nullable_without_universal_zero_guarantees():
    for name in ('annual_salary', 'hourly_salary', 'rate_1'):
        column = _entry()['columns'][name]
        assert column['type'].startswith('Nullable(Decimal(')
        assert 'never NULL' not in column['description']
    annual = _entry()['columns']['annual_salary']['description']
    assert 'default' in annual
    assert 'do not assume' in annual
    assert 'quantileExactLow(0.5)' in annual


def test_hourly_value_is_not_documented_as_a_calculated_annual_ratio():
    description = _entry()['columns']['hourly_salary']['description']
    assert 'source record' in description
    assert 'does not independently calculate' in description


def test_pay_classification_does_not_claim_equivalence_to_positive_salary():
    description = _entry()['columns']['employee_pay_type']['description']
    assert 'classification' in description
    assert 'annual_salary > 0' not in description
    # A source classification must not silently discard other compensation
    # populations for every unqualified salary question.
    assert not any(
        rule['id'] == 'annual_compensation'
        and rule['predicate'] == "employee_pay_type = 'Salary'"
        for rule in _entry()['rules']
    )
