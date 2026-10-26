"""Conservative ClickHouse construct normalization before Trino routing.

Unknown types are not grounds for guessing aggregate defaults or precision.
"""
import re

from sqlglot import exp
from app.errors import QueryValidationError


def unsupported(message):
    raise QueryValidationError(message, "TRINO_UNSUPPORTED_SQL")


def inline_scalar_constants(tree):
    """Inline literal WITH bindings lexically, preserving nested/lambda shadows.

    Expressions and scalar subqueries require a separate name-resolution contract.
    Never replace table names, qualified columns, or arbitrary identifiers.
    """
    def visit(node, bindings):
        bindings = dict(bindings)
        with_node = node.args.get("with_")
        if with_node:
            retained = []
            for cte in with_node.expressions:
                bindings.pop(cte.alias, None)
                if not isinstance(cte.this, exp.Query):
                    value = cte.this
                    if not (isinstance(value, (exp.Literal, exp.Boolean, exp.Null)) or
                            isinstance(value, exp.Neg) and isinstance(value.this, exp.Literal)):
                        unsupported("Only literal scalar WITH bindings are supported in Trino conversion.")
                    bindings[cte.alias] = value
                else:
                    retained.append(cte)
            with_node.set("expressions", retained)
            if not retained:
                node.set("with_", None)
        if isinstance(node, exp.Lambda):
            for argument in node.expressions:
                bindings.pop(argument.name, None)
        if isinstance(node, exp.Column) and not node.table and node.name in bindings:
            node.replace(bindings[node.name].copy())
            return
        for child in list(node.iter_expressions()):
            visit(child, bindings)
    visit(tree, {})


def normalize_constructs(tree):
    """Normalize supported signatures, and reject known unsafe conversions."""
    for node in tree.find_all(exp.ParameterizedAgg):
        unsupported("Parameterized aggregates, including exact quantiles, are not yet supported in Trino conversion. Exact results are never replaced with estimates.")
    for node in reversed(list(tree.walk())):
        if isinstance(node, (exp.Anonymous, exp.AnonymousAggFunc)):
            name = node.name.lower()
            args = node.expressions
            if name in {"formatreadablequantity"}:
                unsupported(f"{name} is not yet supported with equivalent type, empty-set, and precision semantics in Trino conversion.")
            arities = {"today": {0}, "groupuniqarray": {1}, "arraysort": {1}, "arraystringconcat": {1, 2}}
            if name not in arities:
                continue
            if len(args) not in arities[name]:
                unsupported(f"Unsupported {name} signature in Trino conversion.")
            if name == "today":
                node.replace(exp.CurrentDate())
            elif name == "groupuniqarray":
                value = args[0]
                aggregate = exp.Filter(
                    this=exp.ArrayAgg(this=exp.Distinct(expressions=[value.copy()])),
                    expression=exp.Where(this=exp.Not(this=exp.Is(this=value.copy(), expression=exp.Null()))),
                )
                node.replace(exp.Coalesce(this=aggregate, expressions=[exp.Array(expressions=[])]))
            elif name == "arraysort":
                node.replace(exp.Anonymous(this="ARRAY_SORT", expressions=args))
            else:
                # This signature is supported for string arrays. Trino rejects
                # non-string arrays instead of silently formatting their values.
                node.replace(exp.Anonymous(this="ARRAY_JOIN", expressions=[args[0], args[1] if len(args) == 2 else exp.Literal.string("")]))
        elif isinstance(node, (exp.Dot, exp.Column)):
            position = node.expression if isinstance(node, exp.Dot) else node.this
            if isinstance(position, exp.Literal) and not position.is_string:
                if not position.this.isdigit() or int(position.this) < 1:
                    unsupported("Tuple positions must be positive integer constants.")
                base = node.this if isinstance(node, exp.Dot) else node.args.get("table")
                if base is None:
                    unsupported("Unsupported positional tuple reference.")
                # offset=1 prevents dialect index rebasing: ROW uses 1-based fields.
                node.replace(exp.Bracket(this=base.copy(), expressions=[position.copy()], offset=1))
        elif isinstance(node, (exp.Add, exp.Sub)) and isinstance(node.this, exp.CurrentDate):
            amount = node.expression.unnest()
            if isinstance(amount, exp.Interval):
                literal = amount.this
                if (amount.text("unit").upper() != "DAY" or not isinstance(literal, exp.Literal)
                        or not re.fullmatch(r"-?\d+", literal.this)):
                    unsupported("Date offsets require an integer DAY interval in Trino conversion.")
                amount = exp.convert(int(literal.this))
            elif not (isinstance(amount, exp.Literal) and not amount.is_string and amount.this.isdigit()
                      or isinstance(amount, exp.Neg) and isinstance(amount.this, exp.Literal)
                      and not amount.this.is_string and amount.this.this.isdigit()):
                unsupported("Date offsets must be integer day literals in Trino conversion.")
            if isinstance(node, exp.Sub):
                amount = exp.Neg(this=amount.copy())
            node.replace(exp.Anonymous(this="DATE_ADD", expressions=[exp.Literal.string("day"), amount, node.this]))


def preserve_paging_order(tree):
    """Carry projected ordering through SELECT * paging wrappers.

    Trino may discard a derived table ORDER BY when its parent applies paging.
    Only identity wrappers are admitted. A projected source column may be
    referenced by its unique output alias; unprojected sort keys remain rejected.
    """
    for select in tree.find_all(exp.Select):
        if not (select.args.get("limit") or select.args.get("offset")) or select.args.get("order"):
            continue
        if len(select.expressions) != 1 or not isinstance(select.expressions[0], exp.Star):
            continue
        if any(select.args.get(key) for key in ("joins", "where", "group", "having", "qualify", "distinct")):
            continue
        source = select.args.get("from_")
        source = source.this if source is not None else None
        if not isinstance(source, exp.Subquery) or not isinstance(source.this, exp.Select):
            continue
        order = source.this.args.get("order")
        if order is None:
            continue
        names = source.this.named_selects
        copied = order.copy()
        for item in copied.expressions:
            key = item.this
            if isinstance(key, exp.Column) and not key.table and names.count(key.name) == 1:
                continue
            if isinstance(key, exp.Column):
                matches = [projection for projection in source.this.expressions
                           if (projection.this if isinstance(projection, exp.Alias) else projection) == key]
                if len(matches) == 1 and names.count(matches[0].alias_or_name) == 1:
                    item.set("this", exp.column(matches[0].alias_or_name, quoted=True))
                    continue
            if isinstance(key, exp.Literal) and not key.is_string and key.this.isdigit() and 1 <= int(key.this) <= len(names):
                continue
            unsupported("Paging over an ordered subquery requires ordering by projected column names or positions.")
        select.set("order", copied)
