"""Type-aware ClickHouse semantics for the supported Trino conversion subset.

Catalog types are the logical input contract. Unknown nullability is never
inferred from the rows currently present or from a WHERE predicate.
"""
from dataclasses import dataclass, replace
import json

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.errors import SqlglotError
from app.trino_constructs import unsupported


@dataclass(frozen=True)
class ValueType:
    kind: str
    nullable: bool
    intdiv_safe: bool = False
    year_safe: bool = False


def declared_type(dtype):
    if isinstance(dtype, str):
        try:
            dtype = exp.DataType.build(dtype, dialect="clickhouse")
        except Exception:
            return None
    if not isinstance(dtype, exp.DataType):
        return None
    name = dtype.this.value.upper()
    if name in {"TINYINT", "SMALLINT", "INT", "BIGINT", "UTINYINT", "USMALLINT", "UINT", "UBIGINT", "INT128", "UINT128", "INT256", "UINT256"}:
        kind = "integer"
    elif name in {"DECIMAL", "DECIMAL32", "DECIMAL64", "DECIMAL128", "DECIMAL256"}:
        kind = "decimal"
    elif name in {"FLOAT", "DOUBLE"}:
        kind = "float"
    elif name in {"VARCHAR", "TEXT", "CHAR", "STRING"}:
        kind = "string"
    elif name == "ARRAY":
        kind = "array"
    elif name in {"DATE", "DATE32", "DATETIME", "DATETIME64", "TIMESTAMP", "TIMESTAMPTZ"}:
        kind = "datetime"
    else:
        return None
    safe = name in {"TINYINT", "SMALLINT", "INT", "BIGINT", "UTINYINT", "USMALLINT", "UINT"}
    if kind == "decimal" and name == "DECIMAL" and dtype.expressions:
        try:
            safe = int(dtype.expressions[0].this.this) <= 18
        except (ValueError, AttributeError):
            pass
    return ValueType(kind, bool(dtype.args.get("nullable")), safe,
                     kind == "datetime" and not any(x.is_string for x in dtype.find_all(exp.Literal)))


def value_type(node):
    if "ch_value_type" in node.meta:
        return node.meta["ch_value_type"]
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and not node.this.is_string and node.this.this.isdigit():
        return ValueType("integer", False, -9223372036854775808 <= -int(node.this.this) <= 9223372036854775807)
    if isinstance(node, (exp.Alias, exp.Paren, exp.Neg)):
        return value_type(node.this)
    if isinstance(node, exp.Literal):
        kind = "string" if node.is_string else "float" if "." in node.this or "e" in node.this.lower() else "integer"
        return ValueType(kind, False, kind == "integer" and 0 <= int(node.this) <= 9223372036854775807)
    if isinstance(node, exp.Null):
        return ValueType("null", True)
    if isinstance(node, exp.Array):
        return ValueType("array", False)
    if isinstance(node, (exp.CurrentDate, exp.CurrentTimestamp)):
        return ValueType("datetime", False, year_safe=True)
    if isinstance(node, (exp.TimestampTrunc, exp.DateTrunc)):
        return value_type(node.this)
    if isinstance(node, exp.DateDiff):
        start, end = value_type(node.expression), value_type(node.this)
        if (start and end and start.kind == end.kind == "datetime"
                and node.text("unit").upper() == "DAY"
                and node.args.get("zone") is None):
            return ValueType("integer", start.nullable or end.nullable)
    if isinstance(node, exp.Cast):
        target = declared_type(node.args["to"])
        source = value_type(node.this)
        # Trino CAST does not enforce CH non-nullability. Do not hide a
        # source-side NULL conversion error behind a successful aggregate.
        if target and not target.nullable and (source is None or source.nullable):
            return None
        return target
    if isinstance(node, (exp.Count, exp.CountIf)):
        return ValueType("integer", False, True)
    if isinstance(node, (exp.Window, exp.Filter)):
        return value_type(node.this)
    if isinstance(node, exp.Coalesce):
        types = [value_type(n) for n in [node.this, *node.expressions]]
        if any(t is None for t in types):
            return None
        concrete = [t for t in types if t.kind != "null"]
        kind = _common_kind(node, concrete)
        if kind:
            return ValueType(kind, all(t.nullable for t in types))
    if isinstance(node, exp.Case):
        branches = [clause.args["true"] for clause in node.args.get("ifs", [])]
        branches.append(node.args.get("default") or exp.Null())
        types = [value_type(branch) for branch in branches]
        if any(t is None for t in types):
            return None
        kind = _common_kind(node, [t for t in types if t.kind != "null"])
        if kind:
            return ValueType(kind, any(t.nullable for t in types))
    if isinstance(node, (exp.Floor, exp.Ceil)):
        source = value_type(node.this)
        if source and source.kind in {"integer", "decimal", "float"}:
            return source
    if isinstance(node, (exp.Abs, exp.Round)):
        # SQLGlot currently reports ROUND(Decimal) as DOUBLE. CH preserves the
        # input numeric family and nullability, so retain this small correction.
        source = value_type(node.this)
        if source and source.kind in {"integer", "decimal", "float"}:
            if isinstance(node, exp.Round) and source.kind == "float":
                return None  # CH float ties-to-even is not Trino's rounding rule.
            precision = node.args.get("decimals") if isinstance(node, exp.Round) else None
            if precision is not None:
                ptype = value_type(precision)
                if ptype is None or ptype.nullable:
                    return None
            return source
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)):
        a, b = value_type(node.this), value_type(node.expression)
        numeric = {"integer", "decimal", "float"}
        if a and b and a.kind in numeric and b.kind in numeric:
            kind = "float" if isinstance(node, exp.Div) or "float" in {a.kind, b.kind} else "decimal" if "decimal" in {a.kind, b.kind} else "integer"
            return ValueType(kind, a.nullable or b.nullable, kind == "integer" and a.intdiv_safe and b.intdiv_safe)
    if isinstance(node, exp.ArgMax):
        value, key = value_type(node.this), value_type(node.expression)
        return replace(value, nullable=value.nullable or key.nullable) if value and key else None
    if isinstance(node, (exp.Sum, exp.Avg, exp.Min, exp.Max)):
        t = value_type(node.this)
        return replace(t, kind="float") if t and isinstance(node, exp.Avg) else t
    if isinstance(node, (exp.Anonymous, exp.AnonymousAggFunc)):
        name = node.name.lower()
        args = node.expressions
        if name == "todatetime64" and len(args) == 2:
            source = value_type(args[0])
            return replace(source, kind="datetime", year_safe=True) if source else None
        if name in {"addyears", "addmonths"} and len(args) == 2:
            source, offset = value_type(args[0]), value_type(args[1])
            if source and offset and source.kind == "datetime" and offset.kind == "integer":
                return replace(source, nullable=source.nullable or offset.nullable)
        if name in {"sumif", "avgif", "minif", "maxif"} and len(args) == 2:
            value = value_type(args[0])
            return replace(value, kind="float") if value and name == "avgif" else value
        # The supported single-input mapping becomes COUNT(DISTINCT ...).
        # Its result is non-null even when the source column is nullable.
        # Record this before resolving derived projections and outer aggregates.
        if name in {"uniqexact", "countdistinct"} and len(args) == 1:
            return ValueType("integer", False, True)
        if name == "intdiv" and len(args) == 2:
            source = value_type(args[0])
            if source and source.intdiv_safe:
                return ValueType("integer", source.nullable, True)
        if name in {"today", "now"} and not args:
            return ValueType("datetime", False, year_safe=True)
        if len(args) == 1:
            source = value_type(args[0])
            if source:
                if name in {"toint64", "toyear"}:
                    return ValueType("integer", source.nullable, True)
                if name == "tonullable":
                    return replace(source, nullable=True)
                kinds = {"tofloat64": "float", "tostring": "string", "todate": "datetime", "todate32": "datetime", "todatetime": "datetime"}
                if name in kinds:
                    return replace(source, kind=kinds[name], year_safe=kinds[name] == "datetime")
    return None



def _common_kind(node, types):
    """Use SQLGlot for common numeric types, never for source nullability."""
    if not types:
        return "null"
    kinds = {t.kind for t in types}
    if len(kinds) == 1:
        return types[0].kind
    if kinds <= {"integer", "decimal", "float"}:
        inferred = node.meta.get("sqlglot_kind")
        return inferred if inferred in kinds else None
    return None


def annotate_input_types(tree, catalog):
    """Analyze a qualified copy; preserve the original authorization/routing AST.

    SQLGlot supplies name resolution and common expression types. Its nullable
    flags are not a CH semantics proof (COALESCE, UNION and outer joins differ).
    Transfer only types proven by our nullability rules back to original nodes.
    Schema qualification failures use the conservative scoped fallback.
    """
    schema = {}
    for key, entry in catalog.items():
        database, table = key.split(".", 1)
        columns = {name: details["type"] for name, details in entry.get("columns", {}).items() if details.get("type")}
        if columns:
            schema.setdefault(database, {})[table] = columns
    original = list(tree.walk())
    analyzed = tree.copy()
    for number, node in enumerate(analyzed.walk()):
        node.meta["analysis_origin"] = number
    try:
        analyzed = qualify(
            analyzed, schema=schema, db="dbpcm_warehouse", dialect="clickhouse",
            infer_schema=False, expand_alias_refs=False, expand_stars=False,
            quote_identifiers=False, identify=False,
        )
        annotate_types(analyzed, schema=schema, dialect="clickhouse")
    except SqlglotError:
        _annotate_scoped_types(tree, catalog)
        return
    for node in analyzed.walk():
        inferred = declared_type(node.type)
        if inferred:
            node.meta["sqlglot_kind"] = inferred.kind
    _annotate_scoped_types(analyzed, catalog)
    for node in analyzed.walk():
        origin = node.meta.get("analysis_origin")
        if origin is not None:
            # No aliases, SQL text, stars, or relations from the analysis copy
            # are installed into the executable query.
            original[origin].meta["ch_value_type"] = value_type(node)
            original[origin].meta["ch_outer_join_unknown"] = node.meta.get("ch_outer_join_unknown", False)


def _annotate_scoped_types(tree, catalog):
    """Resolve catalog/derived columns by SQL scope before physical substitution.

    Preserve known types through same-kind UNION branches and the preserved side
    of outer joins. Unmatched-side nullability still depends on join_use_nulls.
    """
    outputs = {}
    for scope in traverse_scope(tree):
        if isinstance(scope.expression, exp.SetOperation):
            branches = [list(outputs.get(id(branch), {}).items()) for branch in scope.union_scopes]
            merged = {}
            if len(branches) == 2 and len(branches[0]) == len(branches[1]):
                for (name, left), (_, right) in zip(*branches):
                    merged[name] = None  # Keep ordinal positions for enclosing UNIONs.
                    concrete = [t for t in (left, right) if t and t.kind != "null"]
                    if left and right and concrete and len({t.kind for t in concrete}) == 1:
                        merged[name] = ValueType(concrete[0].kind, left.nullable or right.nullable,
                                                all(t.intdiv_safe for t in concrete), all(t.year_safe for t in concrete))
            outputs[id(scope)] = merged
            continue
        sources = scope.selected_sources
        unknown_outer = set()
        source = scope.expression.args.get("from_")
        seen = {source.this.alias_or_name} if source else set()
        for join in scope.expression.args.get("joins", []):
            alias, side = join.this.alias_or_name, join.args.get("side")
            if side in {"RIGHT", "FULL"}:
                unknown_outer.update(seen)
            if side in {"LEFT", "FULL"}:
                unknown_outer.add(alias)
            seen.add(alias)
        for column in (n for n in scope.walk() if isinstance(n, exp.Column)):
            candidates = []
            for alias, (_, source) in sources.items():
                if column.table and column.table != alias:
                    continue
                info = None
                if isinstance(source, exp.Table):
                    entry = catalog.get(f"{source.db or 'dbpcm_warehouse'}.{source.name}", {})
                    info = declared_type(entry.get("columns", {}).get(column.name, {}).get("type"))
                elif isinstance(source, Scope):
                    info = outputs.get(id(source), {}).get(column.name)
                if info:
                    candidates.append(info)
            unambiguous = bool(column.table) or len(sources) == 1
            outer = column.table in unknown_outer if column.table else bool(unknown_outer)
            column.meta["ch_outer_join_unknown"] = outer
            column.meta["ch_value_type"] = candidates[0] if len(candidates) == 1 and not outer and unambiguous else None
        projected = {}
        for item in scope.expression.selects:
            if item.alias_or_name:
                projected[item.alias_or_name] = value_type(item)
        outputs[id(scope)] = projected


def require_type(value, function):
    result = value_type(value)
    if result is None:
        if any(n.meta.get("ch_outer_join_unknown") for n in value.walk()):
            unsupported(f"{function} cannot verify input nullability after an outer join. Moving the same join into a CTE does not fix this. Aggregate before joining only if row grain and unmatched-row semantics remain identical; omit an unnecessary join only if none of its columns or filtering is needed. Do not replace LEFT JOIN with INNER JOIN merely to bypass validation.")
        unsupported(f"{function} requires a known logical input type and nullability. Use a catalog column or an explicit ClickHouse CAST with the intended Nullable type.")
    return result


def aggregate(node, condition=None, window=None):
    """Preserve zero/NaN/NULL defaults using declared source nullability."""
    from app.trino_client import _as_trino_condition
    value = node.this
    info = require_type(value, "aggregate")
    if info.kind not in {"integer", "decimal", "float", "null"}:
        unsupported("Numeric aggregate input must have a supported numeric type.")
    result = node.copy()
    # CH AVG returns Float64 even for Decimal input; Trino AVG(DECIMAL)
    # retains the decimal scale and can round away fractional mean digits.
    if isinstance(result, exp.Avg):
        result.set("this", exp.Cast(this=value.copy(), to=exp.DataType.build("DOUBLE")))
    if condition is not None:
        predicate = _as_trino_condition(condition.copy())
        if window is not None:
            # Trino does not support FILTER on window aggregates. CASE preserves
            # the selected values; the source input still determines defaults.
            result.set("this", exp.Case(ifs=[exp.If(this=predicate, true=result.this)], default=exp.Null()))
        else:
            result = exp.Filter(this=result, expression=exp.Where(this=predicate))
    if window is not None:
        framed = window.copy()
        framed.set("this", result)
        result = framed
    if info.nullable:
        return result
    default = exp.Anonymous(this="NAN") if isinstance(node, exp.Avg) else exp.Literal.number(0)
    return exp.Coalesce(this=result, expressions=[default])



_CONDITIONAL_AGGREGATES = {"sumif": exp.Sum, "avgif": exp.Avg, "minif": exp.Min, "maxif": exp.Max}


def conditional_aggregate(node, window=None):
    if len(node.expressions) != 2 or isinstance(node.parent, exp.Filter):
        unsupported("Unsupported conditional aggregate signature.")
    value, condition = node.expressions
    return aggregate(_CONDITIONAL_AGGREGATES[node.name.lower()](this=value.copy()), condition, window)


def exact_median(node):
    """Exact order statistics; never interpolate or approximate source values.

    ARRAY_AGG and COUNT share the same eligible set. Integer index arithmetic
    avoids float rounding and ELEMENT_AT safely handles an empty group.
    """
    from decimal import Decimal, InvalidOperation
    from sqlglot import parse_one
    from app.trino_client import _as_trino_condition
    name = node.name.lower()
    supported = {"quantileexactlow", "quantileexacthigh", "quantileexactlowif", "quantileexacthighif"}
    if name not in supported:
        unsupported("Unsupported parameterized aggregate. Exact quantiles are never replaced with estimates.")
    levels, args = node.expressions, node.args.get("params", [])
    valid_level = False
    if len(levels) == 1 and isinstance(levels[0], exp.Literal) and not levels[0].is_string:
        try:
            valid_level = Decimal(levels[0].this) == Decimal("0.5")
        except InvalidOperation:
            pass
    if not valid_level or len(args) != (2 if name.endswith("if") else 1):
        unsupported("Exact Low/High quantiles currently support only the median level 0.5 and one numeric value, optionally with an If condition.")
    if isinstance(node.parent, (exp.Window, exp.Filter)):
        unsupported("Windowed/explicitly filtered exact quantiles are not yet supported; use a grouped median joined to its partition.")
    value = args[0]
    info = require_type(value, "Exact median")
    if info.kind not in {"integer", "decimal", "float"}:
        unsupported("Exact medians require a known numeric input type.")
    predicate = exp.Not(this=exp.Is(this=value.copy(), expression=exp.Null()))
    if name.endswith("if"):
        predicate = exp.and_(predicate, _as_trino_condition(args[1].copy()))
    def eligible(agg):
        return exp.Filter(this=agg, expression=exp.Where(this=predicate.copy()))
    values = eligible(exp.ArrayAgg(this=value.copy()))
    count = eligible(exp.Count(this=value.copy()))
    # Parse as Trino to retain integer division, rather than CH float division.
    index = parse_one("(n + 1) / 2" if "low" in name else "n / 2 + 1", read="trino")
    for column in list(index.find_all(exp.Column)):
        column.replace(count.copy())
    index = exp.Anonymous(this="GREATEST", expressions=[exp.Literal.number(1), index])
    result = exp.Anonymous(this="ELEMENT_AT", expressions=[exp.Anonymous(this="ARRAY_SORT", expressions=[values]), index])
    if info.nullable:
        return result
    default = exp.Anonymous(this="NAN") if info.kind == "float" else exp.Literal.number(0)
    return exp.Coalesce(this=result, expressions=[default])


def map_to_year(node):
    if len(node.expressions) != 1:
        unsupported("toYear supports one date/timestamp argument without timezone overrides.")
    value = node.expressions[0]
    info = require_type(value, "toYear")
    if info.kind != "datetime" or not info.year_safe:
        unsupported("toYear requires a known Date/Date32 or timezone-free timestamp contract; string, numeric and timezone overloads are unverified.")
    return exp.Year(this=value.copy())


def map_to_int64(node):
    if len(node.expressions) != 1:
        unsupported("toInt64 requires one numeric argument.")
    value = node.expressions[0]
    info = require_type(value, "toInt64")
    if info.kind == "null":
        return exp.Cast(this=exp.Null(), to=exp.DataType.build("BIGINT"))
    if info.kind not in {"integer", "decimal", "float"} or (
        info.kind != "float" and not info.intdiv_safe
    ):
        unsupported("toInt64 supports signed-64-compatible integers, Decimal precision up to 18, and finite in-range floating-point inputs. String parsing, UInt64 wraparound and wider numeric types are unverified.")
    result = value.copy()
    if info.kind in {"decimal", "float"}:
        result = exp.Anonymous(this="TRUNCATE", expressions=[result])
    if info.kind == "float":
        # CH overflow/non-finite conversion is not a portable contract. Admit
        # only [-2**63, 2**63); a NaN cast explicitly fails outside that domain.
        # NULL remains NULL. Bounds are exact powers of two in IEEE double.
        from sqlglot import parse_one
        guarded = parse_one(
            "IF(x IS NULL OR (is_finite(x) AND x >= -9.223372036854776E18 "
            "AND x < 9.223372036854776E18), truncate(x), nan())", read="trino")
        for col in list(guarded.find_all(exp.Column)):
            col.replace(value.copy())
        result = guarded
    return exp.Cast(this=result, to=exp.DataType.build("BIGINT"))


def map_intdiv(node):
    """Exact truncating division for signed-64-compatible inputs and constants.

    For an integer positive divisor, trunc(trunc(x) / d) == trunc(x / d).
    Truncating Decimal *before* casting prevents Trino decimal division rounding
    a value just below a bucket boundary up into the next bucket.
    """
    from sqlglot import parse_one
    if len(node.expressions) != 2:
        unsupported("intDiv requires two arguments. Write ClickHouse SQL such as intDiv(annual_salary, 25000).")
    value, divisor = node.expressions
    info = require_type(value, "intDiv")
    if (not info.intdiv_safe or info.kind not in {"integer", "decimal"}
            or not isinstance(divisor, exp.Literal) or divisor.is_string
            or not divisor.this.isdigit() or not 0 < int(divisor.this) <= 9223372036854775807):
        unsupported("intDiv currently supports signed-64-compatible integers or Decimal precision up to 18, divided by a positive integer literal. Write ClickHouse SQL using intDiv(annual_salary, 25000); replace a scalar WITH constant with its literal if needed. For other types, return the numeric value for downstream bucketing or use an explicit safe-range cast only when it preserves your intended values.")
    numerator = value.copy()
    if info.kind == "decimal":
        numerator = exp.Anonymous(this="TRUNCATE", expressions=[numerator])
    numerator = exp.Cast(this=numerator, to=exp.DataType.build("BIGINT"))
    division = parse_one("x / y", read="trino")
    division.set("this", numerator)
    division.set("expression", exp.Cast(this=divisor.copy(), to=exp.DataType.build("BIGINT")))
    return division


def map_argmax(node, condition=None, window=None):
    info = require_type(node.this, "argMax")
    key_type = require_type(node.expression, "argMax")
    if info.kind not in {"integer", "float", "decimal", "string"} or key_type.kind not in {"integer", "float", "decimal", "string", "datetime"}:
        unsupported("argMax supports scalar values and keys only; tuple/default semantics are not established.")
    valid = exp.and_(exp.Not(this=exp.Is(this=node.this.copy(), expression=exp.Null())), exp.Not(this=exp.Is(this=node.expression.copy(), expression=exp.Null())))
    if condition is not None:
        from app.trino_client import _as_trino_condition
        valid = exp.and_(valid, _as_trino_condition(condition.copy()))
    if window is None:
        result = exp.Filter(this=node.copy(), expression=exp.Where(this=valid))
    else:
        # Trino window aggregates do not accept FILTER. Null both arguments on
        # ineligible rows so a NULL value cannot win through a non-NULL max key.
        aggregate = node.copy()
        aggregate.set("this", exp.Case(ifs=[exp.If(this=valid.copy(), true=node.this.copy())], default=exp.Null()))
        aggregate.set("expression", exp.Case(ifs=[exp.If(this=valid, true=node.expression.copy())], default=exp.Null()))
        result = window.copy()
        result.set("this", aggregate)
    # Nullable either argument makes the aggregate nullable in ClickHouse.
    if info.nullable or key_type.nullable:
        return result
    default = exp.Literal.string("") if info.kind == "string" else exp.Literal.number(0)
    return exp.Coalesce(this=result, expressions=[default])


def map_json_string(node):
    """Safely fold literal JSON string extraction; reject unverified dynamic JSON.

    CH also serializes composite/numeric values, with number normalization that
    differs from Trino JSON functions. Do not silently approximate that contract.
    """
    source, path = node.this, node.expression
    if isinstance(source, exp.Null):
        return exp.Null()
    if not isinstance(source, exp.Literal) or not source.is_string or not isinstance(path, exp.JSONPath):
        unsupported("JSONExtractString on dynamic JSON is not yet semantics-preserving in Trino. Literal JSON with string-key paths is supported.")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                unsupported("JSONExtractString with duplicate JSON keys is not supported.")
            result[key] = value
        return result

    def reject_constant(value):
        unsupported("Non-standard JSON numeric constants are not supported.")

    try:
        value = json.loads(source.this, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        return exp.Literal.string("")
    for component in path.expressions:
        if isinstance(component, exp.JSONPathRoot):
            continue
        if not isinstance(component, exp.JSONPathKey) or not isinstance(component.this, str):
            unsupported("JSONExtractString numeric/dynamic paths are not supported in Trino conversion.")
        value = value.get(component.this) if isinstance(value, dict) else None
    if value is None:
        return exp.Literal.string("")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            unsupported("JSON strings with unpaired Unicode surrogates are not supported.")
        return exp.Literal.string(value)
    unsupported("JSONExtractString serialization of non-string JSON values is not yet supported in Trino conversion.")


def map_length(node):
    info = require_type(node.this, "length")
    if info.kind == "array":
        return exp.Anonymous(this="CARDINALITY", expressions=[node.this.copy()])
    if info.kind in {"string", "null"}:
        return exp.Anonymous(this="LENGTH", expressions=[exp.Anonymous(this="TO_UTF8", expressions=[node.this.copy()])])
    unsupported("length requires a known string or array input in Trino conversion.")


def map_datetime(node):
    import math
    args = node.expressions
    if len(args) != 1:
        unsupported("toDateTime timezone overloads are not yet supported in Trino conversion.")
    value = args[0]
    info = require_type(value, "toDateTime")
    if info.kind == "string":
        import re
        if not isinstance(value, exp.Literal) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}:\d{2})?", value.this):
            unsupported("toDateTime string inputs require an ISO date/time literal without fractional seconds; dynamic string parsing is not yet supported.")
        from datetime import datetime
        try:
            parsed = datetime.fromisoformat(value.this)
        except ValueError:
            unsupported("Invalid ISO date/time literal.")
        # String parsing saturates; Date32/DateTime64 conversion instead wraps
        # in CH, so the latter is rejected below rather than sharing this rule.
        parsed = min(max(parsed, datetime(1970, 1, 1)), datetime(2106, 2, 7, 6, 28, 15))
        return exp.Cast(this=exp.Literal.string(parsed.isoformat(sep=" ")), to=exp.DataType.build("TIMESTAMP(0)"))
    if info.kind == "null":
        return exp.Cast(this=exp.Null(), to=exp.DataType.build("TIMESTAMP(0)"))
    if info.kind == "datetime":
        unsupported("toDateTime on date/timestamp inputs has source-specific range wrapping; use a supported integer epoch or ISO literal.")
    if info.kind == "float":
        if not isinstance(value, exp.Literal) or not math.isfinite(float(value.this)):
            unsupported("toDateTime floating-point expressions require a finite explicit integer epoch.")
    if info.kind not in {"integer", "float"}:
        unsupported("Unsupported toDateTime input type; decimal epochs require an explicit integer conversion.")
    # CH DateTime saturates at UInt32 limits and truncates fractional seconds.
    clamped = exp.Anonymous(this="GREATEST", expressions=[exp.Literal.number(0), exp.Anonymous(this="LEAST", expressions=[exp.Literal.number(4294967295), value.copy()])])
    seconds = exp.Anonymous(this="TRUNCATE", expressions=[exp.Cast(this=clamped, to=exp.DataType.build("DOUBLE"))])
    return exp.Cast(this=exp.Anonymous(this="FROM_UNIXTIME", expressions=[seconds]), to=exp.DataType.build("TIMESTAMP(0)"))


def normalize_semantics(tree):
    # Snapshot types before replacing child nodes (casts can lose nullable flags
    # during target rendering). Store on each original expression for builders.
    for node in tree.walk():
        node.meta["ch_value_type"] = value_type(node)
    for comparison in tree.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
        for literal, other in ((comparison.this, comparison.expression),
                               (comparison.expression, comparison.this)):
            info = value_type(other)
            if isinstance(literal, exp.Boolean) and info and info.kind == "string":
                unsupported(
                    "Cannot compare a string expression with a boolean literal. "
                    "For an omitted blueprint slot, replace the entire predicate "
                    "with its optional_pattern, not just the value placeholder."
                )
    for node in reversed(list(tree.walk())):
        if isinstance(node, exp.ParameterizedAgg):
            node.replace(exact_median(node))
        elif isinstance(node, (exp.Window, exp.Filter)):
            if isinstance(node, exp.Filter) and isinstance(node.parent, exp.Window):
                continue
            window = node if isinstance(node, exp.Window) else None
            base = node.this
            condition = None
            if isinstance(base, exp.Filter):
                condition, base = base.expression.this, base.this
            elif isinstance(node, exp.Filter):
                condition = node.expression.this
            if isinstance(base, (exp.Sum, exp.Avg)):
                node.replace(aggregate(base, condition, window))
            elif isinstance(base, exp.ArgMax):
                node.replace(map_argmax(base, condition, window))
            elif window is not None and isinstance(base, (exp.Anonymous, exp.AnonymousAggFunc)) and base.name.lower() in _CONDITIONAL_AGGREGATES:
                node.replace(conditional_aggregate(base, window=node))
        elif isinstance(node, (exp.Sum, exp.Avg)):
            if isinstance(node.parent, (exp.Window, exp.Filter)):
                continue
            node.replace(aggregate(node))
        elif isinstance(node, exp.DateDiff) and node.text("unit").upper() == "DAY":
            if value_type(node) is None:
                unsupported("Day dateDiff requires known date/timestamp inputs without an explicit timezone override.")
            # CH counts calendar-day boundaries; Trino timestamp date_diff
            # counts complete days. Cast both endpoints to DATE first.
            node.set("this", exp.Cast(this=node.this.copy(), to=exp.DataType.build("DATE")))
            node.set("expression", exp.Cast(this=node.expression.copy(), to=exp.DataType.build("DATE")))
        elif isinstance(node, exp.ArgMax):
            if isinstance(node.parent, (exp.Window, exp.Filter)):
                continue
            node.replace(map_argmax(node))
        elif isinstance(node, exp.Length) and node.args.get("binary"):
            node.replace(map_length(node))
        elif isinstance(node, exp.JSONExtractScalar):
            node.replace(map_json_string(node))
        elif isinstance(node, (exp.Anonymous, exp.AnonymousAggFunc)):
            name = node.name.lower()
            if name == "toyear":
                replacement = map_to_year(node)
                replacement.meta["ch_value_type"] = value_type(node)
                node.replace(replacement)
            elif name == "toint64":
                replacement = map_to_int64(node)
                replacement.meta["ch_value_type"] = value_type(node)
                node.replace(replacement)
            elif name == "intdiv":
                node.replace(map_intdiv(node))
            elif name == "todatetime":
                node.replace(map_datetime(node))
            elif name == "tonullable" and len(node.expressions) == 1:
                child = node.expressions[0].copy()
                child.meta["ch_value_type"] = value_type(node)
                node.replace(child)
            elif name in _CONDITIONAL_AGGREGATES:
                if isinstance(node.parent, exp.Window):
                    continue
                node.replace(conditional_aggregate(node))
