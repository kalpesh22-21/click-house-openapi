"""Result caps are positive, applied centrally, and reserved from tenant overrides."""
import pytest
from pydantic import ValidationError

from app.clickhouse_client import readonly_settings
from app.config import Settings


def test_row_and_byte_caps_throw_instead_of_returning_partial_results():
    settings = Settings(_env_file=None, max_result_rows=25, max_result_bytes=1024)
    caps = readonly_settings(settings)
    assert caps['max_result_rows'] == 25
    assert caps['max_result_bytes'] == 1024
    assert caps['result_overflow_mode'] == 'throw'


@pytest.mark.parametrize('name', ['max_result_rows', 'max_result_bytes'])
@pytest.mark.parametrize('value', [0, -1])
def test_caps_cannot_be_disabled(name, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})


def test_tenant_mapping_cannot_override_byte_cap():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, clickhouse_tenant_settings={'max_result_bytes': 'jti'})
