"""Semantic Catalog loader — richer overlay source for `getTableSchema` (D83/D84).

Copy-in provenance (D79(a)-style, per mcp-overlay-design.md OQ-1)
-------------------------------------------------------------------
The parsing functions in this module (`_load_raw_table_entries`,
`_resolve_schema_dir`, `load_semantic_catalog`, `DEFAULT_DATABASE`) are copied in
from `data-analysis-agent/src/data_agent/catalog/loader.py`
(`load_semantic_catalog` / `_load_raw_table_entries` / `_resolve_schema_dir` /
`DEFAULT_DATABASE`), mirroring the same copy-in convention D79(a) established for
the column-provenance extractor (`app/sqlparse/provenance.py`). The YAML data
files themselves live in `app/semantic_catalog/data/*.yaml`, copied from
`data-analysis-agent/databaseSchemaDocs/*.yaml`.

The **spec repo (`data-analysis-agent`) is authoritative** for both the loader
contract and the YAML content; any change there is mirrored into this copy in
the same sprint (D79a discipline). `tools/check_catalog_parity.py` provides a
local tamper/drift check (always) plus a best-effort cross-repo staleness check
when a local clone of the source repo is available (see that script's docstring
for the known CI cross-repo-access limitation).

This is a SEPARATE catalog from `app/catalog.py`'s interim `system.columns`
catalog, which continues to feed `extract_column_provenance()` for
`runQuery`/`sampleRows` column-scope enforcement (D57) — replacing that interim
provenance catalog with this richer YAML catalog is an explicit follow-up, not
done in this pass (see D83/D84 delivery notes). This loader is additive: it
feeds ONLY the `getTableSchema` overlay (D83) via `app/semantic_catalog/overlay.py`.

Caching
-------
Unlike `app/catalog.py` (60s TTL, because `system.columns` can change any time
ClickHouse's schema does), the YAML catalog only changes on a `clickhouse-api`
redeploy (it is a copy-in, human-reviewed file per D53(b)/D79a) — so it is
loaded once and cached indefinitely per process, with an explicit
`invalidate_semantic_catalog_cache()` escape hatch for tests.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# The single warehouse database name.  Every current YAML declares
# `database: dbpcm_warehouse`.  If a YAML omits the field, this constant is the
# fallback so the loader remains usable without requiring every file to repeat it.
DEFAULT_DATABASE = "dbpcm_warehouse"

# Directory holding the copied-in YAML files (this file lives at
# app/semantic_catalog/loader.py, so the data dir is a sibling).
_DEFAULT_SCHEMA_DIR = Path(__file__).parent / "data"

# Sidecar file written at copy-in time: the source repo's last commit SHA that
# touched `databaseSchemaDocs/**` (D84). Read verbatim, never recomputed here —
# recomputing it would require the source repo's git history, which a copy-in
# does not have.
_CATALOG_SHA_PATH = _DEFAULT_SCHEMA_DIR / "CATALOG_SHA"


def _resolve_schema_dir(schema_dir: Path | str | None) -> Path:
    """Resolve the semantic-catalog YAML directory, defaulting to the copied-in data/ dir.

    Pass an explicit path in tests to point at a fixture directory instead.
    """
    if schema_dir is None:
        return _DEFAULT_SCHEMA_DIR
    return Path(schema_dir)


def _load_raw_table_entries(schema_dir: Path | str) -> dict[str, dict[str, Any]]:
    """Parse every *.yaml file in `schema_dir` once and return the raw per-table dict.

    Returns {"database.table": <raw parsed YAML dict, with `database` normalized
    to the resolved value>, ...}.

    Only files with a top-level `table` and `columns` key are included.  Files
    that don't declare a table schema (e.g. authoring notes) are silently skipped.
    """
    schema_dir = Path(schema_dir)
    result: dict[str, dict[str, Any]] = {}

    for yaml_path in sorted(schema_dir.glob("*.yaml")):
        with yaml_path.open(encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh) or {}

        table_name = raw.get("table")
        if not table_name:
            # Not a table-schema file.
            continue

        raw_columns = raw.get("columns")
        if not raw_columns or not isinstance(raw_columns, dict):
            # Table declared but no columns block — skip; can't enumerate columns.
            continue

        database = raw.get("database", DEFAULT_DATABASE)
        qualified_key = f"{database}.{table_name}"

        entry = dict(raw)
        entry["database"] = database
        result[qualified_key] = entry

    return result


def load_semantic_catalog(schema_dir: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Return {"database.table": <full parsed semantic entry>, ...} — one entry per table YAML.

    Includes every catalog field (`grain`, `temporal`, `primary_key`, `join_keys`,
    `measures`, `rules`, `ambiguities`, `description`, and the full per-column
    block — `type`, `description`, `unit`, `client_defined`, `sensitive`,
    `values`, `observed_values`, `synonyms`), for the getTableSchema MCP overlay
    (D83/D84).

    Column/table name casing is preserved exactly as authored (D70) — nothing is
    lowercased.

    If `schema_dir` is None, resolves to the copied-in `app/semantic_catalog/data/`
    directory.
    """
    return _load_raw_table_entries(_resolve_schema_dir(schema_dir))


def get_catalog_sha() -> str:
    """Return the CATALOG_SHA sidecar value (the source repo's commit SHA at copy-in time).

    Raises FileNotFoundError if the sidecar is missing — a missing sidecar means
    the copy-in was done incorrectly and should be treated as a deploy-time
    misconfiguration, not silently defaulted.
    """
    return _CATALOG_SHA_PATH.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Module-level cache — loaded once per process (see module docstring: the YAML
# only changes on redeploy, unlike app/catalog.py's live system.columns catalog).
# ---------------------------------------------------------------------------

_cached_semantic_catalog: dict[str, dict[str, Any]] | None = None


def get_semantic_catalog() -> dict[str, dict[str, Any]]:
    """Return the cached semantic catalog, loading it on first call."""
    global _cached_semantic_catalog
    if _cached_semantic_catalog is None:
        logger.debug("semantic_catalog: loading from %s", _DEFAULT_SCHEMA_DIR)
        _cached_semantic_catalog = load_semantic_catalog()
        logger.debug(
            "semantic_catalog: loaded %d table entries", len(_cached_semantic_catalog)
        )
    return _cached_semantic_catalog


def invalidate_semantic_catalog_cache() -> None:
    """Force the next get_semantic_catalog() call to reload from disk.

    Useful in tests to reset state between cases.
    """
    global _cached_semantic_catalog
    _cached_semantic_catalog = None
