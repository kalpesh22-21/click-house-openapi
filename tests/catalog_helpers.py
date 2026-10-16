"""Small authored catalogs for service tests without live metadata."""


def authored_catalog(schema):
    return {
        key: {
            "database": key.rsplit(".", 1)[0],
            "table": key.rsplit(".", 1)[1],
            "columns": {name: {"type": kind} for name, kind in columns.items()},
        }
        for key, columns in schema.items()
    }
