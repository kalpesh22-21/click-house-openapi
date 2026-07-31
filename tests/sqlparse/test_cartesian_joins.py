"""
Unit tests for the cartesian-join guardrail (app/sqlparse/provenance.py).

Layer: 1 — Unit (pure logic; inputs are SQL strings; no ClickHouse connection).

`reject_cartesian_joins(sql)` rejects a query that combines two PHYSICAL BASE
TABLES without a join condition relating them — a cartesian product: CROSS JOIN, a
comma join, a bare JOIN with no ON/USING, or a JOIN whose ON is a constant-true
literal (`ON 1=1` / `ON true`). Cross joins where one side is a subquery / CTE /
table function / VALUES are EXEMPT (the common `CROSS JOIN (SELECT …)` parameter
pattern). The virtual-name (CTE) exemption is resolved PER-SCOPE so a decoy alias
cannot mask a real base table; the `FINAL` modifier is unwrapped so it cannot hide
one; and parenthesized join groups (`FROM (a CROSS JOIN b)`) are recursed into.

The detection rule was verified against sqlglot's ClickHouse dialect (the version
pinned in this repo, 30.12.x). A parse failure (e.g. the NATURAL JOIN ParseError in
this dialect) is a no-op here — it is handled by the caller's fail-closed provenance
parse (PARSE_FAILED_CLOSED).
"""

from __future__ import annotations

import pytest

from app.sqlparse import CartesianJoinForbiddenError, reject_cartesian_joins

# ---------------------------------------------------------------------------
# REJECT cases: two physical base tables cross-joined with no condition.
# Each entry: (sql, expected_table_a, expected_table_b)
# ---------------------------------------------------------------------------

_REJECT_CASES: list[tuple[str, str, str]] = [
    # Explicit CROSS JOIN of two base tables.
    ("SELECT * FROM employees CROSS JOIN departments", "employees", "departments"),
    # Comma join normalizes to a CROSS Join node.
    ("SELECT * FROM employees, departments", "employees", "departments"),
    # Bare JOIN with no ON/USING (kind=None) — still a cartesian product.
    ("SELECT * FROM employees JOIN departments", "employees", "departments"),
    # Three-way comma join: first violation is (a, b).
    ("SELECT * FROM a, b, c", "a", "b"),
    # A proper join followed by a cartesian join: c multiplies base tables a/b.
    (
        "SELECT * FROM a JOIN b ON a.x = b.x CROSS JOIN c",
        "a",
        "c",
    ),
    # Cartesian product buried inside a subquery scope (checked independently).
    (
        "SELECT * FROM ("
        "  SELECT e.id FROM employees e CROSS JOIN departments d"
        ") t",
        "employees",
        "departments",
    ),
]


@pytest.mark.parametrize(
    "sql, table_a, table_b",
    _REJECT_CASES,
    ids=[
        "cross-join-two-base-tables",
        "comma-join-two-base-tables",
        "bare-join-no-on",
        "three-way-comma-join",
        "proper-join-then-cross-join",
        "cartesian-inside-subquery",
    ],
)
def test_cartesian_join_rejected(sql: str, table_a: str, table_b: str) -> None:
    with pytest.raises(CartesianJoinForbiddenError) as exc_info:
        reject_cartesian_joins(sql)
    exc = exc_info.value
    # Both offending base-table names are carried on the exception.
    assert exc.table_a == table_a
    assert exc.table_b == table_b
    # The actionable message NAMES both tables so the model can self-correct.
    assert table_a in exc.message
    assert table_b in exc.message
    assert "cartesian" in exc.message.lower()


# ---------------------------------------------------------------------------
# ALLOW cases: no cartesian product between two physical base tables.
# ---------------------------------------------------------------------------

_ALLOW_CASES: list[str] = [
    # Proper join with an ON condition.
    "SELECT * FROM a JOIN b ON a.id = b.id",
    # Proper join with a USING condition.
    "SELECT * FROM a JOIN b USING (id)",
    # CROSS JOIN of a base table with a scalar subquery (parameter pattern).
    "SELECT * FROM employees CROSS JOIN (SELECT now() AS asof) p",
    # Comma join of a base table with a subquery.
    "SELECT * FROM employees, (SELECT 1 AS x) p",
    # CTE side is virtual, not a physical base table.
    "WITH p AS (SELECT 1 AS x) SELECT * FROM employees CROSS JOIN p",
    # Table function (numbers(10)) — empty .name, not a base table.
    "SELECT * FROM employees CROSS JOIN numbers(10)",
    # Single-table select — no join at all.
    "SELECT * FROM employees",
    # A scalar subquery in the SELECT list is not a join.
    "SELECT (SELECT count() FROM departments) AS n FROM employees",
    # Constant-only select — no FROM, no join.
    "SELECT now() AS asof",
]


@pytest.mark.parametrize("sql", _ALLOW_CASES)
def test_non_cartesian_query_allowed(sql: str) -> None:
    # Must not raise.
    reject_cartesian_joins(sql)


# ---------------------------------------------------------------------------
# Parse-failure fail-open (deferred to the caller's fail-closed provenance parse).
# ---------------------------------------------------------------------------


def test_unparseable_sql_is_a_noop() -> None:
    # NATURAL JOIN raises a ParseError in the ClickHouse dialect; reject_cartesian_joins
    # swallows it (the caller's own provenance parse fail-closes on unparseable SQL,
    # so we do not double-raise a different error here).
    reject_cartesian_joins("SELECT * FROM a NATURAL JOIN b")
    # Syntactic garbage is likewise a no-op here.
    reject_cartesian_joins("this is not sql at all !!!")
    # Empty / whitespace input is a no-op too.
    reject_cartesian_joins("")
    reject_cartesian_joins("   ")


def test_message_matches_the_documented_shape() -> None:
    """Spot-check the exact author-controlled message the model receives."""
    with pytest.raises(CartesianJoinForbiddenError) as exc_info:
        reject_cartesian_joins("SELECT * FROM employees CROSS JOIN departments")
    msg = exc_info.value.message
    assert "employees" in msg and "departments" in msg
    assert "cartesian product" in msg
    assert "Add a real ON/USING condition" in msg
    # The message must NOT coach the subquery escape-hatch as a cartesian workaround.
    assert "subquery" not in msg.lower()
    assert "CROSS JOIN (SELECT" not in msg


def test_message_uses_db_qualified_names_when_present() -> None:
    """When a database is present on the offending tables, the message names them
    DB-qualified (so `system.one`, not a bare `one`) while the `.table_a`/`.table_b`
    attributes stay bare."""
    with pytest.raises(CartesianJoinForbiddenError) as exc_info:
        reject_cartesian_joins("SELECT * FROM db1.a CROSS JOIN db2.b")
    exc = exc_info.value
    assert exc.table_a == "a" and exc.table_b == "b"  # bare attribute contract
    assert "db1.a" in exc.message and "db2.b" in exc.message


# ===========================================================================
# ADVERSARIAL PASS (QA) — edge cases the first test set did not cover.
# Every case below was verified against the real detector before being pinned.
# ===========================================================================

# ---------------------------------------------------------------------------
# Extra MUST-REJECT: forms of "two base tables, no join condition" that the
# original suite did not exercise.  A regression that lets any of these through
# is a real cartesian-product bypass.
# ---------------------------------------------------------------------------

_ADVERSARIAL_REJECT_CASES: list[tuple[str, str, str]] = [
    # Aliased bare JOIN with no ON — aliases must not hide the base tables.
    ("SELECT * FROM a e JOIN b d", "a", "b"),
    # Outer joins with no ON/USING (they still parse to a condition-less Join).
    ("SELECT * FROM a LEFT JOIN b", "a", "b"),
    ("SELECT * FROM a RIGHT JOIN b", "a", "b"),
    ("SELECT * FROM a FULL JOIN b", "a", "b"),
    ("SELECT * FROM a INNER JOIN b", "a", "b"),
    # Cartesian buried inside a CTE body — each SELECT scope is checked.
    ("WITH t AS (SELECT * FROM a, b) SELECT * FROM t", "a", "b"),
    # Both sides of a UNION carry their own base-table cartesian; the first wins.
    ("SELECT * FROM a, b UNION ALL SELECT * FROM c, d", "a", "b"),
    # Self cross-join: same table twice, two aliases, no condition.
    ("SELECT * FROM a x CROSS JOIN a y", "a", "a"),
    # Cartesian two subquery-levels deep.
    ("SELECT * FROM (SELECT * FROM (SELECT * FROM x, y) t) u", "x", "y"),
    # Cartesian inside a scalar subquery in the SELECT list.
    ("SELECT (SELECT count() FROM x, y) FROM a", "x", "y"),
    # Cartesian inside a WHERE IN-subquery.
    ("SELECT * FROM a WHERE id IN (SELECT id FROM x, y)", "x", "y"),
    # Cartesian inside an aliased FROM-subquery that is itself a join side.
    ("SELECT * FROM a JOIN (SELECT * FROM x, y) t ON a.id = t.id", "x", "y"),
    # Comma join followed by an explicit CROSS JOIN — first pair (a,b) violates.
    ("SELECT * FROM a, b CROSS JOIN c", "a", "b"),
    # Database-qualified base tables are still base tables.
    ("SELECT * FROM db.a CROSS JOIN db.b", "a", "b"),
    # Backtick-quoted identifiers.
    ("SELECT * FROM `a`, `b`", "a", "b"),
    # Trailing semicolon must not defeat detection.
    ("SELECT * FROM a CROSS JOIN b;", "a", "b"),
    # SAMPLE modifier on a source keeps it a base table (SAMPLE is a Table arg).
    ("SELECT * FROM a SAMPLE 0.1 CROSS JOIN b", "a", "b"),
]


@pytest.mark.parametrize("sql, table_a, table_b", _ADVERSARIAL_REJECT_CASES)
def test_adversarial_cartesian_rejected(sql: str, table_a: str, table_b: str) -> None:
    with pytest.raises(CartesianJoinForbiddenError) as exc_info:
        reject_cartesian_joins(sql)
    assert exc_info.value.table_a == table_a
    assert exc_info.value.table_b == table_b


# ---------------------------------------------------------------------------
# Extra MUST-ALLOW: shapes that could be false-positived if the base-table
# detection were too loose.  A raise here breaks a legitimate query.
# ---------------------------------------------------------------------------

_ADVERSARIAL_ALLOW_CASES: list[str] = [
    # Three properly-joined base tables.
    "SELECT * FROM a JOIN b ON a.x = b.x JOIN c ON b.y = c.y",
    # CROSS JOIN of two subqueries — neither side is a physical base table.
    "SELECT * FROM (SELECT 1 AS x) p CROSS JOIN (SELECT 2 AS y) q",
    # Comma join of three subqueries.
    "SELECT * FROM (SELECT 1 AS x) p, (SELECT 2 AS y) q, (SELECT 3 AS z) r",
    # Subquery on the LEFT, base table on the right — only one base table.
    "SELECT * FROM (SELECT 1 AS x) p CROSS JOIN b",
    # Table function on the LEFT — numbers() has an empty .name.
    "SELECT * FROM numbers(10) CROSS JOIN b",
    # VALUES source wrapped as a subquery — not a base table.
    "SELECT * FROM a CROSS JOIN (VALUES (1), (2)) v",
    "SELECT * FROM a, (VALUES (1), (2)) v",
    # A CTE cross-joined with a single base table — one base table only.
    "WITH t AS (SELECT 1 AS x) SELECT * FROM t CROSS JOIN t2",
]


@pytest.mark.parametrize("sql", _ADVERSARIAL_ALLOW_CASES)
def test_adversarial_non_cartesian_allowed(sql: str) -> None:
    reject_cartesian_joins(sql)  # must not raise


# ---------------------------------------------------------------------------
# ClickHouse-specific ARRAY JOIN: NOT a cartesian product — it unnests an array
# COLUMN, so its join source parses as an exp.Column (never an exp.Table).  Must
# be ALLOWED (both the plain and LEFT forms).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM a ARRAY JOIN arr",
        "SELECT * FROM a LEFT ARRAY JOIN arr",
    ],
)
def test_array_join_is_not_cartesian(sql: str) -> None:
    reject_cartesian_joins(sql)  # must not raise


# ---------------------------------------------------------------------------
# CONSTANT-TRUE ON: a trivially-true literal condition does NOT relate the two
# tables — it is a full cartesian product dressed up with a fake condition.  These
# now REJECT (the guard recognises `ON true` / `ON 1=1` / `ON 1` / `ON (1=1)`).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql, table_a, table_b",
    [
        ("SELECT * FROM a JOIN b ON 1 = 1", "a", "b"),
        ("SELECT * FROM a CROSS JOIN b ON 1 = 1", "a", "b"),
        ("SELECT * FROM a CROSS JOIN b ON true", "a", "b"),
        ("SELECT * FROM a CROSS JOIN b ON (1 = 1)", "a", "b"),
        ("SELECT * FROM a CROSS JOIN b ON 1", "a", "b"),
    ],
)
def test_constant_true_on_condition_is_rejected(sql: str, table_a: str, table_b: str) -> None:
    with pytest.raises(CartesianJoinForbiddenError) as exc_info:
        reject_cartesian_joins(sql)
    assert exc_info.value.table_a == table_a
    assert exc_info.value.table_b == table_b


# ---------------------------------------------------------------------------
# NON-true constant / NULL conditions are LEFT ALLOWED: a constant-FALSE or NULL
# ON yields zero rows, not a row-multiplying cartesian, so it is out of scope for
# this guard (it only rejects constant-TRUE / missing conditions).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM a CROSS JOIN b ON false",
        "SELECT * FROM a CROSS JOIN b ON 1 = 0",
        "SELECT * FROM a JOIN b ON NULL",
    ],
)
def test_constant_false_or_null_on_condition_is_allowed(sql: str) -> None:
    reject_cartesian_joins(sql)  # must not raise


# ---------------------------------------------------------------------------
# RESIDUAL GAP (documented, by-design): a general tautology over COLUMNS
# (`ON a.x = a.x`) is NOT collapsed — only constant literals are.  Solving
# arbitrary tautologies is out of scope; this characterization test pins the
# current ALLOW so a future tightening is a deliberate, reviewed change.
# ---------------------------------------------------------------------------

def test_self_equality_on_is_allowed_residual_gap() -> None:
    reject_cartesian_joins("SELECT * FROM a JOIN b ON a.x = a.x")  # currently allowed


# ---------------------------------------------------------------------------
# DECOY-ALIAS bypass (fix #1): a subquery/derived-table alias that COLLIDES with a
# real base-table name must NOT exempt that base table.  The virtual-name
# exemption is scoped per-select (CTEs on this select or an ancestor), and
# FROM-subquery aliases are never added to the exemption set at all.
# ---------------------------------------------------------------------------

def test_decoy_subquery_alias_does_not_exempt_base_table() -> None:
    # `employees` appears once as a derived-table alias inside a scalar subquery and
    # once as the real base table in the outer cartesian. The real base table must
    # still be seen — the cartesian REJECTS.
    sql = (
        "SELECT (SELECT 1 FROM (SELECT 1) AS employees), * "
        "FROM employees CROSS JOIN departments"
    )
    with pytest.raises(CartesianJoinForbiddenError) as exc_info:
        reject_cartesian_joins(sql)
    assert exc_info.value.table_a == "employees"
    assert exc_info.value.table_b == "departments"


def test_decoy_cte_in_unrelated_scope_does_not_exempt_base_table() -> None:
    # `departments` is a CTE defined ONLY inside the WHERE IN-subquery scope; it must
    # not exempt the real `departments` base table in the outer cartesian.
    sql = (
        "SELECT * FROM employees CROSS JOIN departments "
        "WHERE id IN (WITH departments AS (SELECT 1 AS id) SELECT id FROM departments)"
    )
    with pytest.raises(CartesianJoinForbiddenError) as exc_info:
        reject_cartesian_joins(sql)
    assert exc_info.value.table_a == "employees"
    assert exc_info.value.table_b == "departments"


# ---------------------------------------------------------------------------
# PARENTHESIZED join groups (fix #3): `FROM (a CROSS JOIN b)` attaches the inner
# joins to the grouped Table node (no inner Select), so the guard must recurse
# into the sub-group.  Both the top-level group and a group used as a join side
# must be detected.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql, table_a, table_b",
    [
        ("SELECT * FROM (a CROSS JOIN b)", "a", "b"),
        ("SELECT * FROM x JOIN (b CROSS JOIN c) ON x.id = b.id", "b", "c"),
        ("SELECT * FROM (a CROSS JOIN b) CROSS JOIN c", "a", "b"),
    ],
)
def test_parenthesized_join_group_cartesian_rejected(
    sql: str, table_a: str, table_b: str
) -> None:
    with pytest.raises(CartesianJoinForbiddenError) as exc_info:
        reject_cartesian_joins(sql)
    assert exc_info.value.table_a == table_a
    assert exc_info.value.table_b == table_b


# ---------------------------------------------------------------------------
# PASTE JOIN: sqlglot 30.12.0 does NOT support ClickHouse's PASTE JOIN — it
# MISPARSES `FROM a PASTE JOIN b` as `FROM a AS PASTE, b` (a comma join between
# base tables a and b).  The detector therefore REJECTS it.  This is a sqlglot
# limitation surfacing as a (benign) false-positive on an exotic, rarely-used
# join; pinned here to document the actual behavior.
# ---------------------------------------------------------------------------

def test_paste_join_is_misparsed_and_rejected() -> None:
    with pytest.raises(CartesianJoinForbiddenError) as exc_info:
        reject_cartesian_joins("SELECT * FROM a PASTE JOIN b")
    # Misparsed to `a AS PASTE, b` — the detector sees base tables a and b.
    assert exc_info.value.table_a == "a"
    assert exc_info.value.table_b == "b"


# ---------------------------------------------------------------------------
# FINAL modifier (fix #2): sqlglot wraps a table carrying `FINAL` in an
# exp.Final(this=Table(...)) node.  `_unwrap_table_source` now peels exp.Final (and
# exp.Alias, in any order), so a FINAL-modified base table stays visible and the
# cartesian is REJECTED.  FINAL is a common ClickHouse modifier
# (ReplacingMergeTree / CollapsingMergeTree), so this is a realistic bypass vector.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM a FINAL CROSS JOIN b FINAL",
        "SELECT * FROM a FINAL, b",
        "SELECT * FROM a CROSS JOIN b FINAL",
        "SELECT * FROM a FINAL JOIN b",
    ],
)
def test_final_modifier_cartesian_is_rejected(sql: str) -> None:
    with pytest.raises(CartesianJoinForbiddenError) as exc_info:
        reject_cartesian_joins(sql)
    assert exc_info.value.table_a == "a"
    assert exc_info.value.table_b == "b"
