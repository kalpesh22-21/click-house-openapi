"""Corpus subsystem — the git-versioned source of truth for the blueprint and
knowledge corpus, served (read-only) via ``/blueprints/export`` and
``/knowledge/export`` on the MCP HTTP app.

This is a SEPARATE subsystem from ``app/semantic_catalog/`` (the table/column
overlay). The two are versioned independently — blueprints and knowledge each
carry their own ``MANIFEST.sha256`` + SHA sidecar — and share no data files.
"""

from app.corpus.export import build_blueprints_export, build_knowledge_export
from app.corpus.loader import (
    CorpusValidationError,
    get_blueprints,
    get_knowledge,
    invalidate_corpus_cache,
)

__all__ = [
    "CorpusValidationError",
    "build_blueprints_export",
    "build_knowledge_export",
    "get_blueprints",
    "get_knowledge",
    "invalidate_corpus_cache",
]
