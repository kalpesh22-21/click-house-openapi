"""Tests for app/corpus/export.py (Phase 1 corpus export serializers)."""

from __future__ import annotations

import json

import pytest

from app.corpus import export, loader


@pytest.fixture(autouse=True)
def _reset_cache():
    loader.invalidate_corpus_cache()
    yield
    loader.invalidate_corpus_cache()


class TestBuildBlueprintsExport:
    def test_top_level_shape(self):
        result = export.build_blueprints_export()
        assert set(result.keys()) == {"blueprints_sha", "blueprints"}
        assert isinstance(result["blueprints"], dict)

    def test_sha_matches_sidecar(self):
        result = export.build_blueprints_export()
        assert result["blueprints_sha"] == loader.get_blueprints_sha()

    def test_all_blueprints_present(self):
        result = export.build_blueprints_export()
        assert set(result["blueprints"].keys()) == set(loader.get_blueprints().keys())

    def test_entries_verbatim_plus_injected_provenance(self):
        result = export.build_blueprints_export()
        bp = result["blueprints"]["bp-overtime-by-department"]
        source_bp = loader.get_blueprints()["bp-overtime-by-department"]
        # Every authored field is carried verbatim...
        for key, value in source_bp.items():
            assert bp[key] == value
        # ...and source/verified are injected on top.
        assert bp["source"] == "mcp"
        assert bp["verified"] is True

    def test_source_and_verified_injected_on_every_entry(self):
        blueprints = export.build_blueprints_export()["blueprints"]
        assert blueprints  # non-empty
        for entry in blueprints.values():
            assert entry["source"] == "mcp"
            assert entry["verified"] is True

    def test_result_is_json_serializable(self):
        result = export.build_blueprints_export()
        assert json.loads(json.dumps(result)) == result

    def test_sha_deterministic_across_calls(self):
        a = export.build_blueprints_export()
        b = export.build_blueprints_export()
        assert a["blueprints_sha"] == b["blueprints_sha"]
        assert a == b

    def test_export_is_deep_copied_from_cache(self):
        result = export.build_blueprints_export()
        result["blueprints"]["bp-overtime-by-department"]["intent"] = "MUTATED"
        result["blueprints"]["bp-overtime-by-department"]["source"] = "TAMPERED"
        # Mutating the export must not corrupt the process-level corpus cache,
        # nor leak the injected keys back into it.
        cached = loader.get_blueprints()["bp-overtime-by-department"]
        assert cached["intent"] != "MUTATED"
        assert "source" not in cached
        assert "verified" not in cached


class TestBuildKnowledgeExport:
    def test_top_level_shape(self):
        result = export.build_knowledge_export()
        assert set(result.keys()) == {"knowledge_sha", "knowledge"}
        assert isinstance(result["knowledge"], dict)

    def test_sha_matches_sidecar(self):
        result = export.build_knowledge_export()
        assert result["knowledge_sha"] == loader.get_knowledge_sha()

    def test_all_knowledge_present(self):
        result = export.build_knowledge_export()
        assert set(result["knowledge"].keys()) == set(loader.get_knowledge().keys())

    def test_entries_verbatim_plus_injected_provenance(self):
        result = export.build_knowledge_export()
        kn = result["knowledge"]["kn-overtime-multiplier"]
        source_kn = loader.get_knowledge()["kn-overtime-multiplier"]
        for key, value in source_kn.items():
            assert kn[key] == value
        assert kn["source"] == "mcp"
        assert kn["verified"] is True

    def test_source_and_verified_injected_on_every_entry(self):
        knowledge = export.build_knowledge_export()["knowledge"]
        assert knowledge
        for entry in knowledge.values():
            assert entry["source"] == "mcp"
            assert entry["verified"] is True

    def test_result_is_json_serializable(self):
        result = export.build_knowledge_export()
        assert json.loads(json.dumps(result)) == result

    def test_export_is_deep_copied_from_cache(self):
        result = export.build_knowledge_export()
        result["knowledge"]["kn-overtime-multiplier"]["text"] = "MUTATED"
        cached = loader.get_knowledge()["kn-overtime-multiplier"]
        assert cached["text"] != "MUTATED"
        assert "source" not in cached


class TestBuildExportFrom:
    def test_blueprints_wraps_given_corpus_and_sha(self):
        fake = {"bp-x": {"id": "bp-x", "intent": "test"}}
        result = export.build_blueprints_export_from(fake, "deadbeef")
        assert result == {
            "blueprints_sha": "deadbeef",
            "blueprints": {
                "bp-x": {
                    "id": "bp-x",
                    "intent": "test",
                    "source": "mcp",
                    "verified": True,
                }
            },
        }

    def test_knowledge_wraps_given_corpus_and_sha(self):
        fake = {"kn-x": {"id": "kn-x", "title": "t"}}
        result = export.build_knowledge_export_from(fake, "cafe")
        assert result == {
            "knowledge_sha": "cafe",
            "knowledge": {
                "kn-x": {
                    "id": "kn-x",
                    "title": "t",
                    "source": "mcp",
                    "verified": True,
                }
            },
        }

    def test_does_not_alias_or_mutate_input(self):
        fake = {"bp-x": {"id": "bp-x", "intent": "test"}}
        result = export.build_blueprints_export_from(fake, "sha")
        result["blueprints"]["bp-x"]["intent"] = "changed"
        # Neither the value nor the injected keys leak back into the caller's dict.
        assert fake["bp-x"]["intent"] == "test"
        assert "source" not in fake["bp-x"]
        assert "verified" not in fake["bp-x"]
