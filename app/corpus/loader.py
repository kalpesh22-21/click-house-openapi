"""Corpus loader — the MCP-owned blueprint + knowledge corpus (Phase 1).

The MCP (``clickhouse-api``) is the git-versioned SOURCE OF TRUTH for the
blueprint and knowledge corpus. The YAML data files are authored/edited HERE, one
file per entry:

  * ``app/corpus/data/blueprints/<id>.yaml`` — one blueprint per file
  * ``app/corpus/data/knowledge/<id>.yaml``  — one knowledge chunk per file

and consumers (the data-analysis-agent) rebuild their corpus from the
``/blueprints/export`` / ``/knowledge/export`` payloads instead of re-parsing a
duplicated copy. This mirrors ``app/semantic_catalog/loader.py``.

Blueprints and knowledge are versioned INDEPENDENTLY: each corpus has its own
``MANIFEST.sha256`` + SHA sidecar (``BLUEPRINTS_SHA`` / ``KNOWLEDGE_SHA``),
regenerated in lockstep by ``tools/check_corpus_parity.py --write``.

Caching
-------
Like the semantic catalog (and unlike ``app/catalog.py``'s live 60s-TTL
``system.columns`` catalog), the corpus YAML only changes on a ``clickhouse-api``
redeploy — so it is loaded once and cached indefinitely per process, with an
explicit ``invalidate_corpus_cache()`` escape hatch for tests.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Directories holding the per-entry YAML files (this file lives at
# app/corpus/loader.py, so the data dirs are siblings).
_DATA_DIR = Path(__file__).parent / "data"
_BLUEPRINTS_DIR = _DATA_DIR / "blueprints"
_KNOWLEDGE_DIR = _DATA_DIR / "knowledge"

# Sidecar files: each corpus CONTENT fingerprint — ``sha1(MANIFEST.sha256)``, a
# pure function of that corpus's per-file sha256 map. NOT a git commit SHA. Read
# verbatim here; ``tools/check_corpus_parity.py --write`` regenerates them in
# lockstep with the respective MANIFEST.sha256 whenever the YAML changes.
_BLUEPRINTS_SHA_PATH = _BLUEPRINTS_DIR / "BLUEPRINTS_SHA"
_KNOWLEDGE_SHA_PATH = _KNOWLEDGE_DIR / "KNOWLEDGE_SHA"


class CorpusValidationError(RuntimeError):
    """Raised at load time when a corpus file is mis-authored.

    Covers a non-mapping YAML root, a missing / non-string ``id``, a duplicate
    ``id`` (which would silently shadow another entry depending on glob order), or
    a stored reserved key (``source`` / ``verified``, which are injected only at
    export time). The loader fails LOUD instead of serving a half- or
    wrong-provenance corpus, so a mis-authored file is caught at startup.
    """


# Reserved keys: injected at EXPORT time (app/corpus/export.py) as the constant
# MCP provenance stamp, so they must never be authored into the stored YAML — a
# stored value would masquerade as, or contradict, the export-time constant.
_RESERVED_KEYS = ("source", "verified")


def _load_entries(data_dir: Path | str) -> dict[str, dict[str, Any]]:
    """Parse every ``*.yaml`` file in ``data_dir`` once, keyed by each entry's ``id``.

    Returns ``{"<id>": <raw parsed YAML dict>, ...}``. Raises
    :class:`CorpusValidationError` when a file's root is not a mapping, its ``id``
    is missing / not a string, its ``id`` duplicates another file's, or it stores a
    reserved key — the corpus must fail loud rather than silently drop, shadow, or
    mis-provenance an entry.
    """
    data_dir = Path(data_dir)
    result: dict[str, dict[str, Any]] = {}

    for yaml_path in sorted(data_dir.glob("*.yaml")):
        with yaml_path.open(encoding="utf-8") as fh:
            raw: Any = yaml.safe_load(fh)

        if not isinstance(raw, dict):
            raise CorpusValidationError(
                f"Corpus file {yaml_path.name} must be a YAML mapping "
                f"(got {type(raw).__name__})."
            )

        entry_id = raw.get("id")
        if not entry_id:
            raise CorpusValidationError(
                f"Corpus file {yaml_path.name} has no top-level 'id' field."
            )
        if not isinstance(entry_id, str):
            raise CorpusValidationError(
                f"Corpus file {yaml_path.name} has a non-string 'id' "
                f"({entry_id!r}, {type(entry_id).__name__}) — ids must be strings."
            )

        stored_reserved = [k for k in _RESERVED_KEYS if k in raw]
        if stored_reserved:
            raise CorpusValidationError(
                f"Corpus file {yaml_path.name} declares reserved key(s) "
                f"{stored_reserved} — 'source'/'verified' are injected at export "
                "time and must not be stored in the YAML."
            )

        if entry_id in result:
            raise CorpusValidationError(
                f"Duplicate corpus id {entry_id!r} in {yaml_path.name} — each entry "
                "must have a unique id (a duplicate would silently shadow an entry)."
            )
        result[entry_id] = raw

    return result


def load_blueprints(data_dir: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Return ``{"<id>": <full parsed blueprint entry>, ...}`` — one entry per YAML file.

    If ``data_dir`` is None, resolves to ``app/corpus/data/blueprints/``. Pass an
    explicit path in tests to point at a fixture directory instead.
    """
    return _load_entries(_BLUEPRINTS_DIR if data_dir is None else data_dir)


def load_knowledge(data_dir: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Return ``{"<id>": <full parsed knowledge entry>, ...}`` — one entry per YAML file.

    If ``data_dir`` is None, resolves to ``app/corpus/data/knowledge/``. Pass an
    explicit path in tests to point at a fixture directory instead.
    """
    return _load_entries(_KNOWLEDGE_DIR if data_dir is None else data_dir)


def get_blueprints_sha() -> str:
    """Return the BLUEPRINTS_SHA sidecar value (the blueprint corpus fingerprint).

    Deterministic CONTENT fingerprint — ``sha1(MANIFEST.sha256)``, the same value
    ``build_blueprints_export()`` emits as ``blueprints_sha`` — NOT a git commit
    SHA. Raises FileNotFoundError if the sidecar is missing (a misassembled data
    dir is a deploy-time misconfiguration, not silently defaulted).
    """
    return _BLUEPRINTS_SHA_PATH.read_text(encoding="utf-8").strip()


def get_knowledge_sha() -> str:
    """Return the KNOWLEDGE_SHA sidecar value (the knowledge corpus fingerprint).

    Deterministic CONTENT fingerprint — ``sha1(MANIFEST.sha256)``, the same value
    ``build_knowledge_export()`` emits as ``knowledge_sha`` — NOT a git commit SHA.
    Raises FileNotFoundError if the sidecar is missing.
    """
    return _KNOWLEDGE_SHA_PATH.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Module-level caches — loaded once per process (see module docstring: the YAML
# only changes on redeploy). Blueprints and knowledge are cached independently.
# ---------------------------------------------------------------------------

_cached_blueprints: dict[str, dict[str, Any]] | None = None
_cached_knowledge: dict[str, dict[str, Any]] | None = None


def get_blueprints() -> dict[str, dict[str, Any]]:
    """Return the cached blueprint corpus, loading it on first call."""
    global _cached_blueprints
    if _cached_blueprints is None:
        logger.debug("corpus: loading blueprints from %s", _BLUEPRINTS_DIR)
        _cached_blueprints = load_blueprints()
        logger.debug("corpus: loaded %d blueprint entries", len(_cached_blueprints))
    return _cached_blueprints


def get_knowledge() -> dict[str, dict[str, Any]]:
    """Return the cached knowledge corpus, loading it on first call."""
    global _cached_knowledge
    if _cached_knowledge is None:
        logger.debug("corpus: loading knowledge from %s", _KNOWLEDGE_DIR)
        _cached_knowledge = load_knowledge()
        logger.debug("corpus: loaded %d knowledge entries", len(_cached_knowledge))
    return _cached_knowledge


def invalidate_corpus_cache() -> None:
    """Force the next get_blueprints()/get_knowledge() call to reload from disk.

    Useful in tests to reset state between cases.
    """
    global _cached_blueprints, _cached_knowledge
    _cached_blueprints = None
    _cached_knowledge = None
