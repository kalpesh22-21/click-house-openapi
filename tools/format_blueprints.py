"""Format corpus SQL without changing its AST; preserve bind tokens and YAML blocks."""
import re
from pathlib import Path
import sqlparse
import sqlglot
from sqlglot import exp
import yaml

ROOT = Path(__file__).resolve().parents[1] / 'app/corpus/data/blueprints'
TOKEN = re.compile(r"('(?:[^'\\]|\\.|'')*')|(--[^\n]*)|(/\*(?:.|\n)*?\*/)|(\{([A-Za-z_][A-Za-z0-9_]*)\})")


def parse(sql):
    return sqlglot.parse_one(TOKEN.sub(lambda m: ':'+m[5] if m[4] else m[0], sql), read='clickhouse')


def format_sql(sql):
    before = parse(sql)
    rendered = sqlparse.format(sql, reindent=True, indent_width=2).strip() + '\n'
    assert parse(rendered) == before, 'Formatting changed the SQL AST'
    return rendered


class Dumper(yaml.SafeDumper):
    pass


def string(dumper, value):
    return dumper.represent_scalar('tag:yaml.org,2002:str', value, style='|' if '\n' in value else None)


Dumper.add_representer(str, string)


def write(path, entry):
    path.write_text(yaml.dump(entry, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=110))


def main():
    for path in sorted(ROOT.glob('*.yaml')):
        entry = yaml.safe_load(path.read_text())
        for node in [entry, *entry.get('composes', [])]:
            if node.get('sql_template'):
                node['sql_template'] = format_sql(node['sql_template'])
        write(path, entry)
    print('Formatted SQL in', len(list(ROOT.glob('*.yaml'))), 'blueprints')


if __name__ == '__main__':
    main()
