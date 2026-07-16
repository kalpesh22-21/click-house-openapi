"""Semantic Catalog overlay for `getTableSchema` (D83/D84).

See `app/semantic_catalog/loader.py` for the copy-in provenance note and
`app/semantic_catalog/overlay.py` for the merge + scope-filter pipeline.
"""

from app.semantic_catalog.loader import (
    get_catalog_sha,
    get_semantic_catalog,
    invalidate_semantic_catalog_cache,
    load_semantic_catalog,
)
from app.semantic_catalog.overlay import build_table_schema_response

__all__ = [
    "build_table_schema_response",
    "get_catalog_sha",
    "get_semantic_catalog",
    "invalidate_semantic_catalog_cache",
    "load_semantic_catalog",
]
