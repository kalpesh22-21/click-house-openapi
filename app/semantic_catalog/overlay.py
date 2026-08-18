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
# mcp_projection.hidden_columns — catalog-authored, UNCONDITIONAL projection
# ---------------------------------------------------------------------------


def _hidden_columns(entry: dict[str, Any] | None) -> set[str]:
    """Return the catalog entry's ``mcp_projection.hidden_columns`` as a set.

    These are columns the catalog author has decided the model must NEVER see —
    RLS-enforced physical columns (e.g. ``client_code`` / ``proc_center``) and
    internal engine columns (e.g. a ReplacingMergeTree ``version``). Unlike
    column_scope, this is not a per-caller decision: it is stripped from the
    schema response UNCONDITIONALLY (every caller, every transport, including the
    scope-disabled stdio/local-trust path). Returns an empty set when the entry
    has no ``mcp_projection`` block or an empty/absent ``hidden_columns`` list.
    """
    if not entry:
        return set()
    projection = entry.get("mcp_projection")
    if not isinstance(projection, dict):
        return set()
    hidden = projection.get("hidden_columns")
    if not isinstance(hidden, list):
        return set()
    return {str(name) for name in hidden}


def _sanitize_join_key(
    jk: dict[str, Any], hidden: set[str]
) -> dict[str, Any]:
    """Return a copy of a join_keys entry safe to emit to the model (A-H2).

    Strips ``implicit_join_scope`` entirely — it holds the RLS tenant columns
    ([client_code, proc_center]) that the row policy applies server-side and the
    model must never see. Also defensively drops any hidden-column NAME that
    might appear inside the ``join_on`` clause fragments, so a stray reference
    can't leak a hidden name. ``column`` / ``joins`` are left intact (an entry
    whose own-side ``column`` is hidden is filtered out earlier).
    """
    sanitized = {k: v for k, v in jk.items() if k != "implicit_join_scope"}
    join_on = sanitized.get("join_on")
    if hidden and isinstance(join_on, list):
        sanitized["join_on"] = [
            clause
            for clause in join_on
            if not _clause_names_hidden(clause, hidden)
        ]
    return sanitized


def _clause_names_hidden(clause: Any, hidden: set[str]) -> bool:
    """True if *clause* (a join_on string fragment) references a hidden column name."""
    if not isinstance(clause, str):
        return False
    tokens = set(_IDENTIFIER_RE.findall(clause))
    return bool(tokens & hidden)


def _strip_hidden_from_temporal(
    temporal: dict[str, Any] | None, hidden: set[str]
) -> dict[str, Any] | None:
    """Drop any temporal dimension whose date/range columns name a hidden column.

    Defensive (A-H2): temporal dimensions reference visible date columns today,
    but if one ever named a hidden column, drop that dimension (and reset
    ``default_pin`` if it pointed at it) rather than ship the hidden NAME. Runs
    UNCONDITIONALLY, independent of scope enforcement.
    """
    if not hidden or not isinstance(temporal, dict):
        return temporal

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
        if any(c in hidden for c in cols):
            continue
        surviving_dims.append(dim)
        if dim.get("name"):
            surviving_names.add(dim["name"])

    default_pin = temporal.get("default_pin")
    if default_pin is not None and default_pin not in surviving_names:
        default_pin = None

    new_temporal = dict(temporal)
    new_temporal["dimensions"] = surviving_dims
    new_temporal["default_pin"] = default_pin
    return new_temporal


def hidden_columns_for(
    catalog: dict[str, dict[str, Any]], qualified_key: str
) -> set[str]:
    """Return ``mcp_projection.hidden_columns`` for ``qualified_key`` ("db.table").

    Public accessor over :func:`_hidden_columns` so callers outside this module
    (e.g. app/service.py's ``sampleRows`` visible-column projection and the
    ``SELECT *`` scope-violation guard, A-H1) resolve the SAME hidden-column set
    the overlay strips from ``getTableSchema`` — one source of truth. Returns an
    empty set for an uncatalogued table or one without a hidden-columns block.
    """
    return _hidden_columns(catalog.get(qualified_key))


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


# ---------------------------------------------------------------------------
# join_keys[] — singular `column` entries AND templated `column_pattern` entries
# ---------------------------------------------------------------------------
#
# A join_keys entry comes in one of two authoring forms:
#
#   (1) SINGULAR — one physical own-side column:
#         - column: employee_code
#           joins: payroll.employee_code
#           join_on: [employee_code]            # list of clause fragments
#           cardinality: 1:N
#
#   (2) PATTERN (templated) — ONE entry standing in for a contiguous family of
#       near-identical own-side columns that all join the SAME target identically
#       (e.g. employee/payroll's labor_allocation_key_1..labor_allocation_key_20,
#       which are DISTINCT dimension slots but all join labor_allocation.join_key
#       the same way). Authored as:
#         - column_pattern: labor_allocation_key_{n}   # "{n}" is the slot index
#           index_range: [1, 20]                       # inclusive [lo, hi]
#           joins: labor_allocation.join_key
#           join_on: labor_allocation_key_{n} = join_key   # scalar template string
#           cardinality: N:1
#           description: ...
#
# The pattern entry is emitted to the model COMPACTLY — as ONE join_key carrying
# the pattern + range + join_on template + description — and is deliberately NOT
# expanded to its 20 underlying columns (fewer tokens; the model already handles
# "which of 1..20"). Its underlying physical columns (used only for scope
# filtering / hidden sweeps here) are `column_pattern` with "{n}" substituted by
# each index in `index_range`.


def _join_key_columns(jk: dict[str, Any]) -> list[str]:
    """Return the own-side physical column name(s) a join_keys entry references.

    Singular entry -> ``[column]`` (or ``[]`` if malformed / column missing).
    Pattern entry  -> the expanded ``column_pattern`` over the inclusive
    ``index_range`` (e.g. ``labor_allocation_key_1 .. labor_allocation_key_20``).
    Never raises: a pattern entry with a missing/garbage ``index_range`` yields
    an empty list rather than throwing.
    """
    pattern = jk.get("column_pattern")
    if pattern is not None:
        rng = jk.get("index_range")
        if not isinstance(rng, (list, tuple)) or len(rng) != 2:
            return []
        try:
            lo, hi = int(rng[0]), int(rng[1])
        except (TypeError, ValueError):
            return []
        return [str(pattern).replace("{n}", str(i)) for i in range(lo, hi + 1)]
    col = jk.get("column")
    return [col] if col is not None else []


def _filter_join_keys(
    join_keys: list[dict[str, Any]] | None,
    qualified_key: str,
    scope: frozenset[str],
) -> list[dict[str, Any]] | None:
    """join_keys[]: drop any entry whose own-side column(s) are out of scope.

    Singular entry: kept iff its `column` is in scope (unchanged behaviour).
    Pattern entry: kept iff AT LEAST ONE of its underlying columns
    (`column_pattern` expanded over `index_range`) is in scope — dropped only
    when EVERY slot is out of scope. Never throws on a pattern entry that lacks
    a singular `column` (design note above: `_join_key_columns` tolerates both
    forms and any malformed range).
    """
    if join_keys is None:
        return None
    kept: list[dict[str, Any]] = []
    for jk in join_keys:
        cols = _join_key_columns(jk)
        if any(f"{qualified_key}.{c}" in scope for c in cols):
            kept.append(jk)
    return kept


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
    columns_filter: list[str] | None = None,
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

    `columns_filter` (M3) narrows the emitted `columns` array to the named
    columns only — the affordance a model needs when the runtime's schema
    preview showed a column as name/type only and it wants that column's full
    documentation. Semantics:

      * The filter is applied LAST — after the `mcp_projection.hidden_columns`
        projection AND after scope enforcement — so it can only ever REMOVE
        entries from the already-authorized column list. It is structurally
        incapable of reaching a column that projection or scope hides; there is
        no code path on which a filtered response contains a column an
        unfiltered response would not.
      * Matching is exact and CASE-SENSITIVE against the catalog's own spelling,
        matching the D70 merge join — the model copies names out of the schema
        skeleton, which carries that spelling.
      * `None` and `[]` BOTH mean "all columns". An empty list is what a
        confused model sends when it means "no narrowing"; reading it as "no
        columns" would return a husk (base sections with an empty column array)
        that looks like a table with no columns and would send the model
        chasing a non-problem.
      * Requested names that do not appear in the final authorized column list
        are echoed back under `columns_not_found` (present ONLY when a
        narrowing filter was actually applied, so an unfiltered call's response
        shape is byte-identical to before). That bucket is deliberately
        UNDIFFERENTIATED: a name that does not exist, a name hidden by
        `mcp_projection`, and a name outside the caller's scope are
        indistinguishable in it. Echoing back only the caller's own supplied
        strings discloses nothing the caller did not already know — whereas a
        "hidden" vs "unknown" distinction would confirm the existence of an
        RLS/tenancy column whose NAME this module strips from every other
        surface unconditionally (A-H1/A-H2).

    The filter narrows `columns` ONLY. Every base section (description, grain,
    temporal, primary_key, join_keys, measures, rules, ambiguities) always
    rides complete — they are the table-level semantics the model needs to use
    whichever columns it asked about, and withholding them would make a
    narrowed fetch strictly less useful than a full one.

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

    # mcp_projection.hidden_columns (catalog-authored): drop these columns
    # UNCONDITIONALLY — independent of column_scope, on every transport — so the
    # model never sees the RLS-enforced physical columns (client_code /
    # proc_center) or internal engine columns (version). Applied AFTER the
    # introspection merge so a physically-present, catalog-stripped column is
    # removed even though introspection surfaced it. Also swept out of the
    # grain / primary_key / join_keys own-side vectors defensively — they should
    # never name a hidden column, but a stray reference must not leak the name.
    hidden = _hidden_columns(entry)
    if hidden:
        merged_columns = [c for c in merged_columns if c["name"] not in hidden]
        if isinstance(grain, list):
            grain = [g for g in grain if g not in hidden]
        if isinstance(primary_key, list):
            primary_key = [p for p in primary_key if p not in hidden]
        if isinstance(join_keys, list):
            # Drop an entry only if EVERY own-side column it references is hidden.
            # Singular entry -> "column in hidden" (unchanged). Pattern entry ->
            # dropped only when all its expanded slots are hidden (never true for
            # labor_allocation_key_1..20, which are not RLS columns) — so the
            # compact pattern entry is not accidentally swept out. A malformed
            # entry with no resolvable columns is kept (matches prior behaviour).
            join_keys = [
                jk
                for jk in join_keys
                if not (
                    (cols := _join_key_columns(jk)) and all(c in hidden for c in cols)
                )
            ]
        # Defensive: a measure / temporal dimension must never reference a hidden
        # column (none do today), but if one ever did, drop it rather than ship
        # the hidden NAME (A-H2). Applied UNCONDITIONALLY (not only under scope
        # enforcement) so the hidden name never leaks on the stdio/allow-all path.
        if isinstance(measures, list):
            measures = [m for m in measures if m.get("column") not in hidden]
        temporal = _strip_hidden_from_temporal(temporal, hidden)

    # A-H2: strip `implicit_join_scope` (the RLS tenant columns [client_code,
    # proc_center]) from EVERY emitted join_keys entry — on every path, whether
    # or not this table declares hidden_columns — so the hidden RLS column NAMES
    # never ship inside join metadata. The row policy applies the tenant
    # predicate server-side; the model neither needs nor may see these columns.
    if isinstance(join_keys, list):
        join_keys = [_sanitize_join_key(jk, hidden) for jk in join_keys]

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

    # M3 column narrowing — applied LAST, on the fully authorized column list.
    # Everything above this line (hidden-column projection, scope enforcement)
    # has already run, so this can only remove entries, never restore one.
    # `None` and `[]` alike mean "all columns" (see the docstring).
    columns_not_found: list[str] | None = None
    if columns_filter:
        requested: list[str] = []
        for name in columns_filter:
            if name not in requested:
                requested.append(name)
        available = {c["name"] for c in merged_columns}
        # Preserve the merged (introspection position) order rather than the
        # caller's argument order, so the narrowed array reads like a slice of
        # the full response.
        merged_columns = [c for c in merged_columns if c["name"] in requested]
        columns_not_found = [name for name in requested if name not in available]

    response = {
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
    if columns_not_found is not None:
        response["columns_not_found"] = columns_not_found
    return response
