"""Corpus JSON export serializers (Phase 1 of "MCP as source of truth").

These builders produce the runtime-bootstrap export shapes that let a consumer
(the data-analysis-agent) rebuild its blueprint + knowledge corpus from a single
MCP-served payload instead of re-parsing a duplicated copy of the YAML:

  * ``build_blueprints_export()`` -> ``{"blueprints_sha": <sha>, "blueprints": {...}}``
  * ``build_knowledge_export()``  -> ``{"knowledge_sha": <sha>, "knowledge": {...}}``

Design: emit-as-authored + inject the constant provenance
---------------------------------------------------------
Each entry is carried *verbatim* (a deep copy of what the loader returns), wrapped
with the corpus SHA — mirroring ``app/semantic_catalog/export.py`` (no default
padding, no reformatting).

On top of that, every exported entry has two CONSTANT provenance fields injected
at export time: ``source: "mcp"`` and ``verified: true``. These mark the entry as
MCP-owned, human-reviewed canon. They are deliberately NOT stored in the YAML
files (all MCP-served entries carry the identical values) — they are added here so
the on-disk corpus stays free of redundant boilerplate and the SHA is a pure
function of the authored content.

These functions are intentionally free of any FastAPI/HTTP coupling so they can be
reused by both the ``/blueprints/export`` / ``/knowledge/export`` endpoints and any
consumer-side fixture-regen script.
"""

from __future__ import annotations

import copy
from typing import Any

from app.corpus.loader import (
    get_blueprints,
    get_blueprints_sha,
    get_knowledge,
    get_knowledge_sha,
)

# Constant provenance stamped onto every MCP-served corpus entry at export time.
# Not stored in the YAML (see module docstring): every entry the MCP serves is,
# by definition, MCP-owned and verified canon.
_SOURCE = "mcp"
_VERIFIED = True


def _export_entries(
    entries: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Deep-copy each entry verbatim and inject the constant ``source``/``verified``.

    The entries are deep-copied so callers (and the process-level corpus cache) can
    never be mutated through the returned structure.
    """
    exported: dict[str, dict[str, Any]] = {}
    for entry_id, entry in entries.items():
        copied = copy.deepcopy(entry)
        copied["source"] = _SOURCE
        copied["verified"] = _VERIFIED
        exported[entry_id] = copied
    return exported


def build_blueprints_export_from(
    blueprints: dict[str, dict[str, Any]], sha: str
) -> dict[str, Any]:
    """Build the blueprints export dict from an already-loaded corpus and its SHA.

    ``blueprints`` is the ``{"<id>": <raw parsed YAML entry>, ...}`` mapping as
    returned by ``load_blueprints()`` / ``get_blueprints()``.

    Returns::

        {
          "blueprints_sha": "<sha>",
          "blueprints": {
            "<id>": { <verbatim entry>, "source": "mcp", "verified": true },
            ...
          }
        }

    The result is pure data (dicts / lists / scalars) and JSON-serializable.
    """
    return {
        "blueprints_sha": sha,
        "blueprints": _export_entries(blueprints),
    }


def build_knowledge_export_from(
    knowledge: dict[str, dict[str, Any]], sha: str
) -> dict[str, Any]:
    """Build the knowledge export dict from an already-loaded corpus and its SHA.

    ``knowledge`` is the ``{"<id>": <raw parsed YAML entry>, ...}`` mapping as
    returned by ``load_knowledge()`` / ``get_knowledge()``.

    Returns::

        {
          "knowledge_sha": "<sha>",
          "knowledge": {
            "<id>": { <verbatim entry>, "source": "mcp", "verified": true },
            ...
          }
        }

    The result is pure data (dicts / lists / scalars) and JSON-serializable.
    """
    return {
        "knowledge_sha": sha,
        "knowledge": _export_entries(knowledge),
    }


def build_blueprints_export() -> dict[str, Any]:
    """Build the full blueprints export from the process corpus + BLUEPRINTS_SHA."""
    return build_blueprints_export_from(get_blueprints(), get_blueprints_sha())


def build_knowledge_export() -> dict[str, Any]:
    """Build the full knowledge export from the process corpus + KNOWLEDGE_SHA."""
    return build_knowledge_export_from(get_knowledge(), get_knowledge_sha())
