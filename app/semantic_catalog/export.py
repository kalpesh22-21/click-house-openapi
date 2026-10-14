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

Documented exceptions — backend-scoped keys
-------------------------------------------
The optional backend-scoped keys (see ``app/semantic_catalog/loader.py``) are
NEVER emitted verbatim. Three resolutions apply, all keyed off the GENERATING
PROCESS's flavor (``trino_enabled`` — the deployment's ``TRINO_ENABLED``
setting at export build time; the payload carries exactly one flavor, not
both):

  * scoped NOTES: ``trino_notes`` / ``clickhouse_notes`` entry keys are
    stripped unconditionally; when the ACTIVE backend has scoped notes they
    surface merged under a synthesized ``notes`` key (the ONLY key this
    module synthesizes). Shared ``schema_notes`` stay untouched — they are
    backend-neutral by construction. A table without scoped notes serializes
    byte-identically to the pre-mechanism export, and the inactive backend's
    text never crosses the wire on either payload shape (/catalog/export or
    getTableSchema).
  * scoped VALUES: per-column fields in the loader's
    ``BACKEND_SCOPED_VALUE_FIELDS`` (today: only ``values``) are substituted
    with the ACTIVE backend's ``<backend>_<field>`` sibling-key override when
    one applies (a None-valued scoped key is no override), and every
    mechanism spelling in ``BACKEND_SCOPED_VALUE_KEYS`` is stripped from the
    emitted column block — the consumer sees exactly one resolved ``values``
    list, as authored for its own flavor. ``observed_values`` is plain
    pass-through (never scoped).
  * rule BACKENDS: a ``rules[]`` entry declaring ``backends: [...]`` that
    does not include the ACTIVE backend is EXCLUDED from the emitted rules
    list; surviving rules never carry the ``backends`` key.

A table without any scoped content serializes byte-identically to the
pre-mechanism export on BOTH flavors (the byte-identical-output invariant).

This function is intentionally free of any FastAPI/HTTP coupling so it can be
reused by both the future ``/catalog/export`` endpoint and the agent-side
fixture-regen script.

Types are the YAML-declared catalog types (e.g. ``Nullable(String)``), NOT live
``system.columns`` types — the export has no database dependency.
"""

from __future__ import annotations

import copy
from typing import Any

from app.semantic_catalog.loader import (
    BACKEND_SCOPED_NOTE_KEYS,
    BACKEND_SCOPED_VALUE_FIELDS,
    KNOWN_BACKENDS,
    get_catalog_sha,
    get_semantic_catalog,
    resolve_rules_for_backend,
    scoped_field_for_backend,
    scoped_notes_for_backend,
)


def build_catalog_export_from(
    catalog: dict[str, dict[str, Any]], sha: str, *, trino_enabled: bool = False
) -> dict[str, Any]:
    """Build the export dict from an already-loaded catalog and its SHA.

    ``catalog`` is the ``{"database.table": <raw parsed YAML entry>, ...}`` mapping
    as returned by ``load_semantic_catalog()`` / ``get_semantic_catalog()``. The
    entries are deep-copied so callers (and the process-level catalog cache) can
    never be mutated through the returned structure.

    ``trino_enabled`` is the running deployment's active backend.  Every entry
    round-trips verbatim EXCEPT the backend-scoped mechanism keys, which are
    consumed at this surface:

      * the scoped note keys (``trino_notes`` / ``clickhouse_notes``) are
        stripped unconditionally; the active backend's notes surface under the
        synthesized ``notes`` key (absent when the active backend has none — a
        table without scoped notes is byte-identical to the pre-mechanism
        shape);
      * per-column fields in ``BACKEND_SCOPED_VALUE_FIELDS`` are substituted
        with the active backend's sibling-key override when one applies, and
        every spelling in ``BACKEND_SCOPED_VALUE_KEYS`` is stripped;
      * ``rules[]`` entries whose ``backends`` membership excludes the active
        backend are excluded, and the ``backends`` key never appears on an
        emitted rule (``resolve_rules_for_backend``, identity pass-through
        when nothing is scoped).

    Returns::

        {
          "catalog_sha": "<sha>",
          "catalog": { "<db>.<table>": <parsed-YAML entry, backend-scoped keys resolved>, ... }
        }

    The result is pure data (dicts / lists / scalars) and JSON-serializable.
    """
    projected: dict[str, dict[str, Any]] = {}
    for qualified_key, entry in catalog.items():
        copied = copy.deepcopy(entry)
        copied.pop("trino_join_keys", None)
        active_notes = scoped_notes_for_backend(copied, trino_enabled=trino_enabled)
        for scoped_key in BACKEND_SCOPED_NOTE_KEYS:
            copied.pop(scoped_key, None)
        if active_notes:
            copied["notes"] = active_notes
        # Backend-scoped VALUES: resolve the active flavor's override into the
        # plain field and strip every mechanism spelling, so the payload shows
        # exactly one `values` list and never a mechanism key. Untouched when
        # no scoped key exists (byte-identical invariant).
        columns = copied.get("columns")
        if isinstance(columns, dict):
            for column_def in columns.values():
                if not isinstance(column_def, dict):
                    continue
                for field in BACKEND_SCOPED_VALUE_FIELDS:
                    scoped_keys = tuple(
                        f"{backend}_{field}" for backend in KNOWN_BACKENDS
                    )
                    if not any(scoped_key in column_def for scoped_key in scoped_keys):
                        continue  # nothing scoped for this field — verbatim
                    resolved = scoped_field_for_backend(
                        column_def, field, trino_enabled=trino_enabled
                    )
                    for scoped_key in scoped_keys:
                        column_def.pop(scoped_key, None)
                    if resolved is None:
                        column_def.pop(field, None)
                    else:
                        column_def[field] = resolved
        # Rule backend membership: exclude non-matching rules; strip the key.
        rules = copied.get("rules")
        if isinstance(rules, list):
            copied["rules"] = resolve_rules_for_backend(
                rules, trino_enabled=trino_enabled
            )
        projected[qualified_key] = copied
    return {
        "catalog_sha": sha,
        "catalog": projected,
    }


def build_catalog_export() -> dict[str, Any]:
    """Build the full catalog export from the process's loaded catalog + CATALOG_SHA.

    Scope-INDEPENDENT: emits every catalogued table with every column — this is
    runtime-bootstrap data, not the model-facing, column-scope-filtered
    ``getTableSchema`` overlay.

    Flavor: backend-scoped content (notes / values / rule membership) follows
    the GENERATING process's ``TRINO_ENABLED`` setting — the payload is the
    active backend's single resolved flavor.
    """
    from app.config import get_settings

    return build_catalog_export_from(
        get_semantic_catalog(),
        get_catalog_sha(),
        trino_enabled=get_settings().trino_enabled,
    )