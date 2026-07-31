"""Tests for tools/check_catalog_parity.py (post-Wave-1a: MCP is source of truth).

The catalog is authored on the MCP; the data-analysis-agent DERIVES
`tests/fixtures/catalog_export.json` from it. `CATALOG_SHA` is a deterministic
CONTENT fingerprint (`sha1(MANIFEST.sha256)`) — the same value
`build_catalog_export()` emits as `catalog_sha`.

Coverage:
  * the content-fingerprint derivation matches the committed CATALOG_SHA;
  * the fixture-staleness check passes for an in-sync fixture and flags a drifted
    one, using the fingerprint (no git history, no databaseSchemaDocs/ dir);
  * the local tamper/drift check still works and its message reflects the new
    MCP-owns-the-catalog workflow.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_PATH = _REPO_ROOT / "tools" / "check_catalog_parity.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("check_catalog_parity", _TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


parity = _load_tool()


def _write_fixture(path: Path, catalog_sha) -> Path:
    """Write a minimal agent catalog_export.json carrying the given catalog_sha."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"catalog_sha": catalog_sha, "catalog": {}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ===========================================================================
# Content fingerprint derivation
# ===========================================================================

class TestContentFingerprint:

    def test_fingerprint_of_current_manifest_matches_catalog_sha_sidecar(self):
        recorded = parity._CATALOG_SHA_PATH.read_text(encoding="utf-8").strip()
        assert parity._live_fingerprint() == recorded

    def test_render_matches_manifest_file_bytes(self):
        rendered = parity._render_manifest(parity._compute_manifest())
        assert rendered == parity._MANIFEST_PATH.read_text(encoding="utf-8")

    def test_fingerprint_is_sha1_of_manifest_bytes(self):
        expected = hashlib.sha1(parity._MANIFEST_PATH.read_bytes()).hexdigest()
        assert parity._live_fingerprint() == expected


# ===========================================================================
# Agent-fixture staleness check (replaces the former cross-repo mode)
# ===========================================================================

class TestFixtureStaleness:

    def test_in_sync_fixture_has_no_errors(self, tmp_path):
        fixture = _write_fixture(
            tmp_path / "tests" / "fixtures" / "catalog_export.json",
            parity._live_fingerprint(),
        )
        assert parity.check_fixture_staleness(fixture) == []

    def test_stale_fixture_flagged_with_fingerprints(self, tmp_path):
        fixture = _write_fixture(
            tmp_path / "tests" / "fixtures" / "catalog_export.json",
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        )
        errors = parity.check_fixture_staleness(fixture)
        assert any("Agent catalog-export fixture is stale" in e for e in errors)
        assert any("live-mcp-fingerprint=" in e for e in errors)

    def test_missing_fixture_reported(self, tmp_path):
        errors = parity.check_fixture_staleness(tmp_path / "does_not_exist.json")
        assert len(errors) == 1
        assert "fixture not found" in errors[0]

    def test_fixture_without_catalog_sha_reported(self, tmp_path):
        path = tmp_path / "catalog_export.json"
        path.write_text(json.dumps({"catalog": {}}), encoding="utf-8")
        errors = parity.check_fixture_staleness(path)
        assert any("no 'catalog_sha' field" in e for e in errors)

    def test_malformed_json_reported(self, tmp_path):
        path = tmp_path / "catalog_export.json"
        path.write_text("{ not json", encoding="utf-8")
        errors = parity.check_fixture_staleness(path)
        assert any("Could not read agent catalog-export fixture" in e for e in errors)

    def test_no_git_history_dependency(self, tmp_path, monkeypatch):
        """The check must NOT shell out to git — it is a pure fingerprint compare.

        Regression guard against the former `git log -1` cross-repo implementation.
        """
        def _boom(*args, **kwargs):  # pragma: no cover - only fires on regression
            raise AssertionError("check_fixture_staleness must not invoke subprocess/git")

        monkeypatch.setattr(subprocess, "run", _boom)
        fixture = _write_fixture(
            tmp_path / "catalog_export.json", parity._live_fingerprint()
        )
        assert parity.check_fixture_staleness(fixture) == []


# ===========================================================================
# Fixture-path resolution (--fixture / --agent-repo / env)
# ===========================================================================

class TestResolveFixturePath:

    def _args(self, fixture=None, agent_repo=None):
        import argparse

        return argparse.Namespace(fixture=fixture, agent_repo=agent_repo)

    def test_explicit_fixture_wins(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENT_CATALOG_FIXTURE", raising=False)
        monkeypatch.delenv("DATA_ANALYSIS_AGENT_REPO", raising=False)
        p = tmp_path / "f.json"
        assert parity._resolve_fixture_path(self._args(fixture=p)) == p

    def test_agent_repo_resolves_default_relpath(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENT_CATALOG_FIXTURE", raising=False)
        monkeypatch.delenv("DATA_ANALYSIS_AGENT_REPO", raising=False)
        resolved = parity._resolve_fixture_path(self._args(agent_repo=tmp_path))
        assert resolved == tmp_path / "tests" / "fixtures" / "catalog_export.json"

    def test_env_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_CATALOG_FIXTURE", str(tmp_path / "env.json"))
        monkeypatch.delenv("DATA_ANALYSIS_AGENT_REPO", raising=False)
        assert parity._resolve_fixture_path(self._args()) == tmp_path / "env.json"

    def test_none_when_nothing_provided(self, monkeypatch):
        monkeypatch.delenv("AGENT_CATALOG_FIXTURE", raising=False)
        monkeypatch.delenv("DATA_ANALYSIS_AGENT_REPO", raising=False)
        assert parity._resolve_fixture_path(self._args()) is None


# ===========================================================================
# Local drift check remains intact
# ===========================================================================

class TestLocalDriftUnchanged:

    def test_local_drift_passes_for_current_copy(self):
        assert parity.check_local_drift() == []

    def test_drift_message_reflects_mcp_ownership(self, tmp_path, monkeypatch):
        """A YAML edit without --write is flagged with the new run-`--write` guidance,
        NOT the old 're-mirror from data-analysis-agent' text."""
        # Point the tool at a throwaway catalog dir so we can mutate a file safely.
        cat_dir = tmp_path / "data"
        cat_dir.mkdir()
        (cat_dir / "t.yaml").write_text("table: t\ncolumns:\n  a: {type: String}\n", encoding="utf-8")
        monkeypatch.setattr(parity, "_CATALOG_DIR", cat_dir)
        monkeypatch.setattr(parity, "_MANIFEST_PATH", cat_dir / "MANIFEST.sha256")
        # Record the manifest, then tamper with the YAML.
        parity._write_manifest(parity._compute_manifest())
        (cat_dir / "t.yaml").write_text(
            "table: t\ncolumns:\n  a: {type: String}\n# tampered\n", encoding="utf-8"
        )
        errors = parity.check_local_drift()
        assert any("Content hash mismatch for t.yaml" in e for e in errors)
        joined = " ".join(errors)
        assert "run --write" in joined
        assert "re-mirror from data-analysis-agent" not in joined
