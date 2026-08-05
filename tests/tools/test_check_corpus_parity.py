"""Tests for tools/check_corpus_parity.py (Phase 1: MCP owns the corpus).

The blueprint + knowledge corpora are authored on the MCP and versioned
INDEPENDENTLY: each has its own MANIFEST.sha256 and its own SHA sidecar
(BLUEPRINTS_SHA / KNOWLEDGE_SHA), where SHA = sha1(MANIFEST.sha256) — the same
value the export builders emit.

Coverage:
  * the content fingerprint matches each committed SHA sidecar;
  * the rendered manifest matches the on-disk MANIFEST.sha256 bytes;
  * the local drift + sidecar checks pass for the current committed corpus;
  * a content change without --write is flagged by the drift check;
  * an out-of-date SHA sidecar is flagged by the fingerprint check;
  * --write regenerates a throwaway corpus so the check then passes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_PATH = _REPO_ROOT / "tools" / "check_corpus_parity.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("check_corpus_parity", _TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before exec: the tool defines a @dataclass, whose processing looks
    # the module up in sys.modules by name (sys.modules[cls.__module__]).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


parity = _load_tool()


def _throwaway_corpus(tmp_path: Path, name: str = "blueprints"):
    """A Corpus pointing at a fresh tmp dir with one seed entry, no sidecars yet."""
    data_dir = tmp_path / name
    data_dir.mkdir()
    (data_dir / "x.yaml").write_text("id: x\nintent: test\n", encoding="utf-8")
    return parity.Corpus(
        name=name,
        data_dir=data_dir,
        manifest_path=data_dir / "MANIFEST.sha256",
        sha_path=data_dir / f"{name.upper()}_SHA",
    )


# ===========================================================================
# Content fingerprint derivation against the committed corpora
# ===========================================================================

class TestContentFingerprint:
    def test_fingerprint_matches_each_sidecar(self):
        for corpus in parity.CORPORA:
            recorded = corpus.sha_path.read_text(encoding="utf-8").strip()
            assert parity._live_fingerprint(corpus) == recorded

    def test_render_matches_manifest_file_bytes(self):
        for corpus in parity.CORPORA:
            rendered = parity._render_manifest(parity._compute_manifest(corpus))
            assert rendered == corpus.manifest_path.read_text(encoding="utf-8")

    def test_fingerprint_is_sha1_of_manifest_bytes(self):
        for corpus in parity.CORPORA:
            expected = hashlib.sha1(corpus.manifest_path.read_bytes()).hexdigest()
            assert parity._live_fingerprint(corpus) == expected

    def test_two_corpora_configured(self):
        assert {c.name for c in parity.CORPORA} == {"blueprints", "knowledge"}


# ===========================================================================
# Committed corpora pass both checks
# ===========================================================================

class TestCommittedCorporaPass:
    def test_local_drift_passes(self):
        for corpus in parity.CORPORA:
            assert parity.check_local_drift(corpus) == []

    def test_sidecar_fingerprint_passes(self):
        for corpus in parity.CORPORA:
            assert parity.check_sidecar_fingerprint(corpus) == []


# ===========================================================================
# --write then check passes; a content change without --write fails
# ===========================================================================

class TestWriteThenCheck:
    def test_write_generates_sidecars_and_check_passes(self, tmp_path):
        corpus = _throwaway_corpus(tmp_path)
        # Before --write: no manifest recorded.
        assert parity.check_local_drift(corpus)  # non-empty (missing manifest)
        # Emulate --write for this corpus.
        manifest = parity._compute_manifest(corpus)
        parity._write_manifest(corpus, manifest)
        parity._write_sha(corpus, parity._content_fingerprint(manifest))
        assert parity.check_local_drift(corpus) == []
        assert parity.check_sidecar_fingerprint(corpus) == []

    def test_content_change_without_write_fails_drift(self, tmp_path):
        corpus = _throwaway_corpus(tmp_path)
        parity._write_manifest(corpus, parity._compute_manifest(corpus))
        parity._write_sha(corpus, parity._live_fingerprint(corpus))
        # Tamper with the YAML but do NOT regenerate the manifest.
        (corpus.data_dir / "x.yaml").write_text(
            "id: x\nintent: test\n# tampered\n", encoding="utf-8"
        )
        errors = parity.check_local_drift(corpus)
        assert any("Content hash mismatch for x.yaml" in e for e in errors)
        assert any("run --write" in e for e in errors)

    def test_new_file_without_write_fails_drift(self, tmp_path):
        corpus = _throwaway_corpus(tmp_path)
        parity._write_manifest(corpus, parity._compute_manifest(corpus))
        parity._write_sha(corpus, parity._live_fingerprint(corpus))
        (corpus.data_dir / "y.yaml").write_text("id: y\nintent: new\n", encoding="utf-8")
        errors = parity.check_local_drift(corpus)
        assert any("not recorded in MANIFEST.sha256: y.yaml" in e for e in errors)

    def test_stale_sidecar_flagged(self, tmp_path):
        corpus = _throwaway_corpus(tmp_path)
        parity._write_manifest(corpus, parity._compute_manifest(corpus))
        parity._write_sha(corpus, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        errors = parity.check_sidecar_fingerprint(corpus)
        assert any("SHA sidecar out of date" in e for e in errors)
