"""Tests for app/corpus/loader.py (Phase 1 corpus loader + SHA sidecars)."""

from __future__ import annotations

import pytest

from app.corpus import loader


@pytest.fixture(autouse=True)
def _reset_cache():
    loader.invalidate_corpus_cache()
    yield
    loader.invalidate_corpus_cache()


# All ten seed blueprint ids and three knowledge ids migrated from the source
# corpus fixtures — asserted explicitly so a dropped file fails the suite.
_BLUEPRINT_IDS = {
    "bp-overtime-by-department",
    "bp-active-headcount-by-department",
    "bp-average-salary-by-department",
    "bp-total-earnings-by-department",
    "bp-departments-above-company-average-salary",
    "bp-earnings-by-department-via-scratch-join",
    "bp-hires-per-month",
    "bp-hires-in-range",
    "bp-hires-projection",
    "bp-compare-employee-check-detail-two-periods",
}
_KNOWLEDGE_IDS = {
    "kn-overtime-multiplier",
    "kn-pay-period-definition",
    "kn-register-type",
}


class TestLoadBlueprints:
    def test_loads_all_migrated_ids(self):
        blueprints = loader.load_blueprints()
        assert set(blueprints.keys()) == _BLUEPRINT_IDS

    def test_entry_carries_authored_fields_verbatim(self):
        bp = loader.load_blueprints()["bp-overtime-by-department"]
        assert bp["intent"].startswith("Earnings by department")
        assert bp["status"] == "validated"
        assert bp["drift_status"] == "clean"
        assert bp["result_grain"] == ["Department"]
        assert "dbpcm_warehouse.payroll.amount" in bp["uses"]
        # pay type is now a slot, not hard-coded (warehouse has no overtime code).
        assert "dbpcm_warehouse.payroll.type_code" in bp["uses"]
        assert bp["sql_template"].startswith("SELECT e.department_name")
        # source/verified are NOT stored in the YAML — injected only at export.
        assert "source" not in bp
        assert "verified" not in bp

    def test_composes_blueprint_preserved(self):
        bp = loader.load_blueprints()["bp-departments-above-company-average-salary"]
        assert isinstance(bp["composes"], list)
        assert bp["composes"][0]["order"] == 0
        assert bp["composes"][1]["consumes"] == {"company_avg": "$0.company_avg"}

    def test_custom_data_dir(self, tmp_path):
        (tmp_path / "bp-x.yaml").write_text(
            "id: bp-x\nintent: test\n", encoding="utf-8"
        )
        blueprints = loader.load_blueprints(tmp_path)
        assert blueprints == {"bp-x": {"id": "bp-x", "intent": "test"}}


class TestLoadKnowledge:
    def test_loads_all_migrated_ids(self):
        knowledge = loader.load_knowledge()
        assert set(knowledge.keys()) == _KNOWLEDGE_IDS

    def test_entry_carries_authored_fields_verbatim(self):
        kn = loader.load_knowledge()["kn-overtime-multiplier"]
        assert kn["title"] == "Overtime pay rule"
        assert kn["doc_id"] == "hr-policy-payroll"
        assert kn["status"] == "validated"
        assert kn["text"].startswith("Overtime is paid at 1.5 times")
        assert "source" not in kn
        assert "verified" not in kn


class TestDuplicateIdRejection:
    def test_duplicate_id_fails_loud(self, tmp_path):
        (tmp_path / "a.yaml").write_text("id: dup\nintent: a\n", encoding="utf-8")
        (tmp_path / "b.yaml").write_text("id: dup\nintent: b\n", encoding="utf-8")
        with pytest.raises(loader.CorpusValidationError, match="Duplicate corpus id"):
            loader.load_blueprints(tmp_path)

    def test_missing_id_fails_loud(self, tmp_path):
        (tmp_path / "a.yaml").write_text("intent: no id here\n", encoding="utf-8")
        with pytest.raises(loader.CorpusValidationError, match="no top-level 'id'"):
            loader.load_knowledge(tmp_path)

    def test_non_string_id_fails_loud(self, tmp_path):
        (tmp_path / "a.yaml").write_text("id: 123\nintent: numeric id\n", encoding="utf-8")
        with pytest.raises(loader.CorpusValidationError, match="non-string 'id'"):
            loader.load_blueprints(tmp_path)


class TestMalformedRootRejection:
    def test_list_root_fails_loud(self, tmp_path):
        (tmp_path / "a.yaml").write_text("- id: x\n  intent: t\n", encoding="utf-8")
        with pytest.raises(loader.CorpusValidationError, match="must be a YAML mapping"):
            loader.load_blueprints(tmp_path)

    def test_scalar_root_fails_loud(self, tmp_path):
        (tmp_path / "a.yaml").write_text("just a string\n", encoding="utf-8")
        with pytest.raises(loader.CorpusValidationError, match="must be a YAML mapping"):
            loader.load_knowledge(tmp_path)

    def test_empty_file_fails_loud(self, tmp_path):
        # An empty file parses to None — a non-mapping root, rejected loud.
        (tmp_path / "a.yaml").write_text("", encoding="utf-8")
        with pytest.raises(loader.CorpusValidationError, match="must be a YAML mapping"):
            loader.load_blueprints(tmp_path)


class TestReservedKeyRejection:
    def test_stored_source_fails_loud(self, tmp_path):
        (tmp_path / "a.yaml").write_text(
            "id: bp-x\nintent: t\nsource: mcp\n", encoding="utf-8"
        )
        with pytest.raises(loader.CorpusValidationError, match="reserved key"):
            loader.load_blueprints(tmp_path)

    def test_stored_verified_fails_loud(self, tmp_path):
        (tmp_path / "a.yaml").write_text(
            "id: kn-x\ntitle: t\nverified: true\n", encoding="utf-8"
        )
        with pytest.raises(loader.CorpusValidationError, match="reserved key"):
            loader.load_knowledge(tmp_path)

    def test_error_names_the_offending_keys(self, tmp_path):
        (tmp_path / "a.yaml").write_text(
            "id: bp-x\nintent: t\nsource: mcp\nverified: false\n", encoding="utf-8"
        )
        with pytest.raises(loader.CorpusValidationError) as exc:
            loader.load_blueprints(tmp_path)
        msg = str(exc.value)
        assert "source" in msg and "verified" in msg
        assert "a.yaml" in msg


class TestCaching:
    def test_blueprints_cached_between_calls(self):
        first = loader.get_blueprints()
        second = loader.get_blueprints()
        assert first is second

    def test_knowledge_cached_between_calls(self):
        first = loader.get_knowledge()
        second = loader.get_knowledge()
        assert first is second

    def test_invalidate_forces_reload(self):
        first_bp = loader.get_blueprints()
        first_kn = loader.get_knowledge()
        loader.invalidate_corpus_cache()
        second_bp = loader.get_blueprints()
        second_kn = loader.get_knowledge()
        assert first_bp is not second_bp
        assert first_kn is not second_kn
        assert first_bp == second_bp
        assert first_kn == second_kn


class TestShaSidecars:
    def test_blueprints_sha_returns_sidecar_contents(self):
        sha = loader.get_blueprints_sha()
        expected = loader._BLUEPRINTS_SHA_PATH.read_text().strip()
        assert sha == expected
        # sha1 content fingerprint: 40 lowercase hex chars, no surrounding space.
        assert len(sha) == 40
        assert sha == sha.strip()
        assert all(c in "0123456789abcdef" for c in sha)

    def test_knowledge_sha_returns_sidecar_contents(self):
        sha = loader.get_knowledge_sha()
        expected = loader._KNOWLEDGE_SHA_PATH.read_text().strip()
        assert sha == expected
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

    def test_blueprints_and_knowledge_sha_differ(self):
        # Independently versioned corpora — different content, different fingerprint.
        assert loader.get_blueprints_sha() != loader.get_knowledge_sha()
