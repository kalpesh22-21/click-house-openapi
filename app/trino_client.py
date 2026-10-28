"""Experimental read-only Trino adapter for the existing logical SQL interface.

The MCP continues to validate ClickHouse-dialect logical SQL. This adapter
transpiles only after validation and resolves physical sources using the YAML
catalog. It does NOT install database row policies or employee reporting views.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from contextlib import suppress, contextmanager
from typing import Any, NamedTuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.optimizer.qualify_tables import qualify_tables
from sqlglot.optimizer.scope import traverse_scope

from app.config import Settings, get_settings
from app.errors import (
    ClickHouseQueryError,
    ClickHouseUnavailableError,
    QueryValidationError,
)
from app.principal import get_current_principal
from app.semantic_catalog.loader import DEFAULT_DATABASE, get_semantic_catalog
from app.shard_routing import resolve_mysql_shard, validate_identifier
from app.trino_errors import rewrite_trino_error
from app.trino_diagnostics import trace_execution, record_physical, record_query_id

logger = logging.getLogger(__name__)


def _close(cursor, conn) -> None:
    # Cleanup must not replace the original query error. Cursor.close cancels
    # unfinished queries, including requests stopped by local result limits.
    for resource in (cursor, conn):
        if resource is not None:
            with suppress(Exception):
                resource.close()


def request_identity(settings: Settings) -> tuple[str, str, str]:
    """Values must come from verified claims, never SQL or client headers."""
    principal = get_current_principal()
    if principal is None:
        raise QueryValidationError(
            "Trino requires an authenticated principal.", "TRINO_IDENTITY_REQUIRED"
        )
    values = []
    for claim in (
        settings.trino_proc_center_claim,
        settings.trino_client_code_claim,
        settings.trino_username_claim,
    ):
        value = principal.claims.get(claim)
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 128
            or any(c in value for c in "|\r\n\x00")
            or not value.isprintable()
        ):
            raise QueryValidationError(
                "Missing or invalid Trino identity claim.", "TRINO_IDENTITY_REQUIRED"
            )
        values.append(value)
    return values[0], values[1], values[2]


def normalize_logical_sql(sql: str) -> str:
    """Map the public Trino database alias to the existing authorization namespace."""
    from app.trino_metadata import PUBLIC_DATABASE

    try:
        statements = sqlglot.parse(sql, read="clickhouse", error_level=ErrorLevel.RAISE)
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            return sql  # The normal statement guard rejects unsupported inputs.
        tree = statements[0]
        changed = False
        for node in tree.walk():
            if (
                isinstance(node, (exp.Table, exp.Column))
                and node.db == PUBLIC_DATABASE
                and not node.catalog
            ):
                node.set("db", exp.to_identifier(DEFAULT_DATABASE))
                changed = True
        return tree.sql(dialect="clickhouse") if changed else sql
    except SqlglotError as exc:
        # Sanitized by construction: code + exception class only, never SQL
        # text, identity claims, or data values.
        logger.error(
            "trino normalize: unparseable logical SQL: code=%s "
            "failure_class=%s",
            "TRINO_UNSUPPORTED_SQL",
            type(exc).__name__,
        )
        raise QueryValidationError(
            "Invalid logical SQL. Check for incomplete SQL or unterminated quotes.", "TRINO_UNSUPPORTED_SQL"
        ) from exc


# --- ClickHouse -> Trino function mapping -----------------------------------
# The MCP validates ClickHouse-dialect logical SQL, so ClickHouse-only
# functions legitimately reach this transpiler. sqlglot has no Trino mapping
# for them and passes their names through VERBATIM (uniqExact renders as
# UNIQEXACT): the query passes validation, dispatches, and only then dies on
# the coordinator with FUNCTION_NOT_FOUND, visible to the caller as the
# generic TRINO_QUERY_ERROR (bp-active-headcount-by-department hit exactly
# this). This pass rewrites the ClickHouse-only functions we can map
# EXACTLY, in place on the routed AST, post-validation and pre-render.
#
# Map discipline: one entry per function, keyed by the case-folded parsed
# name; builders receive the parsed call and return its replacement. An
# ABSENT entry must be handled by the semantics/construct passes; otherwise
# reject before dispatch. Unsupported mapped arities also fail closed.
#
# Inventory (exact-safe mappings only):
#   uniqexact           -> COUNT(DISTINCT x)                            [1 arg]
#   todate              -> CAST(x AS DATE)                              [1 arg]
#   todate32            -> CAST(x AS DATE)                              [1 arg]
#   todatetime          -> type-aware timestamp/epoch mapping (trino_semantics)
#   todatetime64        -> CAST(x AS TIMESTAMP(p))                      [2 args]
#   tofloat64           -> CAST(x AS DOUBLE)                            [1 arg]
#   tostring            -> CAST(x AS VARCHAR)                           [1 arg]
#   touint8             -> truncate toward zero, modulo 256            [1 arg]
#   sumif / avgif       -> type-aware defaults (trino_semantics)
#   multiif             -> CASE WHEN c THEN v ... ELSE v_else END       [odd arity >= 3]
#   addmonths           -> DATE_ADD('month', n, d)  (arg-order swap)    [2 args]
#   concatwithseparator -> NULL-guarded CONCAT_WS                     [>= 2 args]
#
# Parameterized aggregates (including exact quantiles) are rejected explicitly
# by trino_constructs. Never silently substitute an approximate percentile.


class _FunctionMapping(NamedTuple):
    """One map entry: the AST builder plus its arity admission test."""

    builder: Callable[[exp.Func], exp.Expression]
    accepts: Callable[[int], bool]


def _uniq_exact_as_count_distinct(func: exp.Func) -> exp.Expression:
    """uniqExact(x) -> COUNT(DISTINCT x), preserving EXACT semantics.

    Never degrade to approx_count_distinct: ClickHouse `uniq` is the
    approximate sibling (sqlglot already maps that one to APPROX_DISTINCT),
    while uniqExact is exact, like COUNT(DISTINCT x). Both count distinct
    values ignoring NULLs.
    """
    return exp.Count(this=exp.Distinct(expressions=list(func.expressions)))


def _to_date32_as_cast_date(func: exp.Func) -> exp.Expression:
    """toDate32(x) -> CAST(x AS DATE).

    ClickHouse toDate32 widens Date to Date32 (range 1900-01-01..2299-12-31
    vs Date's 1970-01-01..2149-06-06). On this catalog's columns the input is
    a Date already or the literal '1900-01-01' missing-value sentinel, both
    inside Trino DATE's range, and the corpus's missing_date_sentinel guard
    (toDate32(col) > toDate32('1900-01-01')) evaluates identically once both
    sides collapse to DATE — DATE-vs-DATE comparison needs no wider type.
    """
    return exp.Cast(
        this=func.expressions[0], to=exp.DataType.build(exp.DataType.Type.DATE)
    )


def _to_date_as_cast_date(func: exp.Func) -> exp.Expression:
    """Accept ISO date/datetime strings as well as typed temporal values."""
    from datetime import datetime
    from app.trino_constructs import unsupported
    from app.trino_semantics import require_type

    value = func.expressions[0]
    info = require_type(value, "toDate")
    if isinstance(value, exp.Literal) and value.is_string:
        try:
            parsed = datetime.fromisoformat(value.this)
            if parsed.tzinfo is not None:
                raise ValueError
        except ValueError:
            unsupported("toDate requires an ISO date/time string without a timezone override.")
        value = exp.Literal.string(parsed.date().isoformat())
    elif info.kind == "string":
        value = exp.Cast(this=value, to=exp.DataType.build("TIMESTAMP(9)"))
    elif info.kind == "datetime":
        from app.trino_semantics import truncate_timestamp_seconds
        # CH Date stores unsigned 16-bit epoch days. Timestamp inputs wrap,
        # unlike Date32; preserve that behavior even for pre-1970 DOBs.
        value = truncate_timestamp_seconds(exp.Cast(this=value.copy(), to=exp.DataType.build("TIMESTAMP(9)")))
        days = exp.DateDiff(this=exp.Cast(this=value, to=exp.DataType.build("DATE")),
                            expression=exp.Cast(this=exp.Literal.string("1970-01-01"), to=exp.DataType.build("DATE")),
                            unit=exp.Var(this="DAY"))
        wrapped = exp.Mod(this=exp.Paren(this=exp.Add(this=exp.Mod(this=days, expression=exp.Literal.number(65536)),
                                      expression=exp.Literal.number(65536))), expression=exp.Literal.number(65536))
        return exp.Anonymous(this="DATE_ADD", expressions=[exp.Literal.string("day"), wrapped,
            exp.Cast(this=exp.Literal.string("1970-01-01"), to=exp.DataType.build("DATE"))])
    elif info.kind not in {"datetime", "null"}:
        unsupported("toDate numeric epochs require an explicit temporal conversion first.")
    return exp.Cast(this=value, to=exp.DataType.build("DATE"))


def _to_datetime64_as_cast_timestamp(func: exp.Func) -> exp.Expression:
    """Preserve epoch seconds and precision; reject unverified input signatures."""
    from app.trino_constructs import unsupported
    from app.trino_semantics import require_type

    value, precision = func.expressions
    if not (isinstance(precision, exp.Literal) and not precision.is_string
            and precision.this.isdigit() and 0 <= int(precision.this) <= 9):
        unsupported("toDateTime64 precision must be an integer literal from 0 through 9.")
    info = require_type(value, "toDateTime64")
    target = exp.DataType.build(f"TIMESTAMP({precision.this})")
    if info.kind == "integer" and info.intdiv_safe:
        value = exp.DateAdd(this=exp.Cast(this=exp.Literal.string("1970-01-01 00:00:00"), to=target.copy()),
                            expression=value, unit=exp.Var(this="second"))
    elif info.kind == "float":
        # ClickHouse scales the Float64 epoch before truncating to integer ticks.
        scaled = exp.Mul(this=exp.Cast(this=value, to=exp.DataType.build("DOUBLE")),
                         expression=exp.Literal.number(10 ** int(precision.this)))
        # DOUBLE -> DECIMAL formats a rounded decimal string in Trino. Go via
        # integer ticks to retain the exact scaled binary-float value instead.
        ticks = exp.Cast(this=exp.Cast(this=exp.Anonymous(this="TRUNCATE", expressions=[scaled]),
                                      to=exp.DataType.build("BIGINT")),
                         to=exp.DataType.build("DECIMAL(38,0)"))
        nanos = exp.Mul(this=ticks, expression=exp.Literal.number(10 ** (9 - int(precision.this))))
        value = exp.AtTimeZone(this=exp.Anonymous(this="FROM_UNIXTIME_NANOS", expressions=[nanos]),
                              zone=exp.Literal.string("UTC"))
    elif info.kind == "decimal" and info.intdiv_safe:
        # Truncate before reducing TIMESTAMP precision: Trino CAST rounds.
        seconds = exp.Anonymous(this="TRUNCATE", expressions=[value, precision.copy()])
        nanos = exp.Mul(this=seconds, expression=exp.Literal.number(1000000000))
        value = exp.AtTimeZone(this=exp.Anonymous(this="FROM_UNIXTIME_NANOS", expressions=[nanos]),
                              zone=exp.Literal.string("UTC"))
    elif info.kind in {"string", "datetime", "null"}:
        # Literal strings are truncated before casting, preserving CH's scale.
        if isinstance(value, exp.Literal) and value.is_string:
            from datetime import datetime
            import re
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?)?", value.this):
                unsupported("toDateTime64 requires a timezone-free ISO date/time literal.")
            try:
                datetime.fromisoformat(value.this)
            except ValueError:
                unsupported("Invalid toDateTime64 ISO date/time literal.")
            whole, _, fraction = value.this.partition(".")
            fraction = fraction[:int(precision.this)]
            value = exp.Literal.string(whole + ("." + fraction if fraction else ""))
        elif info.kind in {"string", "datetime"}:
            unsupported("toDateTime64 dynamic text/timestamp scale conversion is unverified; use an ISO literal or an integer/Decimal epoch.")
    else:
        unsupported("toDateTime64 numeric epochs require signed-64-compatible integers, Float64, or Decimal precision up to 18.")
    return exp.Cast(this=value, to=target)


def _to_float64_as_cast_double(func: exp.Func) -> exp.Expression:
    """toFloat64(x) -> CAST(x AS DOUBLE).

    ClickHouse Float64 and Trino DOUBLE are both IEEE-754 binary64, so the
    cast is value-preserving (the corpus's ratio convention
    toFloat64(x) / y then divides in double precision on both engines).
    """
    return exp.Cast(
        this=func.expressions[0], to=exp.DataType.build(exp.DataType.Type.DOUBLE)
    )


def _to_string_as_cast_varchar(func: exp.Func) -> exp.Expression:
    """toString(x) -> CAST(x AS VARCHAR).

    Corpus usage renders codes, dates and decimal amounts; both dialects
    render dates as 'YYYY-MM-DD' and preserve decimal scale text ('100.00'),
    so the rendering is equivalent for the catalog's column types.
    """
    return exp.Cast(
        this=func.expressions[0], to=exp.DataType.build(exp.DataType.Type.VARCHAR)
    )


def _to_uint8_as_cast_integer(func: exp.Func) -> exp.Expression:
    """Truncate numeric inputs toward zero and wrap into the UInt8 range.

    Take the remainder BEFORE truncating: Trino may coerce integral inputs to
    double for truncate(), and doing that to a full Int64 would lose low bits.
    The remainder is bounded to (-256, 256), so that coercion is exact for
    integers. String literals are admitted only as unsigned integer text;
    string-valued expressions require an explicit numeric cast from the caller.
    """
    value = func.expressions[0]
    while isinstance(value, exp.Paren):
        value = value.this
    if isinstance(value, exp.Literal) and value.is_string:
        if not re.fullmatch(r"[+]?[0-9]+", value.this):
            raise QueryValidationError(
                "toUInt8 requires numeric input or an unsigned integer string literal.",
                "TRINO_UNSUPPORTED_SQL",
            )
        # 10**8 is divisible by 256; earlier digits cannot affect the result.
        # Bound int() parsing even for an unusually long input literal.
        value = exp.Literal.number(int(value.this[-8:]) % 256)
    elif isinstance(value, exp.Boolean):
        value = exp.Literal.number(int(value.this))
    elif isinstance(value, (exp.Predicate, exp.And, exp.Or, exp.Not)):
        value = exp.Cast(this=_as_trino_condition(value), to=exp.DataType.build("INTEGER"))
    remainder = exp.Mod(this=exp.Paren(this=value), expression=exp.Literal.number(256))
    truncated = exp.Anonymous(this="truncate", expressions=[remainder])
    wrapped = exp.Mod(
        this=exp.Paren(this=exp.Add(this=truncated, expression=exp.Literal.number(256))),
        expression=exp.Literal.number(256),
    )
    return exp.Cast(this=wrapped, to=exp.DataType.build("INTEGER"))


def _as_trino_condition(condition: exp.Expression) -> exp.Expression:
    """Trino requires Boolean conditions; ClickHouse also permits numeric flags."""
    if isinstance(condition, (exp.And, exp.Or, exp.Not)):
        condition.set("this", _as_trino_condition(condition.this))
        if condition.expression is not None:
            condition.set("expression", _as_trino_condition(condition.expression))
        return condition
    if isinstance(condition, (exp.Predicate, exp.Boolean)):
        return condition
    if isinstance(condition, exp.Paren):
        return exp.Paren(this=_as_trino_condition(condition.this))
    return exp.Cast(this=condition, to=exp.DataType.build("BOOLEAN"))


def _multi_if_as_case(func: exp.Func) -> exp.Expression:
    """multiIf(c1, v1, c2, v2, ..., v_else) -> CASE WHEN ... ELSE ... END.

    ClickHouse multiIf is exactly a left-to-right CASE chain: same
    first-true-branch-wins evaluation, same else fallback. Arity admission
    (map entry): ODD count >= 3 (one or more cond/value pairs plus the else
    value); even-arity calls are invalid in ClickHouse itself and stay
    untouched for the coordinator to reject.
    """
    args = func.expressions
    ifs = [
        exp.If(this=_as_trino_condition(args[i]), true=args[i + 1]) for i in range(0, len(args) - 1, 2)
    ]
    return exp.Case(ifs=ifs, default=args[-1])


def _add_months_as_date_add(func: exp.Func) -> exp.Expression:
    """addMonths(d, n) -> DATE_ADD('month', n, d) — NOTE the argument-order
    swap: ClickHouse is (datetime, num_months), Trino is
    date_add(unit, value, datetime). The unit is the verbatim string 'month'
    (negative n shifts backwards, as in ClickHouse)."""
    date_arg, months = func.expressions
    return exp.DateAdd(this=date_arg, expression=months, unit=exp.Var(this="month"))


def _typed_date_add(func: exp.Func, unit: str) -> exp.Expression:
    from app.trino_constructs import unsupported
    from app.trino_semantics import value_type

    date_arg, offset = func.expressions
    date_type, offset_type = value_type(date_arg), value_type(offset)
    if not date_type or date_type.kind != "datetime" or not offset_type or offset_type.kind != "integer":
        unsupported(f"{func.name} requires a known date/timestamp and an integer {unit} offset.")
    return exp.DateAdd(this=date_arg, expression=offset, unit=exp.Var(this=unit))


def _date_trunc(func: exp.Func) -> exp.Expression:
    # SQLGlot renders toStartOfMonth as dateTrunc in logical SQL round trips.
    from app.trino_constructs import unsupported
    from app.trino_semantics import value_type

    unit, value = func.expressions
    info = value_type(value)
    if (not isinstance(unit, exp.Literal) or not unit.is_string
            or unit.this.lower() not in {"year", "quarter", "month", "week", "day", "hour", "minute", "second"}
            or not info or info.kind != "datetime"):
        unsupported("dateTrunc requires a supported constant unit and a known date/timestamp.")
    return exp.TimestampTrunc(this=value, unit=exp.Var(this=unit.this.upper()))


def _concat_with_separator_as_concat_ws(func: exp.Func) -> exp.Expression:
    """Preserve ClickHouse NULL propagation with sqlglot's CASE guard.

    Trino's bare CONCAT_WS skips NULL arguments; coalesce=False requests the
    guard that returns NULL when any argument is NULL. Non-string inputs still
    require explicit string conversion on this path.
    """
    return exp.ConcatWs(expressions=list(func.expressions), coalesce=False)


def _lower_utf8(func: exp.Func) -> exp.Expression:
    from app.trino_semantics import require_type
    from app.trino_constructs import unsupported
    if require_type(func.expressions[0], "lowerUTF8").kind not in {"string", "null"}:
        unsupported("lowerUTF8 requires a string input.")
    return exp.Lower(this=func.expressions[0].copy())


_CLICKHOUSE_FUNCTION_MAP: dict[str, _FunctionMapping] = {
    "datetrunc": _FunctionMapping(_date_trunc, lambda n: n == 2),
    "lowerutf8": _FunctionMapping(_lower_utf8, lambda n: n == 1),
    "uniqexact": _FunctionMapping(_uniq_exact_as_count_distinct, lambda n: n == 1),
    "countdistinct": _FunctionMapping(_uniq_exact_as_count_distinct, lambda n: n == 1),
    "now": _FunctionMapping(lambda _: exp.CurrentTimestamp(), lambda n: n == 0),
    "addyears": _FunctionMapping(lambda f: _typed_date_add(f, "year"), lambda n: n == 2),
    "adddays": _FunctionMapping(lambda f: _typed_date_add(f, "day"), lambda n: n == 2),
    "todate": _FunctionMapping(_to_date_as_cast_date, lambda n: n == 1),
    "todate32": _FunctionMapping(_to_date32_as_cast_date, lambda n: n == 1),
    "todatetime64": _FunctionMapping(
        _to_datetime64_as_cast_timestamp, lambda n: n == 2
    ),
    "tofloat64": _FunctionMapping(_to_float64_as_cast_double, lambda n: n == 1),
    "tostring": _FunctionMapping(_to_string_as_cast_varchar, lambda n: n == 1),
    "touint8": _FunctionMapping(_to_uint8_as_cast_integer, lambda n: n == 1),
    "multiif": _FunctionMapping(_multi_if_as_case, lambda n: n >= 3 and n % 2 == 1),
    "addmonths": _FunctionMapping(_add_months_as_date_add, lambda n: n == 2),
    "concatwithseparator": _FunctionMapping(
        _concat_with_separator_as_concat_ws, lambda n: n >= 2
    ),
}


def _map_clickhouse_functions(tree: exp.Query) -> None:
    """Normalize supported constructs, reject known unsafe signatures, then map functions.

    Reject unknown anonymous functions before dispatch rather than relying on
    a coordinator FUNCTION_NOT_FOUND error. Typed SQLGlot functions retain
    their dialect conversions.
    """
    from app.trino_constructs import normalize_constructs, unsupported

    from app.trino_semantics import normalize_semantics, value_type

    # These names are handled by the type/construct passes below. Validate
    # caller nodes before those passes introduce Trino-native helper functions.
    semantic_names = {
        "toyear", "tomonth", "todayofmonth", "toint64", "intdiv", "todatetime", "tonullable",
        "sumif", "avgif", "minif", "maxif", "today", "groupuniqarray",
        "arraysort", "arraystringconcat", "formatreadablequantity",
    }
    for func in tree.find_all(exp.Anonymous, exp.AnonymousAggFunc):
        name = func.name.lower()
        mapping = _CLICKHOUSE_FUNCTION_MAP.get(name)
        if mapping is not None and mapping.accepts(len(func.expressions)):
            continue
        if mapping is None and name in semantic_names:
            continue
        unsupported(f"Function {name} with {len(func.expressions)} argument(s) has no verified Trino conversion.")

    normalize_semantics(tree)
    normalize_constructs(tree)
    for conditional in tree.find_all(exp.If):
        # sqlglot represents both CASE variants' WHEN arms as If nodes.
        # Simple CASE compares values; only searched CASE/IF take predicates.
        parent = conditional.parent
        if isinstance(parent, exp.Case) and parent.this is not None:
            continue
        conditional.set("this", _as_trino_condition(conditional.this))
    # Map children before a parent can duplicate them in a NULL guard.
    for func in reversed(list(tree.find_all(exp.Anonymous, exp.AnonymousAggFunc))):
        mapping = _CLICKHOUSE_FUNCTION_MAP.get(func.name.lower())
        if mapping is None or not mapping.accepts(len(func.expressions)):
            continue
        replacement = mapping.builder(func)
        if (logical_type := value_type(func)) is not None:
            replacement.meta["ch_value_type"] = logical_type
        func.replace(replacement)


def rewrite_query(sql: str, settings: Settings) -> str:
    """Route base relations, preserving CTE names, aliases and logical columns.

    Physical catalog names are inaccessible to caller SQL; session-owned
    scratch relations use the configured ClickHouse connector. SELECT/UNION and plain EXPLAIN SELECT are the initial surface.

    Transpile/validation rejections are logged at ERROR (sanitized: error
    code + failure class only — never SQL text, identity values, or row data)
    before they propagate; the wire contract (raised codes) is unchanged.
    """
    try:
        return _rewrite_query(sql, settings)
    except QueryValidationError as exc:
        logger.error(
            "trino transpile/validation rejected query: code=%s "
            "failure_class=%s",
            exc.code,
            type(exc.__cause__).__name__
            if exc.__cause__ is not None
            else type(exc).__name__,
        )
        raise


def _rewrite_query(sql: str, settings: Settings) -> str:
    # All three identity claims are required and strictly validated, but only
    # client_code is a routing input (paycompy client-code RANGE model).
    identity = request_identity(settings)
    client_code = identity[1]
    explain = bool(re.match(r"^\s*EXPLAIN\s+", sql, re.IGNORECASE))
    inner = (
        re.sub(r"^\s*EXPLAIN\s+", "", sql, count=1, flags=re.IGNORECASE)
        if explain
        else sql
    )
    try:
        statements = sqlglot.parse(
            normalize_logical_sql(inner),
            read="clickhouse",
            error_level=ErrorLevel.RAISE,
        )
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise QueryValidationError(
                "Trino supports SELECT/WITH and plain EXPLAIN SELECT only.",
                "TRINO_UNSUPPORTED_SQL",
            )
        from app.trino_constructs import inline_scalar_constants, preserve_paging_order

        inline_scalar_constants(statements[0])
        preserve_paging_order(statements[0])
        tree = qualify_tables(statements[0], db=DEFAULT_DATABASE, dialect="clickhouse")
        # ClickHouse resolves SELECT aliases in GROUP BY/HAVING (alias-first).
        # Trino does not. Expand within this SELECT only, never into subqueries.
        for select in tree.find_all(exp.Select):
            aliases = {item.alias: item.this for item in select.expressions if isinstance(item, exp.Alias)}
            for clause_name in ("group", "having"):
                clause = select.args.get(clause_name)
                if clause is not None:
                    for node in list(clause.walk(prune=lambda n: isinstance(n, (exp.Subquery, exp.Select)))):
                        if isinstance(node, exp.Column) and not node.table and node.name in aliases:
                            node.replace(aliases[node.name].copy())
        from app.scratch_metadata import catalog_entries as scratch_catalog_entries
        catalog = {**get_semantic_catalog(), **scratch_catalog_entries(tree, settings)}
        from app.trino_semantics import annotate_input_types

        annotate_input_types(tree, catalog)
        from app.trino_metadata import canonical_database
        from app.trino_sources import source_relation

        allowed = settings.allowed_databases_list()
        allowed = (
            None if allowed is None else {canonical_database(db) for db in allowed}
        )
        shard = None
        routed = set()
        replacements = []
        for scope in traverse_scope(tree):
            for _, source in scope.selected_sources.values():
                if not isinstance(source, exp.Table):
                    continue  # CTE or derived relation; its own scope is visited.
                if source.catalog or not isinstance(source.this, exp.Identifier):
                    raise QueryValidationError(
                        "Physical catalogs and table functions are not accepted.",
                        "TRINO_UNSUPPORTED_SQL",
                    )
                key = f"{source.db}.{source.name}"
                if key not in catalog or (
                    allowed is not None and source.db not in allowed
                ):
                    raise QueryValidationError(
                        "Trino accepts only allowed catalog tables and session-owned scratch tables.",
                        "TABLE_NOT_ALLOWED",
                    )
                entry = catalog[key]
                # proc_center is NOT a routing input (paycompy client-code range model).
                if entry.get("source", "clickhouse") == "mysql" and shard is None:
                    shard = resolve_mysql_shard(client_code, settings)
                replacements.append((source, entry, shard or ""))
                routed.add(id(source))
        # Catch table-function/other relation forms that scope traversal omitted.
        for table in tree.find_all(exp.Table):
            if id(table) not in routed and (
                table.db or table.catalog or not isinstance(table.this, exp.Identifier)
            ):
                raise QueryValidationError(
                    "Unsupported Trino relation.", "TRINO_UNSUPPORTED_SQL"
                )
        for count in tree.find_all(exp.Count):
            if count.this is None:
                count.set("this", exp.Star())
        # Map caller expressions before inserting backend-native source SQL.
        _map_clickhouse_functions(tree)
        for source, entry, resolved_shard in replacements:
            final = source.parent if isinstance(source.parent, exp.Final) else None
            if final is not None and entry.get("source", "clickhouse") != "clickhouse":
                raise QueryValidationError("FINAL requires a ClickHouse catalog table.", "TRINO_UNSUPPORTED_SQL")
            if entry.get("source") == "scratch":
                replacement = exp.Table(
                    this=exp.to_identifier(entry["table"], quoted=True),
                    db=exp.to_identifier(entry["database"], quoted=True),
                    catalog=exp.to_identifier(validate_identifier(settings.trino_clickhouse_catalog), quoted=True))
            else:
                replacement = source_relation(entry, resolved_shard, identity, settings)
            replacement.set("alias", source.args.get("alias"))
            # The native ClickHouse source already enforces SETTINGS final=1.
            # FINAL must not survive around the Trino pass-through relation.
            (final or source).replace(replacement)
        rendered = tree.sql(dialect="trino", unsupported_level=ErrorLevel.RAISE)
        return f"EXPLAIN {rendered}" if explain else rendered
    except SqlglotError as exc:
        raise QueryValidationError(
            "SQL cannot be safely translated to Trino.", "TRINO_UNSUPPORTED_SQL"
        ) from exc


def _uses_source(sql: str, source_kind: str) -> bool:
    """Inspect already validated logical SQL; CTE aliases are not physical sources."""
    inner = re.sub(r"^\s*EXPLAIN\s+", "", sql, count=1, flags=re.IGNORECASE)
    tree = qualify_tables(sqlglot.parse_one(normalize_logical_sql(inner), read="clickhouse"),
                          db=DEFAULT_DATABASE, dialect="clickhouse")
    catalog = get_semantic_catalog()
    return any(
        isinstance(source, exp.Table)
        and catalog.get(f"{source.db}.{source.name}", {}).get("source", "clickhouse") == source_kind
        for scope in traverse_scope(tree)
        for _, source in scope.selected_sources.values()
    )


def uses_clickhouse_sources(sql: str) -> bool:
    return _uses_source(sql, "clickhouse")


def _connect(settings: Settings, user: str, *, mysql_catalog: str | None = None):
    # Lazy import: existing ClickHouse-only installations keep working.
    from trino.auth import BasicAuthentication
    from trino.dbapi import connect

    if settings.trino_password and settings.trino_http_scheme != "https":
        raise QueryValidationError(
            "Trino password authentication requires HTTPS.", "TRINO_TLS_REQUIRED"
        )
    auth = (
        BasicAuthentication(settings.trino_user, settings.trino_password)
        if settings.trino_user and settings.trino_password
        else None
    )
    # Authentication identifies the service; the query user is the verified
    # caller identity. Trino must authorize service-to-user impersonation.
    session_properties = {"query_max_run_time": f"{settings.max_execution_time}s"}
    if mysql_catalog:
        validate_identifier(mysql_catalog.lower())
        session_properties[f"{mysql_catalog}.aggregation_pushdown_enabled"] = "false"
    return connect(
        host=settings.trino_host,
        port=settings.trino_port,
        http_scheme=settings.trino_http_scheme,
        user=user,
        auth=auth,
        source="clickhouse-api-mcp",
        http_headers=({"X-Trino-Routing-Group": settings.trino_routing_group}
                      if settings.trino_routing_group else None),
        request_timeout=settings.trino_request_timeout,
        max_attempts=1,
        session_properties=session_properties,
    )


def _execute_physical_query(physical_sql: str, settings: Settings, identity: tuple[str, str, str], *, mysql_catalog: str | None = None):
    conn = cursor = None
    try:
        conn = _connect(settings, "|".join(identity), mysql_catalog=mysql_catalog)
        cursor = conn.cursor()
        cursor.execute(physical_sql)
        rows = []
        size = 0
        while batch := cursor.fetchmany(
            min(1000, settings.max_result_rows + 1 - len(rows))
        ):
            rows.extend([list(row) for row in batch])
            size += len(
                json.dumps(batch, default=str, ensure_ascii=False).encode("utf-8")
            )
            if len(rows) > settings.max_result_rows or size > settings.max_result_bytes:
                raise QueryValidationError(
                    "Trino result exceeded the configured result limit.",
                    "TRINO_RESULT_LIMIT",
                )
        return [col[0] for col in cursor.description], rows
    finally:
        record_query_id(getattr(cursor, "query_id", None))
        _close(cursor, conn)


@trace_execution
def execute_query(
    sql: str, settings: Settings | None = None, parameters: dict[str, Any] | None = None
) -> tuple[list[str], list[list[Any]]]:
    settings = settings or get_settings()
    if not settings.trino_security_ready:
        raise QueryValidationError(
            "Validate MySQL source SQL and ClickHouse row policies before setting TRINO_SECURITY_READY=true. "
            "ClickHouse source policies must apply to the connector account.",
            "TRINO_SECURITY_NOT_CONFIGURED",
        )
    if parameters:
        raise QueryValidationError(
            "Internal ClickHouse parameter syntax cannot execute in Trino.",
            "TRINO_UNSUPPORTED_SQL",
        )
    identity = request_identity(settings)
    physical_sql = rewrite_query(sql, settings)
    record_physical(physical_sql)
    mysql_catalog = (
        resolve_mysql_shard(identity[1], settings) if _uses_source(sql, "mysql") else None
    )
    if uses_clickhouse_sources(sql):
        # Do not refresh for MySQL-only requests; validate/compile before I/O.
        from app.employee_access import ensure_employee_access_fresh
        ensure_employee_access_fresh(settings)
    try:
        return _execute_physical_query(physical_sql, settings, identity, mysql_catalog=mysql_catalog)
    except QueryValidationError:
        raise
    except Exception as exc:
        _raise_execution_error(exc)


def ping() -> bool:
    """Connectivity only; data access is independently gated by security_ready."""
    settings = get_settings()
    conn = cursor = None
    try:
        conn = _connect(settings, settings.trino_user)
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        return cursor.fetchone() == [1]
    except Exception as exc:  # noqa: BLE001 — connectivity probes must return a boolean
        from app.error_logging import log_failure
        log_failure(logger, "trino.health", exc, code="TRINO_UNAVAILABLE")
        return False
    finally:
        _close(cursor, conn)


def _raise_execution_error(exc: Exception) -> None:
    # Do not echo connector errors: they can contain SQL, identities and credentials.
    # The server log deliberately carries only sanitized fields — our error
    # code, the exception/coordinator failure CLASS, and the coordinator
    # query_id for ledger correlation — never the connector message, SQL
    # text, identity claims, or row data.
    from trino.exceptions import (
        TrinoAuthError,
        TrinoDataError,
        TrinoQueryError,
        TrinoUserError,
    )

    if isinstance(exc, TrinoDataError):
        logger.error("trino result decoding failed: code=TRINO_QUERY_ERROR failure_class=TrinoDataError")
        raise ClickHouseQueryError(
            "The Trino client cannot decode a result value. Use supported result types or ask the operator to check client compatibility.",
            "TRINO_QUERY_ERROR",
        ) from exc

    if isinstance(exc, TrinoQueryError) and (
        isinstance(exc, TrinoUserError) or exc.error_type == "INSUFFICIENT_RESOURCES"
    ):
        logger.error(
            "trino query rejected by coordinator: code=%s failure_class=%s "
            "trino_error=%s query_id=%s",
            "TRINO_QUERY_ERROR",
            type(exc).__name__,
            exc.error_name,
            exc.query_id,
        )
        # Return only an allowlisted error name and authored hint. Raw
        # coordinator text may contain private identifiers or row values;
        # query_id stays log-side only.
        raise ClickHouseQueryError(
            rewrite_trino_error(exc),
            "TRINO_QUERY_ERROR",
        ) from exc
    logger.error(
        "trino query execution failed: code=%s failure_class=%s",
        "TRINO_UNAVAILABLE",
        type(exc).__name__,
    )
    raise ClickHouseUnavailableError(
        "Trino connector authentication failed; ask the operator to check credentials."
        if isinstance(exc, TrinoAuthError) else "Trino query execution failed.",
        "TRINO_UNAVAILABLE"
    ) from exc


class TrinoRowsStream:
    """Bounded-batch export stream; closing also cancels unfinished queries."""

    def __init__(self, sql: str, settings: Settings):
        self.sql = sql
        self.settings = settings.model_copy(update={
            "max_execution_time": settings.export_max_execution_time,
            "max_result_rows": settings.export_total_max_rows + 1,
            "max_result_bytes": settings.export_total_max_bytes,
        })
        self.conn = self.cursor = None

    def __enter__(self):
        settings = self.settings
        if not settings.trino_security_ready:
            raise QueryValidationError("Trino source security is not configured.", "TRINO_SECURITY_NOT_CONFIGURED")
        identity = request_identity(settings)
        physical = rewrite_query(self.sql, settings)
        record_physical(physical)
        catalog = resolve_mysql_shard(identity[1], settings) if _uses_source(self.sql, "mysql") else None
        if uses_clickhouse_sources(self.sql):
            from app.employee_access import ensure_employee_access_fresh
            ensure_employee_access_fresh(settings)
        try:
            self.conn = _connect(settings, "|".join(identity), mysql_catalog=catalog)
            self.cursor = self.conn.cursor()
            self.cursor.execute(physical)
            self.column_names = [col[0] for col in self.cursor.description]
            self.column_type_names = [str(col[1]) for col in self.cursor.description]
            return self
        except Exception as exc:
            self.__exit__(None, None, None)
            _raise_execution_error(exc)

    def __iter__(self):
        try:
            while batch := self.cursor.fetchmany(1000):
                for row in batch:
                    yield list(row)
        except Exception as exc:
            _raise_execution_error(exc)

    def __exit__(self, *exc_info):
        record_query_id(getattr(self.cursor, "query_id", None))
        _close(self.cursor, self.conn)


@contextmanager
def execute_query_rows_stream(sql: str, settings: Settings):
    from app.trino_diagnostics import trace_query
    with trace_query(sql, settings):
        with TrinoRowsStream(sql, settings) as stream:
            yield stream
