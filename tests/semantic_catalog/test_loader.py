"""Tests for app/semantic_catalog/loader.py (D83/D84 copy-in loader + CATALOG_SHA)."""

from __future__ import annotations

import pytest

from app.semantic_catalog import loader


@pytest.fixture(autouse=True)
def _reset_cache():
    loader.invalidate_semantic_catalog_cache()
    yield
    loader.invalidate_semantic_catalog_cache()


class TestLoadSemanticCatalog:
    def test_loads_all_copied_in_yaml_files(self):
        catalog = loader.load_semantic_catalog()
        assert "dbpcm_warehouse.employee" in catalog
        assert "dbpcm_warehouse.payroll" in catalog

    def test_entry_has_rich_fields_not_just_col_type(self):
        catalog = loader.load_semantic_catalog()
        employee = catalog["dbpcm_warehouse.employee"]
        assert "grain" in employee
        assert "rules" in employee
        assert "ambiguities" in employee
        assert isinstance(employee["columns"]["employee_code"], dict)
        assert "description" in employee["columns"]["employee_code"]

    def test_custom_schema_dir(self, tmp_path):
        (tmp_path / "t.yaml").write_text(
            "table: t\ncolumns:\n  a: { type: String }\n", encoding="utf-8"
        )
        catalog = loader.load_semantic_catalog(tmp_path)
        assert catalog == {
            "dbpcm_warehouse.t": {
                "table": "t",
                "columns": {"a": {"type": "String"}},
                "database": "dbpcm_warehouse",
            }
        }

    def test_file_without_columns_block_is_skipped(self, tmp_path):
        (tmp_path / "notes.yaml").write_text("table: notes\n", encoding="utf-8")
        catalog = loader.load_semantic_catalog(tmp_path)
        assert catalog == {}


class TestGetSemanticCatalog:
    def test_caches_between_calls(self):
        first = loader.get_semantic_catalog()
        second = loader.get_semantic_catalog()
        assert first is second

    def test_invalidate_forces_reload(self):
        first = loader.get_semantic_catalog()
        loader.invalidate_semantic_catalog_cache()
        second = loader.get_semantic_catalog()
        assert first is not second
        assert first == second


class TestCatalogSha:
    def test_get_catalog_sha_returns_sidecar_contents(self):
        sha = loader.get_catalog_sha()
        # Reads the actual CATALOG_SHA sidecar (refreshed on every catalog re-copy),
        # so assert against the file's live content — never a hardcoded value.
        expected = loader._CATALOG_SHA_PATH.read_text().strip()
        assert sha == expected
        # A git commit SHA-1: 40 lowercase hex characters, no surrounding whitespace.
        assert len(sha) == 40
        assert sha == sha.strip()
        assert all(c in "0123456789abcdef" for c in sha)
