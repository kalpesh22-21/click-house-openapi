"""Caller-owned SQL context and authored recovery guidance for REST and MCP.

Never use coordinator SQL, exception bodies, error positions in rewritten SQL,
identity claims or native view definitions to build an excerpt.
"""
from functools import wraps
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel
from app.errors import ClickHouseAPIError, QueryValidationError, ClickHouseQueryError, ParseFailedError, CartesianJoinError


_ALTERNATIVES = {
    'formatreadablequantity': "If display formatting is optional, return the numeric bucket or use toString(bucket); this does not reproduce the human-readable suffix formatting.",
    'toint64': "Use toInt64 only for supported numeric inputs; it truncates toward zero. For exact salary buckets prefer intDiv(annual_salary, 25000). String parsing and unsigned overflow have no verified equivalent here; inspect getTableSchema before choosing a conversion.",
    'toyear': "Use toYear(date_column) with a catalog date/timestamp. For a string column, establish its format before an explicit ClickHouse date conversion; timezone overloads remain unsupported.",
    'intdiv': "For salary buckets use intDiv(annual_salary, 25000) with a catalog integer or Decimal(18, scale) input and a positive integer literal divisor. Do not substitute floating-point division near bucket boundaries.",
    'quantile': "Exact medians support quantileExactLow(0.5)(salary) and quantileExactHigh(0.5)(salary), including If variants. For their average use (quantileExactLow(0.5)(salary) + quantileExactHigh(0.5)(salary)) / 2. Other levels have no verified equivalent here; do not replace exact results with estimates.",
    'jsonextractstring': "Dynamic JSON extraction has no verified equivalent on this route. Select the authorized JSON column for downstream parsing, or use a catalog column exposing the required field.",
    'argmax': "Use scalar argMax(value, ordering_key) in a GROUP BY query when a grouped result meets the request; tuple and windowed variants remain unsupported.",
    'todatetime': "Use toDateTime(integer_epoch) or toDateTime('2024-01-01 00:00:00') with the intended session timezone. There is no verified equivalent for the rejected timezone/range-wrapping overloads.",
    'paging': "Project the sort key from the inner query and order the paging query by that key, for example SELECT * FROM (SELECT department_code FROM dbpcm_warehouse.employee) AS p ORDER BY department_code LIMIT 25. Preserve any secondary keys needed for deterministic paging.",
}


# Curated from the trace battery and remote-dev report, not arbitrary functions.
_OBSERVED_FUNCTIONS = {'formatreadablequantity', 'toint64', 'toyear', 'intdiv',
                       'quantile', 'groupuniqarray', 'arraysort',
                       'arraystringconcat', 'today', 'minif', 'maxif', 'uniqexact'}
_ALTERNATIVES.update({
    'groupuniqarray': "Use groupUniqArray(column) for an exact distinct list. Size-limited variants are unsupported on this route; do not silently drop a requested size limit.",
    'arraysort': "Use arraySort(array_expression) for natural ascending order. Lambda/key sorting has no verified conversion here; use rows with ORDER BY on the required key if a row result meets the request.",
    'arraystringconcat': "Use arrayStringConcat(string_array, ', ') or arrayStringConcat(string_array). For directory labels try arrayStringConcat(arraySort(groupUniqArray(department_name)), ', '). Verify that elements are strings.",
    'today': "Use today() without arguments, for example date_column >= today() - 30. Verify the catalog date type and intended session timezone before changing boundaries.",
    'minif': "Use minIf(amount, condition) with a supported catalog numeric input and a boolean condition. Check nullability with getTableSchema; do not replace empty results with zero unless that preserves the metric.",
    'maxif': "Use maxIf(amount, condition) with a supported catalog numeric input and a boolean condition. Check nullability with getTableSchema; do not replace empty results with zero unless that preserves the metric.",
    'uniqexact': "For one column use uniqExact(employee_code). For distinct pairs, a ClickHouse alternative is SELECT count() FROM (SELECT DISTINCT employee_code, toDate(punch_date) AS day FROM dbpcm_warehouse.punch_pair WHERE employee_code IS NOT NULL AND punch_date IS NOT NULL) AS pairs. Preserve original filters, grouping and NULL-exclusion rules; do not concatenate keys or approximate the count.",
})


def function_error_guidance(sql, message, code):
    """Caller-AST candidates; sanitized runtime errors lack precise attribution."""
    diagnostic = message.split('Hint:', 1)[0].split('Alternative:', 1)[0]
    runtime = bool(re.search(r'\b(FUNCTION_NOT_FOUND|UNKNOWN_FUNCTION|NUMBER_OF_ARGUMENTS_DOESNT_MATCH|BAD_ARGUMENTS)\b', diagnostic))
    if code != 'TRINO_UNSUPPORTED_SQL' and not runtime:
        return ''
    try:
        tree = sqlglot.parse_one(sql, read='clickhouse', error_level=ErrorLevel.RAISE)
        names = []
        for node in tree.find_all(exp.Func):
            name = (node.name if isinstance(node, (exp.Anonymous, exp.AnonymousAggFunc, exp.ParameterizedAgg)) else node.sql_name()).lower()
            key = 'quantile' if name.startswith(('quantileexactlow', 'quantileexacthigh')) else name
            if key in _OBSERVED_FUNCTIONS and key not in names:
                names.append(key)
        if not runtime:
            names = [key for key in names if key in diagnostic.lower()]
        if not names:
            return ''
        prefix = ('Candidate function guidance from the submitted ClickHouse query; the failing function is not confirmed: '
                  if runtime else 'Supported ClickHouse alternatives: ')
        return prefix + ' '.join(_ALTERNATIVES[key] for key in names[:3])
    except Exception:
        return ''


def logical_excerpt(sql, message):
    """A bounded, redacted AST fragment, never a raw-text parsing fallback."""
    try:
        tree = sqlglot.parse_one(sql, read='clickhouse', error_level=ErrorLevel.RAISE)
        if not isinstance(tree, exp.Query):
            return 'unavailable for this statement shape'
        # Prefer a named function explicitly identified by our authored error.
        functions = list(tree.find_all(exp.Func))
        diagnostic = message.split("Hint:", 1)[0].split("Alternative:", 1)[0]
        target = next((f for f in functions if
                       re.search(r"\b" + re.escape(f.name if isinstance(f, (exp.Anonymous, exp.AnonymousAggFunc, exp.ParameterizedAgg)) else f.sql_name()) + r"\b", diagnostic, re.I)), None)
        specific = target is not None
        if target is None:
            target = tree.find(exp.Select) or tree
        target = target.copy()
        for node in target.walk():
            node.comments = None
        for literal in list(target.find_all(exp.Literal)):
            literal.replace(exp.Literal.string('?') if literal.is_string else exp.Placeholder())
        rendered = ' '.join(target.sql(dialect='clickhouse').split())
        # Only caller-supplied identifiers survive; literal values and comments
        # never do. Truncation is for display, not executable replacement SQL.
        return ('identified expression: ' if specific else 'query context; exact failure location unavailable: ') + rendered[:240] + ('…' if len(rendered) > 240 else '')
    except Exception:
        # Malformed quoting cannot safely be redacted with substring heuristics.
        return 'unavailable because the input could not be parsed safely; check incomplete quotes and parentheses'


def type_error_guidance(sql, message):
    """Point to caller-owned candidate operands, never claim backend attribution.

    Coordinator locations refer to rewritten SQL and are intentionally ignored.
    These are structural candidates, not a type checker or automatic coercion.
    """
    if not re.search(r"\b(NO_COMMON_TYPE|TYPE_MISMATCH|OPERATOR_NOT_FOUND|ILLEGAL_TYPE_OF_ARGUMENT|INVALID_CAST_ARGUMENT)\b", message):
        return ""
    try:
        tree = sqlglot.parse_one(sql, read="clickhouse", error_level=ErrorLevel.RAISE)
        if "INVALID_CAST_ARGUMENT" in message and any(
            isinstance(n, exp.Anonymous) and n.name.lower() == "toint64" for n in tree.walk()
        ):
            return (" toInt64 floating-point inputs must be finite and within [-2^63, 2^63). "
                    "Inspect the input values and getTableSchema; do not silently discard rows "
                    "or replace overflow with zero. For exact salary bucketing prefer "
                    "intDiv(annual_salary, 25000) on a supported Decimal column.")
        candidates = []
        for node in tree.walk():
            label = None
            if isinstance(node, exp.Union):
                left, right = node.this, node.expression
                if isinstance(left, exp.Select) and isinstance(right, exp.Select):
                    for i, (a, b) in enumerate(zip(left.expressions, right.expressions), 1):
                        candidates.append((f"UNION output position {i}", exp.Tuple(expressions=[a.copy(), b.copy()])))
            elif isinstance(node, (exp.Case, exp.Coalesce)):
                label = "conditional/default expression"
            elif isinstance(node, (exp.Lag, exp.Lead)):
                label = "window value/default"
            elif isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)):
                label = "comparison operands"
            if label:
                candidates.append((label, node))
        rendered = []
        for label, node in candidates[:3]:
            # Reuse the same literal/comment redaction as ordinary excerpts.
            excerpt = logical_excerpt("SELECT " + node.sql(dialect="clickhouse"), "")
            excerpt = excerpt.split("exact failure location unavailable: ", 1)[-1]
            rendered.append(label + ": " + excerpt)
        if not rendered:
            return ""
        return (" Candidate type-conflict expressions (not a confirmed error location): "
                + "; ".join(rendered)
                + ". Inspect their logical types with getTableSchema. Align corresponding "
                "UNION positions, CASE branches and window defaults. For example, for a "
                "Nullable(Decimal(18,6)) amount use coalesce(amount, CAST(0 AS Decimal(18,6))); "
                "choose the actual catalog precision/scale. For Date comparisons use "
                "date_column >= toDate('2026-01-01'). Do not switch exact money to Float64 "
                "or replace NULL with zero unless the requested metric permits it.")
    except Exception:
        return ""


def unavailable_source(exc, sql):
    """Identify only a confirmed missing table named in both error and logical SQL."""
    if not isinstance(exc, ClickHouseQueryError):
        return None
    message = exc.message.split("Submitted ClickHouse excerpt", 1)[0]
    if not re.search(r"\b(?:UNKNOWN_TABLE|TABLE_NOT_FOUND)\b", exc.code + " " + message):
        return None
    # ClickHouse's public message deliberately strips the table name. Inspect
    # the local cause chain only to match a caller-owned catalog source; never
    # forward a raw cause, physical identifier or SQL excerpt.
    match = None
    cause = exc
    for _ in range(4):
        if cause is None:
            break
        match = re.search(
            r"(?:Unknown table expression identifier|Table)\s+['`\"]([A-Za-z_][A-Za-z_0-9.]*)['`\"]",
            str(cause), re.I,
        ) or re.search(r"Table\s+([A-Za-z_][A-Za-z_0-9.]*)\s+does not exist", str(cause), re.I)
        if match:
            break
        cause = cause.__cause__
    if not match:
        return None
    try:
        from app.semantic_catalog.loader import get_semantic_catalog
        from sqlglot.optimizer.scope import traverse_scope
        tree = sqlglot.parse_one(sql, read="clickhouse")
        tables = {f"{source.db}.{source.name}" for scope in traverse_scope(tree)
                  for source in scope.sources.values() if isinstance(source, exp.Table)}
        missing = match[1]
        # Do not guess a logical source from a rewritten physical table name.
        catalog = get_semantic_catalog()
        candidates = [t for t in tables if t in catalog
                      and (t == missing or ("." not in missing and t.rsplit(".", 1)[-1] == missing))]
        return candidates[0] if len(candidates) == 1 else None
    except Exception:
        return None


def enrich_query_error(exc, sql):
    if getattr(exc, '_query_feedback_added', False):
        return
    message = exc.message
    if (not isinstance(exc, (QueryValidationError, ClickHouseQueryError, ParseFailedError, CartesianJoinError))
            or exc.code in {'TRINO_IDENTITY_REQUIRED', 'TRINO_SECURITY_NOT_CONFIGURED', 'TABLE_NOT_ALLOWED'}):
        # Authorization/infrastructure errors already contain an operator action.
        # Never encourage SQL changes to bypass a policy or resolve an outage.
        return
    source = unavailable_source(exc, sql)
    if source:
        exc.unavailable_source = source
        exc.query_feedback = {"reason": message}
        exc._query_feedback_added = True
        return
    advice = function_error_guidance(sql, message, exc.code)
    if exc.code == 'TRINO_UNSUPPORTED_SQL' and not advice:
        advice = next((text for key,text in _ALTERNATIVES.items() if key in message.lower()), '')
        if not advice:
            advice = 'Call getTableSchema to verify input types, then simplify the rejected expression or use an explicit ClickHouse CAST with the intended Nullable type only if it preserves the requested meaning.'
    typed_guidance = type_error_guidance(sql, message)
    excerpt = logical_excerpt(sql, message)
    # Share structured, authored feedback with MCP without reparsing the REST text.
    exc.query_feedback = {"reason": message, "excerpt": excerpt,
                          "alternative": advice, "type_guidance": typed_guidance}
    exc.message = (message + ' Write the corrected query in ClickHouse SQL using logical catalog tables; the service handles backend translation. '
                   + (('Alternative: ' + advice + ' ') if advice else '')
                   + 'Submitted ClickHouse excerpt (literal values redacted): ' + excerpt + typed_guidance)
    exc.args = (exc.message,)
    exc._query_feedback_added = True


def with_query_feedback(function):
    @wraps(function)
    def wrapped(sql, *args, **kwargs):
        try:
            return function(sql, *args, **kwargs)
        except ClickHouseAPIError as exc:
            enrich_query_error(exc, sql)
            raise
    return wrapped


def observed_function_guide():
    """Brief route-specific index; detailed advice shares the error registry above."""
    return (
        "Translated-query signature guide (conditional, not a complete allowlist): "
        "exact medians use quantileExactLow(0.5)(x)/quantileExactHigh(0.5)(x), including If variants; never substitute approximate percentiles. "
        "Distinct lists use groupUniqArray(x), arraySort(array), arrayStringConcat(string_array, separator). "
        "today() has no arguments. intDiv(x, positive_integer_literal) requires a supported integer or Decimal input; inspect logical types. "
        "minIf/maxIf and window aggregates depend on input types and nullability. Outer-join aggregate nullability is not verified. "
        "Lambda arraySort, formatReadableQuantity and other exact quantile levels have no verified conversion. "
        "For a rejected signature follow its detailed error guidance in ClickHouse SQL. Native ClickHouse supports additional forms; absence from this list is not a ban. "
    )
