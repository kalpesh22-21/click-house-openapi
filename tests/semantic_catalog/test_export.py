"""Tests for app/semantic_catalog/export.py (Wave-0 catalog export serializer)."""

from __future__ import annotations

import json

import pytest

from app.semantic_catalog import export, loader


@pytest.fixture(autouse=True)
def _reset_cache():
    loader.invalidate_semantic_catalog_cache()
    yield
    loader.invalidate_semantic_catalog_cache()


class TestBuildCatalogExport:
    def test_top_level_shape(self):
        result = export.build_catalog_export()
        assert set(result.keys()) == {"catalog_sha", "catalog"}
        assert isinstance(result["catalog"], dict)

    def test_sha_matches_sidecar(self):
        result = export.build_catalog_export()
        assert result["catalog_sha"] == loader.get_catalog_sha()

    def test_all_catalogued_tables_present(self):
        result = export.build_catalog_export()
        catalog = result["catalog"]
        # Every table the loader knows about must be in the export (scope-independent).
        assert set(catalog.keys()) == set(loader.get_semantic_catalog().keys())
        assert "dbpcm_warehouse.employee" in catalog
        assert "dbpcm_warehouse.payroll" in catalog

    def test_columns_carry_declared_types(self):
        result = export.build_catalog_export()
        employee = result["catalog"]["dbpcm_warehouse.employee"]
        columns = employee["columns"]
        assert isinstance(columns, dict)
        # Column type is the YAML-declared catalog type, not a live DB type.
        assert columns["EmployeeCode"]["type"] == "String"
        assert columns["ClientCode"]["type"] == "Nullable(String)"

    def test_rich_overlay_fields_preserved_verbatim(self):
        result = export.build_catalog_export()
        payroll = result["catalog"]["dbpcm_warehouse.payroll"]
        # The three agent projections must all be reconstructable from the entry:
        #  - full overlay: grain / rules / ambiguities / measures present as-authored
        assert "grain" in payroll
        assert "rules" in payroll
        assert "ambiguities" in payroll
        assert "measures" in payroll
        #  - description-col linkage carried on the column block (not padded elsewhere)
        assert payroll["columns"]["TypeCode"]["description_col"] == "TypeCodeDescription"

    def test_result_is_json_serializable(self):
        result = export.build_catalog_export()
        # Round-trips through JSON without custom encoders (pure data).
        reloaded = json.loads(json.dumps(result))
        assert reloaded == result

    def test_export_is_deep_copied_from_cache(self):
        result = export.build_catalog_export()
        result["catalog"]["dbpcm_warehouse.employee"]["description"] = "MUTATED"
        # Mutating the export must not corrupt the process-level catalog cache.
        assert (
            loader.get_semantic_catalog()["dbpcm_warehouse.employee"]["description"]
            != "MUTATED"
        )


class TestBuildCatalogExportFrom:
    def test_wraps_given_catalog_and_sha(self):
        fake = {"dbpcm_warehouse.t": {"table": "t", "columns": {"a": {"type": "String"}}}}
        result = export.build_catalog_export_from(fake, "deadbeef")
        assert result == {
            "catalog_sha": "deadbeef",
            "catalog": {
                "dbpcm_warehouse.t": {"table": "t", "columns": {"a": {"type": "String"}}}
            },
        }

    def test_does_not_alias_input(self):
        fake = {"dbpcm_warehouse.t": {"table": "t", "columns": {}}}
        result = export.build_catalog_export_from(fake, "sha")
        result["catalog"]["dbpcm_warehouse.t"]["table"] = "changed"
        assert fake["dbpcm_warehouse.t"]["table"] == "t"
