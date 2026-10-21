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

Source-of-truth (post-Wave-1a): the **MCP (`clickhouse-api`) is authoritative**
for the YAML content — it is authored/edited HERE, and the data-analysis-agent
DERIVES its `tests/fixtures/catalog_export.json` from this MCP via
`build_catalog_export()` (`app/semantic_catalog/export.py`). The parsing-function
lineage above is historical: those functions were originally copied in from the
agent, but the data direction has since inverted. `tools/check_catalog_parity.py`
provides a local tamper/drift check (always) plus a best-effort agent-fixture
staleness check when the agent's fixture path is available (see that script's
docstring).

The authored catalog also supplies the schema used for query provenance and
scope validation. Physical sources are resolved for the caller at execution time; request
validation never discovers columns or silently removes unavailable entries.

Backend-scoped notes (`trino_notes` / `clickhouse_notes`)
---------------------------------------------------------
A table entry may carry the OPTIONAL top-level keys ``trino_notes`` and/or
``clickhouse_notes`` (each a list of strings) next to the always-on
``schema_notes``. This loader is deliberately unknown-key tolerant: both keys
ride through the raw dict UNMATCHED, and the serialized surfaces
(`app/semantic_catalog/overlay.py::build_table_schema_response` and
`app/semantic_catalog/export.py::build_catalog_export_from`) are the ONLY
points that interpret them — emitting shared notes plus the ACTIVE backend's
scoped notes under a synthesized ``notes`` key, and dropping the inactive
backend's note ENTIRELY (neither scoped key ever appears in a payload). See
`scoped_notes_for_backend` below.

Backend-scoped values (`trino_values` / `clickhouse_values`)
------------------------------------------------------------
A per-column enumeration field listed in ``BACKEND_SCOPED_VALUE_FIELDS``
(today: only ``values``) may carry the OPTIONAL sibling keys
``trino_<field>`` / ``clickhouse_<field>`` next to the always-on plain
``<field>``. The plain key is the ClickHouse default; a sibling key is a
per-backend OVERRIDE resolved by the serialization surfaces (never by this
loader — unknown-key tolerance means the scoped keys ride the raw dict
untouched here). Resolution rules (see `scoped_field_for_backend`):

  * the ACTIVE backend's ``<backend>_<field>`` wins when present and
    non-None; a None-valued scoped key is treated as absent and falls back
    to the plain field;
  * the inactive backend's scoped key is NEVER serialized;
  * a scoped override WITHOUT the plain ``<field>`` default (an "orphan"
    override) is an authoring error and raises ``ValueError`` — checked for
    BOTH spellings regardless of which backend is active, so an orphan can
    never hide behind a deployment flag.

``observed_values`` is deliberately NOT part of this machinery: it is a
plain pass-through field on every backend.

Rule backend membership (`backends:`)
-------------------------------------
A ``rules[]`` entry may carry an OPTIONAL ``backends: [clickhouse|trino]``
list restricting the rule to the named serving backend(s). Absent means the
rule applies to BOTH backends. The serialized surfaces exclude rules whose
membership excludes the active backend and strip the ``backends`` key from
every emitted rule, so the mechanism never appears in a payload. A malformed
``backends`` value (not a non-empty list, or naming anything outside
``KNOWN_BACKENDS``) raises ``ValueError`` LOUDLY at resolution time — a rule
quietly visible on the wrong backend is a semantic-enforcement bug, not a
cosmetic one. See `rule_applies_to_backend` and `resolve_rules_for_backend`.
Authored ``description:`` text (table-level, per-column, per-rule, and every
other ``description:`` field) must stay DATABASE-AGNOSTIC — never naming a
concrete backend; backend-specific text belongs in the scoped note keys
above. This is enforced by
``tests/semantic_catalog/test_description_backend_neutrality.py``.

Caching
-------
The YAML catalog changes on deployment and is cached once per process.
Startup validates the local catalog without tenant-source probes. The explicit
`invalidate_semantic_catalog_cache()` hook is for tests; production catalog
changes require a restart.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# The single warehouse database name.  Every current YAML declares
# `database: dbpcm_warehouse`.  If a YAML omits the field, this constant is the
# fallback so the loader remains usable without requiring every file to repeat it.
DEFAULT_DATABASE = "dbpcm_warehouse"

# ---------------------------------------------------------------------------
# Fail-closed RLS invariant (A-M1)
# ---------------------------------------------------------------------------
# The tenant-scoped warehouse tables carry physical columns (client_code /
# proc_center, plus the ReplacingMergeTree `version` on labor_allocation) that
# are enforced server-side by a ClickHouse row policy and must NEVER be surfaced
# to the model. That guarantee is carried by each table's
# `mcp_projection.hidden_columns` (see overlay.py::_hidden_columns). If one of
# these tables were to load WITHOUT that projection — because the file is
# missing, its `table:`/`columns:` block has a typo (silently degrading it to
# uncatalogued/exposed), or `hidden_columns` was emptied — the RLS columns would
# leak. We therefore assert the invariant at load time and refuse to start,
# rather than fail open. This is deliberately scoped to these RLS tables only:
# unrelated catalog files keep their existing "silently skip a non-table file"
# behaviour.
_REQUIRED_RLS_HIDDEN_COLUMNS: dict[str, frozenset[str]] = {
    f"{DEFAULT_DATABASE}.accrual_events": frozenset({"client_code", "proc_center", "employee_code_num"}),
    f"{DEFAULT_DATABASE}.accrual_balances": frozenset({"client_code", "proc_center", "employee_code_num"}),
    f"{DEFAULT_DATABASE}.employee": frozenset({"client_code", "proc_center"}),
    f"{DEFAULT_DATABASE}.payroll": frozenset({"client_code", "proc_center"}),
    f"{DEFAULT_DATABASE}.department": frozenset({"client_code", "proc_center"}),
    f"{DEFAULT_DATABASE}.punch_pair": frozenset({"client_code", "proc_center"}),
    f"{DEFAULT_DATABASE}.total_earning": frozenset({"client_code", "proc_center"}),
    f"{DEFAULT_DATABASE}.labor_allocation": frozenset(
        {"client_code", "proc_center", "version"}
    ),
    f"{DEFAULT_DATABASE}.employee_change": frozenset({"client_code", "proc_center"}),
}


class CatalogValidationError(RuntimeError):
    """Raised at load time when a required RLS table is missing/uncatalogued or its
    ``mcp_projection.hidden_columns`` does not cover the RLS-enforced columns.

    This is a fail-CLOSED startup error: the process must not serve schema/query
    traffic while an RLS table could leak ``client_code`` / ``proc_center`` /
    ``version`` to the model.
    """


# ---------------------------------------------------------------------------
# Backend-scoped notes (`trino_notes` / `clickhouse_notes`)
# ---------------------------------------------------------------------------
# The catalog's authored keys. A table entry may carry either/both; the shared,
# backend-neutral `schema_notes` key is ALWAYS serialized regardless of the
# active backend. The scoped keys themselves are NEVER serialized — see
# overlay.py::build_table_schema_response and export.py::build_catalog_export_from.
BACKEND_SCOPED_NOTE_KEYS: tuple[str, str] = ("trino_notes", "clickhouse_notes")


def scoped_notes_for_backend(
    entry: dict[str, Any] | None, *, trino_enabled: bool
) -> list[str]:
    """Return the ACTIVE backend's scoped notes for a catalog table entry.

    ``trino_enabled=True`` reads the entry's ``trino_notes``; ``False`` reads
    ``clickhouse_notes``. The INACTIVE backend's key is never even consulted.
    Defensive shape check: a malformed value (not a list of strings) yields an
    empty list rather than propagating garbage into a serialized payload — the
    loader's unknown-key tolerance means the authoring surface is not schema-
    enforced, so the projection fails quietly toward "no note" (notes are
    documentation; dropping one is never a correctness or RLS issue, unlike the
    fail-closed hidden-columns invariant above).
    """
    if not isinstance(entry, dict):
        return []
    raw = entry.get("trino_notes" if trino_enabled else "clickhouse_notes")
    if not isinstance(raw, list):
        return []
    return [note for note in raw if isinstance(note, str) and note]


# ---------------------------------------------------------------------------
# Backend-scoped values (`trino_values` / `clickhouse_values`) + rule backends
# ---------------------------------------------------------------------------
# The registry of per-column enumeration fields a backend may override with a
# sibling key (`<backend>_<field>`). Today only `values` participates;
# `observed_values` is deliberately a plain pass-through field (see module
# docstring). The loader itself never interprets these keys — unknown-key
# tolerance rides them through verbatim; resolution happens ONLY at the
# serialization surfaces (overlay.py / export.py).
BACKEND_SCOPED_VALUE_FIELDS: tuple[str, ...] = ("values",)

# The only backend names a `backends:` rule-membership list (or a
# `<backend>_<field>` override spelling) may name. Anything else is a loud
# ValueError at resolution time, never a silent drop.
KNOWN_BACKENDS: tuple[str, str] = ("clickhouse", "trino")

# Every derived override-spelling the serialization surfaces must strip so the
# mechanism never leaks into a payload (e.g. "clickhouse_values", "trino_values").
BACKEND_SCOPED_VALUE_KEYS: tuple[str, ...] = tuple(
    f"{backend}_{field}"
    for field in BACKEND_SCOPED_VALUE_FIELDS
    for backend in KNOWN_BACKENDS
)


def _active_backend_name(trino_enabled: bool) -> str:
    return "trino" if trino_enabled else "clickhouse"


def scoped_field_for_backend(
    mapping: Mapping[str, Any] | None, field: str, *, trino_enabled: bool
) -> Any:
    """Resolve *field* of *mapping* for the ACTIVE backend.

    Reads ``<backend>_<field>`` (``trino_values`` when ``trino_enabled`` else
    ``clickhouse_values``) as an OVERRIDE of the plain ``field`` — the plain
    value is the ClickHouse default and the fallback when no scoped override
    applies:

      * the active backend's scoped key wins when present and non-None;
      * a None-valued scoped key is treated as ABSENT (None is not an
        override) and falls back to the plain field;
      * the inactive backend's scoped key is never consulted.

    ORPHAN guard (checked for BOTH spellings regardless of which backend is
    active, so the error can never hide behind a deployment flag): a
    non-None scoped override on a mapping with no plain ``field`` default is
    an authoring error and raises ``ValueError``. A mapping without the
    concept at all simply yields ``None`` (never raises).
    """
    if not isinstance(mapping, Mapping):
        return None
    plain = mapping.get(field)
    for backend in KNOWN_BACKENDS:
        scoped_key = f"{backend}_{field}"
        if mapping.get(scoped_key) is not None and plain is None:
            raise ValueError(
                f"Backend-scoped override {scoped_key!r} has no plain {field!r} "
                "default to override; author the plain ClickHouse-default value "
                f"(or drop the scoped key). Checked for both {KNOWN_BACKENDS} "
                "spellings regardless of the active backend."
            )
    override = mapping.get(f"{_active_backend_name(trino_enabled)}_{field}")
    if override is not None:
        return override
    return plain


def rule_applies_to_backend(rule: Mapping[str, Any], *, trino_enabled: bool) -> bool:
    """True iff a ``rules[]`` entry applies to the ACTIVE backend.

    An absent ``backends`` key means the rule applies to EVERY backend
    (unchanged, pre-mechanism behaviour). When present, ``backends`` must be a
    non-empty list drawn from ``KNOWN_BACKENDS`` — anything else (a scalar, a
    mapping, an empty list, or an unknown backend name) raises ``ValueError``
    LOUDLY: a rule silently mis-visible on the wrong backend is a semantic-
    enforcement bug, never a cosmetic one. A non-mapping rule is not this
    helper's concern (the scope filter owns shape handling) and passes.
    """
    if not isinstance(rule, Mapping):
        return True
    backends = rule.get("backends")
    if backends is None:
        return True
    if not isinstance(backends, list) or not backends:
        raise ValueError(
            f"Rule {rule.get('id')!r} has a malformed 'backends' value "
            f"{backends!r}: expected a non-empty list drawn from "
            f"{list(KNOWN_BACKENDS)} (absent means 'applies to every backend')."
        )
    unknown = [name for name in backends if name not in KNOWN_BACKENDS]
    if unknown:
        raise ValueError(
            f"Rule {rule.get('id')!r} names unknown backend(s) {unknown!r} in "
            f"'backends'; known backends are {list(KNOWN_BACKENDS)}. Fix the "
            "typo or extend KNOWN_BACKENDS deliberately."
        )
    return _active_backend_name(trino_enabled) in backends


def resolve_rules_for_backend(
    rules: list[dict[str, Any]] | None, *, trino_enabled: bool
) -> list[dict[str, Any]] | None:
    """Apply rule backend membership: exclude rules not applying to the ACTIVE
    backend and strip the ``backends`` mechanism key from every emitted rule.

    Both serialization surfaces (overlay.py's getTableSchema response and
    export.py's catalog export) call this BEFORE any other rule processing
    (e.g. overlay's ``_filter_rules`` scope enforcement), and it runs in EVERY
    mode — scope enforcement, stdio (``scope=None``), and allow-all alike.

    IDENTITY PASS-THROUGH: a rules list in which NO entry carries a
    ``backends`` key is returned as-is (the same object), so catalogs without
    scoped rules serialize exactly as before the mechanism existed. Validation
    (``rule_applies_to_backend``) only runs on entries that actually declare
    ``backends`` — unscoped entries can never raise here.
    """
    if rules is None:
        return None
    if not any(isinstance(rule, Mapping) and "backends" in rule for rule in rules):
        return rules
    resolved: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, Mapping) or "backends" not in rule:
            resolved.append(rule)
            continue
        if rule_applies_to_backend(rule, trino_enabled=trino_enabled):
            resolved.append({k: v for k, v in rule.items() if k != "backends"})
    return resolved


def _validate_rls_projection(catalog: dict[str, dict[str, Any]]) -> None:
    """Assert every required RLS table is catalogued and hides its RLS columns.

    Runs against the process-level catalog (the real ``data/`` dir). Raises
    :class:`CatalogValidationError` — never silently degrades — if:

      * a required RLS table is absent (uncatalogued, or its YAML failed to load
        because of a ``table:`` / ``columns:`` typo — such a file is skipped by
        ``_load_raw_table_entries`` and would otherwise vanish unnoticed), or
      * its ``mcp_projection.hidden_columns`` is empty / missing, or
      * it does not hide the required RLS columns for that table.
    """
    for qualified_key, required in _REQUIRED_RLS_HIDDEN_COLUMNS.items():
        entry = catalog.get(qualified_key)
        if entry is None:
            raise CatalogValidationError(
                f"Required RLS table {qualified_key!r} is missing from the semantic "
                "catalog. It is either uncatalogued or its YAML failed to load (check "
                "for a `table:`/`columns:` typo that would silently degrade it to an "
                "uncatalogued/exposed table). RLS tables must fail CLOSED — refusing "
                "to start."
            )
        projection = entry.get("mcp_projection")
        hidden = projection.get("hidden_columns") if isinstance(projection, dict) else None
        hidden_set = {str(c) for c in hidden} if isinstance(hidden, list) else set()
        if not hidden_set:
            raise CatalogValidationError(
                f"Required RLS table {qualified_key!r} has an empty/missing "
                "mcp_projection.hidden_columns. The row-policy-enforced physical "
                "columns must be hidden from the model — refusing to start."
            )
        missing = required - hidden_set
        if missing:
            raise CatalogValidationError(
                f"Required RLS table {qualified_key!r} does not hide the RLS "
                f"column(s) {sorted(missing)} "
                f"(mcp_projection.hidden_columns={sorted(hidden_set)}). These columns "
                "are enforced by row-level policy and must never be surfaced — "
                "refusing to start."
            )

# Directory holding the copied-in YAML files (this file lives at
# app/semantic_catalog/loader.py, so the data dir is a sibling).
_DEFAULT_SCHEMA_DIR = Path(__file__).parent / "data"

# Sidecar file: the catalog CONTENT fingerprint — `sha1(MANIFEST.sha256)`, a pure
# function of the per-file sha256 map (D84, Wave 0). It is NOT a git commit SHA.
# Read verbatim here; `tools/check_catalog_parity.py --write` regenerates it in
# lockstep with MANIFEST.sha256 whenever the catalog YAML changes.
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
        if entry.get("source", "clickhouse") not in ("clickhouse", "mysql"):
            raise CatalogValidationError(
                f"{qualified_key}: source must be 'clickhouse' or 'mysql'."
            )
        for name, column in raw_columns.items():
            if isinstance(column, dict) and "context_sql" in column:
                if not isinstance(column["context_sql"], str) or not column["context_sql"].strip():
                    raise CatalogValidationError(f"{qualified_key}.{name}: context_sql must be non-empty SQL")
                # Backend-native syntax is validated at execution, where the
                # active backend is known. Never embed private SQL in diagnostics.
        entry.pop("trino_join_keys", None)  # Retired execution optimization.
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
    """Return the CATALOG_SHA sidecar value (the catalog content fingerprint).

    This is a deterministic CONTENT fingerprint — ``sha1(MANIFEST.sha256)``, the
    same value ``build_catalog_export()`` emits as ``catalog_sha`` — NOT a git
    commit SHA.

    Raises FileNotFoundError if the sidecar is missing — a missing sidecar means
    the catalog data dir is misassembled and should be treated as a deploy-time
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
        catalog = load_semantic_catalog()
        # Fail closed (A-M1): assert the RLS tables are catalogued and hide their
        # row-policy columns BEFORE the catalog is cached/served. Raising here
        # (not caching a partial/leaky catalog) keeps the invariant airtight.
        _validate_rls_projection(catalog)
        _cached_semantic_catalog = catalog
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
