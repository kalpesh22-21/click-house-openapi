"""Conservative display semantics from catalog columns and SQL expression lineage.

Values stay raw. Unknown meaning stays text/number; aliases alone never establish units.
"""
from __future__ import annotations

from copy import deepcopy
import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

KINDS = {"text", "identifier", "integer", "number", "currency", "percent", "date", "datetime", "boolean"}


def validate_formats(raw):
    if not isinstance(raw, dict):
        raise ValueError("column_formats must be an object")
    for name, fmt in raw.items():
        if not isinstance(name, str) or not name or not isinstance(fmt, dict):
            raise ValueError("format entries require a column name and object")
        if not isinstance(fmt.get("kind"), str) or fmt["kind"] not in KINDS or set(fmt) - {"kind", "currency", "scale", "unit", "precision"}:
            raise ValueError(f"Invalid format for {name}")
        if "currency" in fmt and (fmt["kind"] != "currency" or not re.fullmatch(r"[A-Z]{3}", str(fmt["currency"]))):
            raise ValueError(f"Invalid currency for {name}")
        if fmt["kind"] == "currency" and "currency" not in fmt:
            raise ValueError(f"Currency required for {name}")
        if fmt["kind"] == "percent" and fmt.get("scale") not in ("ratio", "percent"):
            raise ValueError(f"Percent scale required for {name}")
        if "scale" in fmt and fmt["kind"] != "percent":
            raise ValueError(f"Unexpected scale for {name}")
        if "precision" in fmt and (type(fmt["precision"]) is not int or not 0 <= fmt["precision"] <= 8):
            raise ValueError(f"Invalid precision for {name}")
        if "unit" in fmt and (not isinstance(fmt["unit"], str) or not 0 < len(fmt["unit"]) <= 40):
            raise ValueError(f"Invalid unit for {name}")
    return deepcopy(raw)


def column_format(column):
    if column.get("format"):
        return validate_formats({"column": column["format"]})["column"]
    unit = column.get("unit") or column.get("units")
    if unit in {"USD", "USD_per_hour"}:
        return {"kind": "currency", "currency": "USD", **({"unit": "per hour"} if unit.endswith("hour") else {})}
    if unit in {"hours", "miles", "years", "days"}:
        return {"kind": "number", "unit": unit, "precision": 2}
    typ = str(column.get("type", "")).lower()
    if "datetime" in typ:
        return {"kind": "datetime"}
    if "date" in typ:
        return {"kind": "date"}
    if "bool" in typ:
        return {"kind": "boolean"}
    if "int" in typ:
        return {"kind": "integer"}
    if any(t in typ for t in ("float", "decimal", "double")):
        return {"kind": "number"}
    return {"kind": "text"}


def _common_format(formats):
    """Merge display precision only when units, scale and semantic kinds agree."""
    if not formats:
        return {"kind": "text"}
    if all(fmt == formats[0] for fmt in formats):
        return dict(formats[0])
    bases = [{k: v for k, v in fmt.items() if k != "precision"} for fmt in formats]
    if {base["kind"] for base in bases} <= {"integer", "number"}:
        for base in bases:
            base["kind"] = "number"
    if not all(base == bases[0] for base in bases):
        return {"kind": "text"}
    result = bases[0]
    default = 2 if result["kind"] in {"currency", "percent"} else None
    precisions = [fmt.get("precision", default) for fmt in formats]
    if all(p is not None for p in precisions):
        result["precision"] = max(precisions)
    return result


def query_formats(sql, columns, catalog=None):
    """Return formats keyed by exact output names, following aliases/CTEs conservatively."""
    if catalog is None:
        from app.semantic_catalog.loader import get_semantic_catalog
        catalog = get_semantic_catalog()
    fallback = {name: {"kind": "text"} for name in columns}
    try:
        tree = sqlglot.parse_one(sql, dialect="clickhouse")
        outputs = {}
        for scope in traverse_scope(tree):
            sources = {}
            for alias, (_, source) in scope.selected_sources.items():
                if isinstance(source, Scope):
                    sources[alias] = outputs.get(id(source), {})
                else:
                    table = f"{source.db}.{source.name}" if source.db else source.name
                    entry = catalog.get(table)
                    if entry is None:
                        matches = [v for k, v in catalog.items() if k.rsplit('.', 1)[-1] == table]
                        entry = matches[0] if len(matches) == 1 else {}
                    sources[alias] = {name: column_format(info) for name, info in entry.get("columns", {}).items()}

            def infer(node):
                if isinstance(node, (exp.Alias, exp.Paren, exp.Window)):
                    return infer(node.this)
                if isinstance(node, exp.Column):
                    matches = [v[node.name] for k, v in sources.items()
                               if (not node.table or node.table == k) and node.name in v]
                    return matches[0] if len(matches) == 1 else {"kind": "text"}
                if isinstance(node, exp.Literal):
                    return {"kind": "text" if node.is_string else "number"}
                name = (node.name if isinstance(node, (exp.Anonymous, exp.AnonymousAggFunc, exp.CombinedAggFunc, exp.ParameterizedAgg)) else node.sql_name() if isinstance(node, exp.Func) else "").lower()
                if isinstance(node, exp.Count) or name.startswith(("count", "uniq")) or isinstance(node, (exp.RowNumber, exp.Rank, exp.DenseRank)):
                    return {"kind": "integer"}
                if isinstance(node, exp.Cast):
                    original = infer(node.this)
                    if original["kind"] in {"identifier", "currency", "percent"} or original.get("unit"):
                        return original
                    return column_format({"type": node.args["to"].sql()})
                if name.startswith(("tofloat", "todecimal", "toint", "touint")):
                    value = infer(node.expressions[0]) if node.expressions else {"kind": "number"}
                    return value if value["kind"] != "text" else {"kind": "number"}
                if name.startswith(("sumif", "avgif", "quantile", "median")):
                    values = node.args.get("params") or node.expressions
                    return infer(values[0]) if values else {"kind": "number"}
                if isinstance(node, exp.Nullif):
                    return infer(node.this)
                if isinstance(node, exp.Coalesce):
                    values = [infer(c) for c in node.iter_expressions() if not isinstance(c, (exp.Null, exp.Literal))]
                    return values[0] if values and all(v == values[0] for v in values) else {"kind": "text"}
                if name in {"datediff", "date_diff"}:
                    return {"kind": "number"}
                if name in {"todate", "datetrunc", "date_trunc", "timestamp_trunc", "tostartofmonth", "tostartofyear", "tostartofweek"}:
                    return {"kind": "date"}
                if name in {"concat", "concat_ws", "tostring", "formatdatetime"}:
                    return {"kind": "text"}
                if isinstance(node, (exp.Div, exp.Mul)):
                    left, right = infer(node.this), infer(node.expression)
                    if isinstance(node.expression, exp.Literal) and not node.expression.is_string:
                        return left if left["kind"] in {"currency", "number"} else {"kind": "number"}
                    if left["kind"] == "currency" and right["kind"] == "integer" and isinstance(node, exp.Div):
                        return left
                    return {"kind": "number"}
                if isinstance(node, (exp.Min, exp.Max)):
                    return infer(node.this)
                children = [infer(child) for child in node.iter_expressions()
                            if not isinstance(child, (exp.Literal, exp.Identifier, exp.DataType))]
                known = [c for c in children if c["kind"] != "text"]
                if name.startswith(("sum", "avg", "quantile", "median")) or isinstance(node, (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Add, exp.Sub)) or name in {"round", "coalesce", "nullif", "abs", "lag", "lead"}:
                    if known and all(c == known[0] for c in known):
                        fmt = dict(known[0])
                        if isinstance(node, exp.Avg) and fmt["kind"] == "integer":
                            fmt["kind"] = "number"
                        if fmt["kind"] == "identifier" and name.startswith(("sum", "avg")):
                            return {"kind": "number"}
                        return fmt
                    return {"kind": "number"}
                if isinstance(node, exp.Case) or isinstance(node, exp.If):
                    values = ([i.args.get("true") for i in node.args.get("ifs", [])] + [node.args.get("default")]
                              if isinstance(node, exp.Case) else [node.args.get("true"), node.args.get("false")])
                    choices = [infer(v) for v in values if v is not None and not isinstance(v, exp.Null)]
                    return choices[0] if choices and all(c == choices[0] for c in choices) else {"kind": "text"}
                return {"kind": "text"}

            result = {}
            if scope.union_scopes:
                branches = [outputs.get(id(branch), {}) for branch in scope.union_scopes]
                # Set operations align values by position, not the right-hand aliases.
                for index, name in enumerate(branches[0]):
                    formats = [list(branch.values())[index] for branch in branches
                               if index < len(branch)]
                    result[name] = _common_format(formats) if len(formats) == len(branches) else {"kind": "text"}
            else:
                for index, node in enumerate(scope.expression.selects):
                    if isinstance(node, exp.Star):
                        for source in sources.values():
                            result.update(source)
                    else:
                        name = node.alias_or_name or f"__unaliased_{index}"
                        result[name] = infer(node)
            if scope.outer_columns:
                # WITH cte(new_name, ...) and derived-table column alias lists.
                result = {scope.outer_columns[i] if i < len(scope.outer_columns) else name: fmt
                          for i, (name, fmt) in enumerate(result.items())}
            outputs[id(scope)] = result
        result = outputs[id(scope)] if outputs else {}
        return {name: deepcopy(result.get(name, fallback[name])) for name in columns}
    except Exception:
        return fallback


def export_headers(columns, formats):
    headers = []
    for name in columns:
        fmt = formats.get(name, {})
        unit = fmt.get("currency") or (fmt.get("scale") if fmt.get("kind") == "percent" else None)
        if fmt.get("unit"):
            unit = " ".join(v for v in (unit, fmt["unit"]) if v)
        headers.append(f"{name} ({unit})" if unit else name)
    return headers
