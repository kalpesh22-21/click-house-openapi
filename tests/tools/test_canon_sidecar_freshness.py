"""Canon freshness gate — the committed sidecars must match the LIVE data dirs.

This repo owns three independently-versioned canon corpora, each fingerprinted by
a pair of committed sidecars:

  * ``app/corpus/data/blueprints/``     -> ``MANIFEST.sha256`` + ``BLUEPRINTS_SHA``
  * ``app/corpus/data/knowledge/``      -> ``MANIFEST.sha256`` + ``KNOWLEDGE_SHA``
  * ``app/semantic_catalog/data/``      -> ``MANIFEST.sha256`` + ``CATALOG_SHA``

WHY THIS FILE EXISTS (J3b-c). A canon YAML edit that does NOT regenerate its
sidecars is silently invisible downstream: the consuming agent's re-seed takes a
sha fast path and skips a corpus whose fingerprint did not move, so the edit
lands in git and never reaches the runtime. Commit ``a2c8cb9`` did exactly that —
it shipped a ``bp-active-headcount-by-department.yaml`` change with a stale
``MANIFEST.sha256``/``BLUEPRINTS_SHA``.

The per-tool test modules (``test_check_corpus_parity.py`` /
``test_check_catalog_parity.py``) already assert the committed corpora pass, but
those assertions are framed as unit tests OF THE TOOL: a failure there reads as
"the parity tool is broken" rather than "you edited canon and forgot
``--write``". This module states the invariant once, for all three corpora, in
the terms the author needs, and adds the two things the per-tool files do not:

  * a VACUITY GUARD (``TestGateIsNotVacuous``) — the checks must be pointed at
    the real, non-empty data directories. ``test_check_catalog_parity.py``
    monkeypatches the catalog tool's ``_CATALOG_DIR`` module global at runtime;
    a leak of that patch (or a future refactor that parameterises the dirs)
    would make a live check pass while inspecting nothing at all.
  * the DOWNSTREAM read (``TestServedShasMatchDisk``) — what actually gates the
    re-seed is the sha the loaders SERVE (``get_blueprints_sha()`` /
    ``get_knowledge_sha()`` / ``get_catalog_sha()``), which is the sidecar file
    read verbatim. Asserting the served value equals the recomputed live
    fingerprint closes the loop from YAML bytes to the exported fingerprint,
    rather than stopping at the on-disk sidecar.

Fixing a failure here is always the same one command:

    python tools/check_corpus_parity.py --write
    python tools/check_catalog_parity.py --write
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from app.corpus.loader import get_blueprints_sha, get_knowledge_sha
from app.semantic_catalog.loader import get_catalog_sha

_REPO_ROOT = Path(__file__).resolve().parents[2]

_REGEN_HINT = (
    "Canon YAML changed but its sidecars were not regenerated. Run "
    "`python tools/check_corpus_parity.py --write` and "
    "`python tools/check_catalog_parity.py --write`, then commit the sidecars — "
    "a canon change without them silently skips the downstream re-seed."
)


def _load_tool(module_name: str, filename: str):
    """Load a ``tools/`` script under a PRIVATE module name.

    The per-tool test modules import these same files under their own names and
    (for the catalog tool) monkeypatch module globals such as ``_CATALOG_DIR``.
    Loading a separate module object here means this gate reads the real
    module-level constants no matter what another test module does to its own
    copy — the isolation is structural, not a matter of fixture ordering.
    """
    spec = importlib.util.spec_from_file_location(module_name, _REPO_ROOT / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before exec: the corpus tool defines a @dataclass, whose processing
    # looks the module up in sys.modules by name (sys.modules[cls.__module__]).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


corpus_parity = _load_tool("_canon_gate_corpus_parity", "check_corpus_parity.py")
catalog_parity = _load_tool("_canon_gate_catalog_parity", "check_catalog_parity.py")


# ===========================================================================
# The gate must be looking at the real canon, not an empty or redirected dir
# ===========================================================================

class TestGateIsNotVacuous:
    def test_corpus_dirs_are_the_repo_data_dirs(self):
        expected = {
            "blueprints": _REPO_ROOT / "app" / "corpus" / "data" / "blueprints",
            "knowledge": _REPO_ROOT / "app" / "corpus" / "data" / "knowledge",
        }
        assert {c.name: c.data_dir for c in corpus_parity.CORPORA} == expected

    def test_catalog_dir_is_the_repo_data_dir(self):
        assert catalog_parity._CATALOG_DIR == _REPO_ROOT / "app" / "semantic_catalog" / "data"

    def test_every_canon_dir_holds_yaml(self):
        """An empty data dir would make every drift/fingerprint check pass trivially."""
        dirs = [c.data_dir for c in corpus_parity.CORPORA] + [catalog_parity._CATALOG_DIR]
        for data_dir in dirs:
            assert list(data_dir.glob("*.yaml")), f"no canon YAML found in {data_dir}"

    def test_manifests_cover_every_committed_yaml_file(self):
        """Recomputed manifests must enumerate the same files present on disk."""
        for corpus in corpus_parity.CORPORA:
            names = {p.name for p in corpus.data_dir.glob("*.yaml")}
            assert set(corpus_parity._compute_manifest(corpus)) == names
        catalog_names = {p.name for p in catalog_parity._CATALOG_DIR.glob("*.yaml")}
        assert set(catalog_parity._compute_manifest()) == catalog_names


# ===========================================================================
# The committed sidecars are current for the LIVE data dirs
# ===========================================================================

class TestCommittedSidecarsAreCurrent:
    def test_corpus_manifests_match_the_yaml_on_disk(self):
        for corpus in corpus_parity.CORPORA:
            errors = corpus_parity.check_local_drift(corpus)
            assert errors == [], f"{_REGEN_HINT}\n" + "\n".join(errors)

    def test_corpus_sha_sidecars_match_their_manifests(self):
        for corpus in corpus_parity.CORPORA:
            errors = corpus_parity.check_sidecar_fingerprint(corpus)
            assert errors == [], f"{_REGEN_HINT}\n" + "\n".join(errors)

    def test_catalog_manifest_matches_the_yaml_on_disk(self):
        errors = catalog_parity.check_local_drift()
        assert errors == [], f"{_REGEN_HINT}\n" + "\n".join(errors)

    def test_catalog_sha_sidecar_matches_its_manifest(self):
        recorded = catalog_parity._CATALOG_SHA_PATH.read_text(encoding="utf-8").strip()
        assert recorded == catalog_parity._live_fingerprint(), _REGEN_HINT


# ===========================================================================
# The fingerprints the RUNTIME serves are the ones the live YAML implies
# ===========================================================================

class TestServedShasMatchDisk:
    """The loaders read the sidecar files verbatim; the exports publish those
    values as ``blueprints_sha`` / ``knowledge_sha`` / ``catalog_sha``, and the
    consumer's re-seed decides whether to reload on exactly those. A stale
    sidecar therefore shows up here as a served fingerprint that disagrees with
    the YAML actually on disk."""

    def _corpus(self, name):
        return next(c for c in corpus_parity.CORPORA if c.name == name)

    def test_served_blueprints_sha_matches_live_yaml(self):
        live = corpus_parity._live_fingerprint(self._corpus("blueprints"))
        assert get_blueprints_sha() == live, _REGEN_HINT

    def test_served_knowledge_sha_matches_live_yaml(self):
        live = corpus_parity._live_fingerprint(self._corpus("knowledge"))
        assert get_knowledge_sha() == live, _REGEN_HINT

    def test_served_catalog_sha_matches_live_yaml(self):
        assert get_catalog_sha() == catalog_parity._live_fingerprint(), _REGEN_HINT
