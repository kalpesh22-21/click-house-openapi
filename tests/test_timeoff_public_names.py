"""Catalog, private templates and physical ClickHouse relations share canonical names."""
import pytest
from app.semantic_catalog.loader import load_semantic_catalog
from app.trino_metadata import physical_target
from app.config import Settings


@pytest.mark.parametrize('table', ['time_off_request', 'time_off_balance'])
def test_timeoff_canonical_names(table):
    catalog = load_semantic_catalog()
    entry = catalog['dbpcm_warehouse.' + table]
    assert entry['table'] == table
    settings = Settings(_env_file=None)
    assert physical_target({**entry, 'source':'clickhouse'}, 'unused', settings)[2] == table
    for old in ['accrual_events', 'accrual_balances', 'timeoff_requests', 'timeoff_balances']:
        assert 'dbpcm_warehouse.'+old not in catalog
