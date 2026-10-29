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


class TestValidateRlsProjection:
    """Fail-closed RLS guard tests (A-M1)."""

    def test_missing_mcp_projection_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            loader,
            "_REQUIRED_RLS_HIDDEN_COLUMNS",
            {f"{loader.DEFAULT_DATABASE}.test_rls": frozenset({"client_code", "proc_center"})},
        )
        (tmp_path / "test_rls.yaml").write_text(
            "table: test_rls\ncolumns:\n  a: { type: String }\n", encoding="utf-8"
        )
        catalog = loader.load_semantic_catalog(tmp_path)
        with pytest.raises(loader.CatalogValidationError) as exc_info:
            loader._validate_rls_projection(catalog)
        assert "empty/missing mcp_projection.hidden_columns" in str(exc_info.value)

    def test_incomplete_hidden_columns_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            loader,
            "_REQUIRED_RLS_HIDDEN_COLUMNS",
            {f"{loader.DEFAULT_DATABASE}.test_rls": frozenset({"client_code", "proc_center"})},
        )
        (tmp_path / "test_rls.yaml").write_text(
            "table: test_rls\ncolumns:\n  a: { type: String }\n"
            "mcp_projection:\n  hidden_columns: [client_code]\n",
            encoding="utf-8",
        )
        catalog = loader.load_semantic_catalog(tmp_path)
        with pytest.raises(loader.CatalogValidationError) as exc_info:
            loader._validate_rls_projection(catalog)
        assert "proc_center" in str(exc_info.value)

    def test_real_catalog_passes_validation(self):
        """Every required RLS table is present and hides its enforced columns."""
        catalog = loader.get_semantic_catalog()
        assert len(catalog) >= 6
        for key in loader._REQUIRED_RLS_HIDDEN_COLUMNS:
            assert key in catalog


class TestBackendScopedMachinery:
    """Registry + helper contract for the backend-scoped values / rule-backends
    mechanism (the sibling-key convention documented in the loader docstring).

    The full per-surface behavior (substitution / exclusion / no-leak /
    byte-identity / real-catalog canary) lives in
    tests/semantic_catalog/test_backend_scoped_fields.py; this class pins the
    loader-level registry and the loud error paths, plus the loader's
    unknown-key tolerance for the mechanism keys.
    """

    def test_registries(self):
        assert loader.BACKEND_SCOPED_VALUE_FIELDS == ("values",)
        assert loader.KNOWN_BACKENDS == ("clickhouse", "trino")
        assert set(loader.BACKEND_SCOPED_VALUE_KEYS) == {
            "clickhouse_values",
            "trino_values",
        }

    def test_loader_passes_mechanism_keys_through_untouched(self, tmp_path):
        """Unknown-key tolerance: scoped values keys and rule ``backends`` ride
        the raw entry verbatim; interpretation happens only at serialization."""
        (tmp_path / "t.yaml").write_text(
            "table: t\n"
            "columns:\n"
            "  a:\n"
            "    type: String\n"
            "    values: [A, B]\n"
            "    trino_values: [X]\n"
            "rules:\n"
            "- id: r\n"
            "  predicate: a IS NOT NULL\n"
            "  backends: [clickhouse]\n",
            encoding="utf-8",
        )
        entry = loader.load_semantic_catalog(tmp_path)["dbpcm_warehouse.t"]
        assert entry["columns"]["a"]["values"] == ["A", "B"]
        assert entry["columns"]["a"]["trino_values"] == ["X"]
        assert entry["rules"][0]["backends"] == ["clickhouse"]

    def test_scoped_field_helper_plain_default(self):
        mapping = {"values": ["A"]}
        assert loader.scoped_field_for_backend(
            mapping, "values", trino_enabled=True
        ) == ["A"]
        assert loader.scoped_field_for_backend(
            mapping, "values", trino_enabled=False
        ) == ["A"]

    def test_rule_applies_unknown_backend_is_loud(self):
        with pytest.raises(ValueError, match="db2"):
            loader.rule_applies_to_backend(
                {"id": "r", "backends": ["db2"]}, trino_enabled=True
            )

    def test_rule_applies_mis_shaped_is_loud(self):
        with pytest.raises(ValueError, match="'backends'"):
            loader.rule_applies_to_backend(
                {"id": "r", "backends": "trino"}, trino_enabled=True
            )


class TestAccrualBalancesYaml:
    """Loader/projection contract for the view-first time_off_balance table
    (authored with the four-template Trino view conversion; no ClickHouse ETL
    counterpart exists yet)."""

    def test_loads_with_mysql_source_and_grain(self):
        catalog = loader.load_semantic_catalog()
        entry = catalog["dbpcm_warehouse.time_off_balance"]
        assert entry["source"] == "mysql"
        assert entry["database"] == "dbpcm_warehouse"
        assert entry["table"] == "time_off_balance"
        assert entry["grain"] == []
        assert entry["grain_verifiable"] is False
        assert entry["primary_key"] == []
        # 17 catalogued business columns (the view additionally emits the
        # hidden client_code/proc_center identity pins).
        assert len(set(entry["columns"]) - set(entry["mcp_projection"]["hidden_columns"])) == 17

    def test_hidden_identity_pins_and_row_scope(self):
        catalog = loader.load_semantic_catalog()
        entry = catalog["dbpcm_warehouse.time_off_balance"]
        hidden = set(entry["mcp_projection"]["hidden_columns"])
        assert {"client_code", "proc_center"} <= hidden
        # Declarations describe source types; model-facing schemas hide pins.
        from app.semantic_catalog.overlay import build_table_schema_response
        response = build_table_schema_response(
            database=entry["database"], table=entry["table"], catalog=catalog,
            introspected_columns=[{"name": name, "type": info["type"], "comment": ""} for name, info in entry["columns"].items()],
            scope=None,
        )
        assert hidden.isdisjoint(c["name"] for c in response["columns"])
        assert set(entry["row_scope"]["implicit_filters"]) >= {"client_code", "proc_center"}

    def test_time_off_request_yaml_rls_projection_matches_house_pattern(self):
        """Source identity declarations must remain hidden on the public schema."""
        catalog = loader.load_semantic_catalog()
        entry = catalog["dbpcm_warehouse.time_off_request"]
        hidden = set(entry["mcp_projection"]["hidden_columns"])
        assert {"client_code", "proc_center"} <= hidden
        assert set(entry["row_scope"]["implicit_filters"]) >= {"client_code", "proc_center"}
        assert entry["source"] == "mysql"

    def test_accrual_event_and_balance_guidance_survives_public_export(self):
        from app.semantic_catalog.export import build_catalog_export_from

        catalog = loader.load_semantic_catalog()
        for trino_enabled in (False, True):
            exported = build_catalog_export_from(catalog, "test", trino_enabled=trino_enabled)
            # Check the public payload, not just the authored YAML: both execution
            # routes must teach the same distinction and keep optimizer keys private.
            import json

            public = json.dumps(exported)
            assert "all types of accrual events" in public
            assert "treat approved time-off requests as assumed taken" in public
            assert "payroll-paid hours" in public
            assert "trino_join_keys" not in public
        events = catalog["dbpcm_warehouse.time_off_request"]
        taken = next(a for a in events["ambiguities"] if a["term"] == "time_off_taken")
        assert "Treat approved time-off request hours as taken" in taken["default"]
        approved = next(r for r in events["rules"] if r["id"] == "approved_events")
        assert "taken" not in approved["applies_when"]
