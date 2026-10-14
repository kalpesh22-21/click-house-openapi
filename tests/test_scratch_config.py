"""Scratch configuration must agree with both authorization layers."""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.constants import SCRATCH_DATABASE
from app.service import _forbidden_out_of_scope_columns
from app.sqlparse.provenance import _SCRATCH_DB


def test_default_scratch_database_matches_authorization(monkeypatch):
    monkeypatch.delenv("SCRATCH_DATABASE", raising=False)
    settings = Settings(_env_file=None)
    assert settings.scratch_database == _SCRATCH_DB == SCRATCH_DATABASE == "dbpcm_scratch"


@pytest.mark.parametrize("database", ["scratch", "sandbox"])
def test_mismatched_scratch_override_is_rejected(database):
    with pytest.raises(ValidationError, match="SCRATCH_DATABASE must be"):
        Settings(_env_file=None, scratch_database=database)


def test_only_current_scratch_database_is_exempt_from_column_scope(monkeypatch):
    monkeypatch.setattr("app.service.get_semantic_catalog", lambda: {})
    uses = {
        ("dbpcm_scratch.s_session_values", "value"),
        ("scratch.s_session_values", "value"),
    }
    assert _forbidden_out_of_scope_columns(uses, frozenset()) == {
        "scratch.s_session_values.value"
    }
