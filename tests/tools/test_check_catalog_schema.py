from copy import deepcopy

from app.semantic_catalog.loader import load_semantic_catalog
from tools.check_catalog_schema import check_schema

TABLE = 'dbpcm_warehouse.personnel_action_form_changes'


def fixture():
    catalog = load_semantic_catalog()
    entry = catalog[TABLE]
    physical = {
        TABLE: {name: column['type'] for name, column in entry['columns'].items()},
        'dbpcm_warehouse.employee': {'employee_code': 'String'},
    }
    return {TABLE: deepcopy(entry)}, physical


def test_paf_schema_matches():
    catalog, physical = fixture()
    assert check_schema(catalog, physical) == []


def test_paf_missing_field_id_reports_every_reference():
    catalog, physical = fixture()
    del physical[TABLE]['field_id']
    errors = check_schema(catalog, physical)
    assert any('.grain:' in e for e in errors)
    assert sum('.resolve_via:' in e for e in errors) == 4
    assert sum('.predicate:' in e for e in errors) == 4
    assert all('field_id' in e for e in errors)


def test_description_and_join_targets():
    catalog, physical = fixture()
    del physical[TABLE]['field_label']
    del physical['dbpcm_warehouse.employee']['employee_code']
    errors = check_schema(catalog, physical)
    assert any('.description_col:' in e for e in errors)
    assert any('.joins:' in e for e in errors)


def test_bad_resolver_cannot_silently_pass():
    catalog, physical = fixture()
    catalog[TABLE]['rules'][0]['resolve_via'] = 'resolveValues(missing, \'pay\')'
    assert any('missing' in e for e in check_schema(catalog, physical))
    catalog[TABLE]['rules'][0]['resolve_via'] = 'nonsense'
    assert any('invalid resolveValues target' in e for e in check_schema(catalog, physical))
