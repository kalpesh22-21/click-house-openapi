"""Bind authored native sort defaults for SQL execution tests (not runtime code)."""
from sqlglot import exp


def bind_sort(blueprint, sql=None, direction=None, column=None):
    sql = blueprint['sql_template'] if sql is None else sql
    if '{order_by}' not in sql:
        return sql
    slots = {s['name']: s for s in blueprint['slots']}
    column = column or slots['order_by']['default'][0]['column']
    direction = (direction or slots['order_by']['default'][0]['direction']).strip("'")
    assert column in slots['order_by']['allowed_columns']
    assert direction in {'ASC', 'DESC'}
    return sql.replace('{order_by}',
                       exp.column(column, quoted=True).sql(dialect='clickhouse') + ' ' + direction)
