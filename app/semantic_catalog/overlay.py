"""`getTableSchema` merge + scope-filter pipeline (D83/D84).

Implements mcp-overlay-design.md §1 exactly:

    1. Introspect (unchanged, done by the caller — app/service.py:get_table_schema).
    2. Load catalog: look up "{database}.{table}" in the semantic catalog (§3).
    3. MERGE: introspection <-> catalog, keyed on column name (case-sensitive
       exact match, D70 — no case-insensitive fallback anywhere in this module).
    4. SCOPE-FILTER: drop any column / table-level block-entry that references a
       column outside `column_scope` (skipped entirely when scope is None
       [stdio/local-trust] or the empty frozenset [allow-all, D80b]).
    5. Return the filtered, merged shape (design §1.2).

Scope-filtering rule (design §1.4, uniform, block-level, no partial redaction):
    If a block-entry references ANY column not in scope, drop the WHOLE entry.
    Never emit a partially-redacted predicate string.

--- Security-review amendment (fail-open fixes, post-QA) ---------------------

Three fail-open holes were found in the free-text predicate scan and fixed:

FIX 1 — scan vocabulary is the REAL introspected column universe, not the
    catalog's `columns:` documentation block. A column can exist in the live
    database (e.g. `SSN`) without being documented in the catalog YAML (design
    §1.2's "column not in catalog's columns: block" case) — a rule/ambiguity
    naming such a column must still be recognized and scope-checked, or it is
    invisible to the scan and leaks. `own_column_names` is therefore derived
    from the caller-supplied `introspected_columns` (the actual
    `system.columns` result for this table, not the catalog's authored
    subset), and the cross-table `global_index` is built from the caller-
    supplied `introspected_schema` (the `system.columns`-backed
    `{database.table: {column: type}}` universe — see app/catalog.py's
    `get_catalog_schema()`) rather than from the catalog's per-table
    `columns:` blocks.

FIX 2 — `rules[].predicate` is now parsed with sqlglot (ClickHouse dialect,
    expression-level parse) instead of the vocabulary-scoped regex, and is
    FAIL-CLOSED: an unparseable predicate, or any extracted column identifier
    that cannot be resolved to a known column anywhere in the introspected
    universe (own table ∪ every other table), causes the WHOLE rule to be
    dropped — never kept on uncertainty. This closes the "own-table-only"
    asymmetry the previous implementation had versus `ambiguities[]` (a rule
    predicating on another table's out-of-scope column, e.g. an `employee`
    rule referencing `payroll.Amount` by bare name, used to be invisible to
    the scan). Residual/tradeoff: a predicate that isn't valid standalone SQL
    (rare in practice — rules are authored as SQL expression fragments, unlike
    `ambiguities[].resolves_to`, which is free prose) is dropped rather than
    kept; this is a usability cost accepted deliberately in exchange for never
    fail-opening on an unverifiable predicate.

FIX 3 — `ambiguities[].resolves_to` free-text scan now fails closed on a
    cross-table name COLLISION. Previously, when a bare identifier in the
    prose matched a column name that exists in >= 2 OTHER catalogued tables
    (a real, live condition — e.g. `EmployeeCode` collides across 6 real
    tables, `ClientCode` across 9), the token was treated as unresolvable and
    silently ignored (erring toward *keeping* the entry). It is now resolved
    to ALL candidate tables at once; if the referenced column is out of scope
    on ANY candidate, the reference counts as out-of-scope (over-drop is the
    safe direction — see `_resolve_token`).

Column-reference extraction inside `ambiguities[].resolves_to` deliberately
still uses a lightweight, vocabulary-scoped identifier scan rather than a full
sqlglot parse (several `resolves_to` entries are natural-language/formula
prose mixed with SQL fragments, e.g. "payroll gross earnings = SUM(Amount)
WHERE RegisterType = 'EARN'", not valid standalone SQL a parser could reliably
consume). Note this scan also matches identifiers that happen to appear inside
string literals in the prose — over-matching in this direction is safe (it can
only cause an entry to be dropped that didn't strictly need to be, never the
reverse), so no literal-stripping pre-pass is applied.
"""

from __future__ import annotations

import re
from typing import Any

import sqlglot
import sqlglot.expressions as sqlglot_exp

# ---------------------------------------------------------------------------
# Identifier extraction helpers
# ---------------------------------------------------------------------------

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _build_global_column_index(flat_schema: dict[str, dict[str, Any]]) -> dict[str, set[str]]:
    """Return {column_name: {"database.table", ...}} across every table in *flat_schema*.

    *flat_schema* is a flat `{"database.table": {column_name: ...}, ...}` map —
    e.g. `app.catalog.get_catalog_schema()`'s introspected `{col: type}` view
    (FIX 1: the authoritative source, when available) or, as a fallback for
    callers that don't supply an introspected universe, a projection of the
    catalog's own `columns:` blocks (see `build_table_schema_response`).

    Used to resolve a cross-table column reference inside free-text
    `rules[].predicate` / `ambiguities[].resolves_to` entries (e.g. "payroll
    gross earnings = SUM(Amount)..." inside employee.yaml, where `Amount`
    belongs to `payroll`, not `employee`). A column name that maps to more
    than one table is a real, live collision (FIX 3) — see `_resolve_token`.
    """
    index: dict[str, set[str]] = {}
    for qualified_key, columns in flat_schema.items():
        if not isinstance(columns, dict):
            continue
        for col_name in columns:
            index.setdefault(col_name, set()).add(qualified_key)
    return index


def _resolve_token(
    token: str,
    own_qualified_key: str,
    own_column_names: set[str],
    global_index: dict[str, set[str]],
) -> set[str] | None:
    """Resolve a bare identifier *token* to the qualified column name(s) it
    could refer to, or None if it matches no known column anywhere.

    Returns:
      - `{own_qualified_key}.{token}` (a 1-element set) if *token* is one of
        the current table's own (introspected) columns — own-table columns
        take priority over any cross-table match of the same name.
      - `{"other_table.token", ...}` (the FULL candidate set) if *token*
        names a column in one or more OTHER tables. A collision across >= 2
        tables resolves to ALL candidates at once (FIX 3) rather than being
        silently treated as "not a reference" — the caller then fails closed
        if ANY candidate is out of scope (over-drop is the safe direction for
        an ambiguous identifier).
      - None if *token* does not match any known column in the introspected
        universe at all — genuinely not a column reference (e.g. an English
        prose word, a SQL keyword, a function name). Callers that scan free
        SQL (FIX 2, `rules[]`) must fail closed on None; callers that scan
        free prose (`ambiguities[]`) treat None as "not a reference", exactly
        as before, since most prose tokens are legitimately not columns.
    """
    if token in own_column_names:
        return {f"{own_qualified_key}.{token}"}
    candidates = global_index.get(token)
    if candidates:
        return {f"{tbl}.{token}" for tbl in candidates}
    return None


def _referenced_columns(
    text: str | None,
    own_qualified_key: str,
    own_column_names: set[str],
    global_index: dict[str, set[str]],
) -> set[str]:
    """Return the set of "database.table.column" strings referenced in *text*.

    Case-sensitive exact match against known column names (D70) — no
    case-insensitive fallback. Used for `ambiguities[].resolves_to` free-text
    prose (see module docstring for why this stays regex-based rather than
    sqlglot). Every token in *text* that resolves (via `_resolve_token`) to
    one or more known columns contributes those qualified names; tokens that
    match no known column anywhere are simply not references (they are
    ordinary prose words, not fail-closed here — see `_resolve_token`).
    """
    if not text:
        return set()

    referenced: set[str] = set()
    for token in _IDENTIFIER_RE.findall(text):
        resolved = _resolve_token(token, own_qualified_key, own_column_names, global_index)
        if resolved:
            referenced |= resolved
    return referenced


def _in_scope(qualified_columns: set[str], scope: frozenset[str]) -> bool:
    """True iff every qualified column name is in *scope* (vacuously true if empty)."""
    return qualified_columns.issubset(scope)


# ---------------------------------------------------------------------------
# rules[].predicate — sqlglot-based extraction (FIX 2)
# ---------------------------------------------------------------------------


def _extract_predicate_column_tokens(predicate: str | None) -> set[str] | None:
    """Parse *predicate* as a ClickHouse SQL expression and return the bare
    column identifiers it references.

    Returns:
      - `set()` if *predicate* is empty/missing (no reference at all).
      - the set of `Column` node names found in the parsed expression.
      - `None` if *predicate* could not be parsed at all — the caller MUST
        treat this as fail-closed (drop the rule), never keep on uncertainty
        (design §1.4 / FIX 2).

    Only genuine `sqlglot.exp.Column` AST nodes are counted — function names
    (`SUM(...)`), string/numeric literals, and keywords are never mistaken
    for column references, unlike the regex scan used for `ambiguities[]`.
    """
    if not predicate or not predicate.strip():
        return set()
    try:
        ast = sqlglot.parse_one(
            predicate,
            dialect="clickhouse",
            error_level=sqlglot.ErrorLevel.RAISE,
        )
    except Exception:
        return None
    if ast is None:
        return None
    return {
        col_node.name
        for col_node in ast.find_all(sqlglot_exp.Column)
        if col_node.name
    }


# ---------------------------------------------------------------------------
# Column-list merge
# ---------------------------------------------------------------------------

_COLUMN_OVERLAY_FIELDS = (
    "description",
    "synonyms",
    "unit",
    "values",
    "observed_values",
)


def _merge_columns(
    introspected_columns: list[dict[str, Any]],
    catalog_columns: dict[str, Any],
) -> list[dict[str, Any]]:
    """Merge introspection {name, type, comment} with the catalog's per-column overlay.

    Introspection fields are authoritative for name/type/comment (unchanged from
    today's get_table_schema). Everything else is the catalog overlay, defaulted
    to None/False when the column has no catalog entry (design §1.2 note: a
    column present in introspection but absent from the catalog's `columns:`
    block still returns with introspection fields only + nulled overlay fields).
    """
    merged: list[dict[str, Any]] = []
    for col in introspected_columns:
        name = col["name"]
        overlay = catalog_columns.get(name)
        entry: dict[str, Any] = {
            "name": name,
            "type": col["type"],
            "comment": col.get("comment") or "",
        }
        if isinstance(overlay, dict):
            for field in _COLUMN_OVERLAY_FIELDS:
                entry[field] = overlay.get(field)
            entry["client_defined"] = bool(overlay.get("client_defined", False))
            entry["sensitive"] = bool(overlay.get("sensitive", False))
        else:
            for field in _COLUMN_OVERLAY_FIELDS:
                entry[field] = None
            entry["client_defined"] = False
            entry["sensitive"] = False
        merged.append(entry)
    return merged


# ---------------------------------------------------------------------------
# Table-level block filtering
# ---------------------------------------------------------------------------


def _filter_column_vector(
    names: list[str] | None,
    qualified_key: str,
    scope: frozenset[str],
) -> list[str] | None:
    """grain / primary_key: a single vector representing one fact (uniqueness).

    A partial vector would misrepresent that fact (design §1.4: "a partial
    grain vector would be actively misleading"), so the WHOLE vector is dropped
    (-> None) if ANY of its columns is out of scope.
    """
    if not names:
        return names
    referenced = {f"{qualified_key}.{n}" for n in names}
    return names if _in_scope(referenced, scope) else None


def _filter_join_keys(
    join_keys: list[dict[str, Any]] | None,
    qualified_key: str,
    scope: frozenset[str],
) -> list[dict[str, Any]] | None:
    """join_keys[]: drop any entry whose own-side `column` is out of scope."""
    if join_keys is None:
        return None
    return [jk for jk in join_keys if f"{qualified_key}.{jk.get('column')}" in scope]


def _filter_measures(
    measures: list[dict[str, Any]] | None,
    qualified_key: str,
    scope: frozenset[str],
) -> list[dict[str, Any]] | None:
    """measures[]: drop any measure whose `column` is out of scope."""
    if measures is None:
        return None
    return [m for m in measures if f"{qualified_key}.{m.get('column')}" in scope]


def _filter_rules(
    rules: list[dict[str, Any]] | None,
    qualified_key: str,
    own_column_names: set[str],
    global_index: dict[str, set[str]],
    scope: frozenset[str],
) -> list[dict[str, Any]] | None:
    """rules[]: drop any rule whose `predicate` references an out-of-scope column.

    FIX 2 (security review): the predicate is parsed with sqlglot rather than
    the vocabulary-scoped regex, and resolution is fail-closed in two ways:
      - an unparseable predicate drops the rule (never kept on uncertainty).
      - a column identifier that cannot be resolved to ANY known column in
        the introspected universe (own table ∪ every other table) drops the
        rule (never silently ignored, unlike the `ambiguities[]` prose scan
        where an unmatched token is legitimately just a non-column word).
      - a resolved reference (own-table or cross-table, including collisions
        across >= 2 tables — see `_resolve_token`) is scope-checked exactly
        like every other block: any referenced column out of scope drops the
        whole rule.
    """
    if rules is None:
        return None
    kept: list[dict[str, Any]] = []
    for rule in rules:
        tokens = _extract_predicate_column_tokens(rule.get("predicate"))
        if tokens is None:
            # Unparseable predicate — fail closed (FIX 2): drop, never keep.
            continue

        referenced: set[str] = set()
        unresolvable = False
        for token in tokens:
            resolved = _resolve_token(token, qualified_key, own_column_names, global_index)
            if resolved is None:
                # A column identifier that matches nothing in the entire
                # introspected universe cannot be verified — fail closed.
                unresolvable = True
                break
            referenced |= resolved
        if unresolvable:
            continue

        if _in_scope(referenced, scope):
            kept.append(rule)
    return kept


def _filter_ambiguities(
    ambiguities: list[dict[str, Any]] | None,
    qualified_key: str,
    own_column_names: set[str],
    global_index: dict[str, set[str]],
    scope: frozenset[str],
) -> list[dict[str, Any]] | None:
    """ambiguities[]: filter `resolves_to` entries individually; drop the whole
    ambiguity if nothing is left to disambiguate to.

    `resolves_to` entries can name a column in another table (e.g. a "salary"
    ambiguity resolving partly to payroll.Amount inside employee.yaml), so
    cross-table resolution (`global_index`) is always enabled here. FIX 3: a
    token that collides across >= 2 other tables now resolves to ALL of them
    (fail-closed on the collision) rather than being silently treated as no
    reference.
    """
    if ambiguities is None:
        return None
    kept: list[dict[str, Any]] = []
    for amb in ambiguities:
        resolves_to = amb.get("resolves_to") or []
        surviving = []
        for candidate in resolves_to:
            referenced = _referenced_columns(
                str(candidate), qualified_key, own_column_names, global_index
            )
            if _in_scope(referenced, scope):
                surviving.append(candidate)
        if surviving:
            new_amb = dict(amb)
            new_amb["resolves_to"] = surviving
            kept.append(new_amb)
    return kept


def _filter_temporal(
    temporal: dict[str, Any] | None,
    qualified_key: str,
    scope: frozenset[str],
) -> dict[str, Any] | None:
    """temporal.dimensions[]: drop any dimension whose date/range columns are out
    of scope; drop `default_pin` if it named a dimension that got dropped.
    """
    if temporal is None:
        return None

    dimensions = temporal.get("dimensions") or []
    surviving_names: set[str] = set()
    surviving_dims = []
    for dim in dimensions:
        cols: list[str] = []
        if dim.get("date_col"):
            cols.append(dim["date_col"])
        rng = dim.get("range")
        if isinstance(rng, dict):
            if rng.get("start_col"):
                cols.append(rng["start_col"])
            if rng.get("end_col"):
                cols.append(rng["end_col"])
        referenced = {f"{qualified_key}.{c}" for c in cols}
        if _in_scope(referenced, scope):
            surviving_dims.append(dim)
            if dim.get("name"):
                surviving_names.add(dim["name"])

    default_pin = temporal.get("default_pin")
    if default_pin is not None and default_pin not in surviving_names:
        default_pin = None

    return {"dimensions": surviving_dims, "default_pin": default_pin}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_table_schema_response(
    *,
    database: str,
    table: str,
    introspected_columns: list[dict[str, Any]],
    catalog: dict[str, dict[str, Any]],
    scope: frozenset[str] | None,
    introspected_schema: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build the merged (+ scope-filtered) getTableSchema response (design §1.2).

    `catalog` is the FULL semantic catalog dict (all tables, keyed
    "database.table"), needed so ambiguities/rules can resolve cross-table
    column references (see `_filter_ambiguities` / `_filter_rules`). `scope`
    is the caller's `current_scope` value: None (stdio/local-trust, no
    filtering), empty frozenset (allow-all, D80b, no filtering), or a
    non-empty frozenset (enforce).

    `introspected_schema` (FIX 1, security review): the REAL, live
    `system.columns`-backed `{"database.table": {column: type}}` universe for
    EVERY table — e.g. `app.catalog.get_catalog_schema()` — used as the
    authoritative vocabulary for the cross-table free-text scan
    (`rules[].predicate`, `ambiguities[].resolves_to`). This is deliberately
    NOT the catalog's own `columns:` documentation blocks: a column can be a
    real DB column that is undocumented in the catalog (e.g. `SSN`), and a
    rule/ambiguity naming it must still be recognized — using only catalog-
    documented columns as the scan vocabulary makes such a reference
    invisible and lets it leak. When omitted (e.g. unit tests exercising this
    function directly without a live introspected universe), the cross-table
    index falls back to a projection of the catalog's own `columns:` blocks,
    matching this function's pre-fix behaviour for callers that don't have a
    richer universe available. The CURRENT table's own vocabulary is always
    taken from `introspected_columns` (never the catalog `columns:` block),
    regardless of whether `introspected_schema` is supplied, since that
    parameter is already the real per-table introspection result.

    Does NOT set `catalog_sha` — the caller (app/service.py) attaches that from
    `app.semantic_catalog.loader.get_catalog_sha()`.
    """
    qualified_key = f"{database}.{table}"
    entry = catalog.get(qualified_key)
    catalogued = entry is not None

    catalog_columns: dict[str, Any] = entry.get("columns", {}) if catalogued else {}

    # FIX 1: the free-text scan's OWN-table vocabulary is the table's REAL
    # introspected columns, not the catalog's documentation subset.
    own_column_names = {c["name"] for c in introspected_columns}

    merged_columns = _merge_columns(introspected_columns, catalog_columns)

    description = entry.get("description") if catalogued else None
    grain = entry.get("grain") if catalogued else None
    primary_key = entry.get("primary_key") if catalogued else None
    temporal = entry.get("temporal") if catalogued else None
    join_keys = entry.get("join_keys") if catalogued else None
    measures_raw = entry.get("measures") if catalogued else None
    rules = entry.get("rules") if catalogued else None
    ambiguities = entry.get("ambiguities") if catalogued else None

    # measures is authored as a {name: {column, agg, defined_over}} mapping in the
    # YAML; the response shape (design §1.2) is a list with `name` folded in.
    measures: list[dict[str, Any]] | None
    if isinstance(measures_raw, dict):
        measures = [{"name": name, **defn} for name, defn in measures_raw.items()]
    else:
        measures = None

    enforce = scope is not None and len(scope) > 0

    if enforce:
        merged_columns = [
            c for c in merged_columns if f"{qualified_key}.{c['name']}" in scope
        ]
        if catalogued:
            grain = _filter_column_vector(grain, qualified_key, scope)
            primary_key = _filter_column_vector(primary_key, qualified_key, scope)
            join_keys = _filter_join_keys(join_keys, qualified_key, scope)
            measures = _filter_measures(measures, qualified_key, scope)

            # FIX 1: build the cross-table scan vocabulary from the REAL
            # introspected universe when the caller supplied one; otherwise
            # fall back to a projection of the catalog's own `columns:`
            # blocks (pre-fix behaviour, kept for callers/tests that only
            # have the catalog available).
            global_schema_source = (
                introspected_schema
                if introspected_schema is not None
                else {key: (e.get("columns") or {}) for key, e in catalog.items()}
            )
            global_index = _build_global_column_index(global_schema_source)

            rules = _filter_rules(rules, qualified_key, own_column_names, global_index, scope)
            ambiguities = _filter_ambiguities(
                ambiguities, qualified_key, own_column_names, global_index, scope
            )
            temporal = _filter_temporal(temporal, qualified_key, scope)

    return {
        "database": database,
        "table": table,
        "catalogued": catalogued,
        "description": description,
        "grain": grain,
        "temporal": temporal,
        "primary_key": primary_key,
        "join_keys": join_keys,
        "columns": merged_columns,
        "measures": measures,
        "rules": rules,
        "ambiguities": ambiguities,
    }
