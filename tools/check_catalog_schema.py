#!/usr/bin/env python3
"""Validate semantic references against physical/candidate metadata.

CI: python tools/check_catalog_schema.py --schema physical-schema.json
Live metadata: python tools/check_catalog_schema.py --live
JSON shape: {"database.table": {"column": "ClickHouseType"}}.
Use --table database.table for a targeted deployment check. No row data is read.
Free-text descriptions/ambiguities are documentation, not machine references.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import sqlglot
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.semantic_catalog.loader import load_semantic_catalog

_COLUMN_FIELDS = {
    'column', 'description_col', 'effective_col', 'date_col', 'end_col',
    'start_col', 'implicit_version_column',
}
_COLUMN_LISTS = {
    'grain', 'primary_key', 'join_on', 'implicit_join_scope',
    'hidden_columns', 'implicit_filters', 'order_by', 'implicit_order_scope',
}


def check_schema(catalog: dict, physical: dict) -> list[str]:
    errors = []

    def check(table, column, path):
        if column == '*':
            return
        if column not in physical.get(table, {}):
            errors.append(f'{path}: {table}.{column} absent from physical/candidate schema')

    def walk(table, value, path):
        if isinstance(value, list):
            for i, item in enumerate(value):
                walk(table, item, f'{path}[{i}]')
        elif isinstance(value, dict):
            for key, item in value.items():
                location = f'{path}.{key}'
                if key in _COLUMN_FIELDS and isinstance(item, str):
                    check(table, item, location)
                elif key in _COLUMN_LISTS and isinstance(item, list):
                    for column in item:
                        check(table, column, location)
                elif key == 'resolve_via':
                    match = re.fullmatch(r"resolveValues\(\s*([\w]+)\s*,\s*(['\"]).*\2\s*\)", item)
                    if not match:
                        errors.append(f'{location}: invalid resolveValues target')
                    else:
                        target = match[1]
                        check(table, target, location)
                        definition = catalog[table].get('columns', {}).get(target)
                        if definition is None:
                            errors.append(f'{location}: {target} has no semantic column definition')
                        elif definition.get('description_col'):
                            check(table, definition['description_col'], location)
                elif key == 'joins' and isinstance(item, str):
                    # Support the catalog's explicit column-range notation.
                    refs = re.fullmatch(r'([\w.]+) through ([\w.]+)', item)
                    targets = [item]
                    if refs:
                        first = re.fullmatch(r'(.*?)(\d+)', refs[1])
                        last = re.fullmatch(r'(.*?)(\d+)', refs[2])
                        if first and last and first[1] == last[1]:
                            targets = [first[1] + str(i) for i in range(int(first[2]), int(last[2]) + 1)]
                    for target in targets:
                        parts = target.split('.')
                        if len(parts) in (2, 3) and all(re.fullmatch(r'\w+', p) for p in parts):
                            target_table = '.'.join(parts[:-1])
                            if len(parts) == 2:
                                target_table = table.split('.')[0] + '.' + target_table
                            check(target_table, parts[-1], location)
                        else:
                            errors.append(f'{location}: unsupported join reference {target!r}')
                elif key == 'predicate' and isinstance(item, str):
                    # Legacy catalog also has prose in predicate fields. Only SQL
                    # predicates (comparisons) are machine-readable references.
                    sql = re.sub(r'\{\w+\}', 'NULL', item)
                    try:
                        expression = sqlglot.parse_one(sql, dialect='clickhouse')
                    except sqlglot.errors.SqlglotError:
                        continue
                    if expression and any(expression.find_all(exp.Predicate)):
                        for column in expression.find_all(exp.Column):
                            check(table, column.name, location)
                else:
                    walk(table, item, location)

    for table, entry in catalog.items():
        if table not in physical:
            errors.append(f'{table}: table absent from physical/candidate schema')
        for column in entry.get('columns', {}):
            check(table, column, f'{table}.columns')
        walk(table, entry, table)
    return sorted(set(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--schema', type=Path)
    source.add_argument('--live', action='store_true')
    parser.add_argument('--table')
    args = parser.parse_args()
    catalog = load_semantic_catalog()
    if args.table:
        if args.table not in catalog:
            parser.error('table is not in the semantic catalog')
        catalog = {args.table: catalog[args.table]}
    if args.live:
        from app.catalog import _build_catalog
        physical = _build_catalog()  # Fresh metadata; do not trust a stale TTL cache.
    else:
        physical = json.loads(args.schema.read_text())
    errors = check_schema(catalog, physical)
    for error in errors:
        print(error, file=sys.stderr)
    print(f'Catalog schema check: {len(errors)} errors')
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
