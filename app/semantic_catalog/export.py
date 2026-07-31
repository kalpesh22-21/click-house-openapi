"""Semantic-catalog JSON export serializer (Wave 0 of "MCP as single source of truth").

This module produces the runtime-bootstrap export shape that lets a consumer
(the data-analysis-agent) rebuild ALL THREE of its catalog projections from a
single MCP-served payload instead of re-parsing a duplicated copy of the YAML:

  * ``build_sqlglot_schema()``  -> ``{db.table: {col: type_string}}``
  * ``load_semantic_catalog()`` -> the full per-table overlay entry
  * ``load_description_cols()`` -> ``{db.table: {code_col: description_col}}``

Design: MINIMAL TRANSFORM / emit-as-authored
---------------------------------------------
The export carries each table's parsed-YAML entry *verbatim* (a deep copy of
what ``get_semantic_catalog()`` returns), wrapped with the ``catalog_sha``. It
does NOT normalize column ``type`` defaults or inject ``description_col: null``.

That is deliberate and load-bearing: the agent's ``load_semantic_catalog()``
full-overlay projection is explicitly the raw YAML *as authored* — "nothing is
padded with defaults the source file didn't declare". If this exporter padded
`type: TEXT` onto typeless columns or added explicit nulls, the reconstructed
overlay handle would no longer match byte-for-byte. Emitting the raw entry lets
the agent re-apply its own extraction functions (`_extract_columns`,
`_extract_description_cols`) to reproduce the two derived projections exactly,
and use the entry itself as the full overlay. The raw entry is therefore a
strict SUPERSET of all three projections.

This function is intentionally free of any FastAPI/HTTP coupling so it can be
reused by both the future ``/catalog/export`` endpoint and the agent-side
fixture-regen script.

Types are the YAML-declared catalog types (e.g. ``Nullable(String)``), NOT live
``system.columns`` types — the export has no database dependency.
"""

from __future__ import annotations

import copy
from typing import Any

from app.semantic_catalog.loader import get_catalog_sha, get_semantic_catalog


def build_catalog_export_from(
    catalog: dict[str, dict[str, Any]], sha: str
) -> dict[str, Any]:
    """Build the export dict from an already-loaded catalog and its SHA.

    ``catalog`` is the ``{"database.table": <raw parsed YAML entry>, ...}`` mapping
    as returned by ``load_semantic_catalog()`` / ``get_semantic_catalog()``. The
    entries are deep-copied so callers (and the process-level catalog cache) can
    never be mutated through the returned structure.

    Returns::

        {
          "catalog_sha": "<sha>",
          "catalog": { "<db>.<table>": <verbatim parsed-YAML entry>, ... }
        }

    The result is pure data (dicts / lists / scalars) and JSON-serializable.
    """
    return {
        "catalog_sha": sha,
        "catalog": copy.deepcopy(catalog),
    }


def build_catalog_export() -> dict[str, Any]:
    """Build the full catalog export from the process's loaded catalog + CATALOG_SHA.

    Scope-INDEPENDENT: emits every catalogued table with every column — this is
    runtime-bootstrap data, not the model-facing, column-scope-filtered
    ``getTableSchema`` overlay.
    """
    return build_catalog_export_from(get_semantic_catalog(), get_catalog_sha())
