"""Semantic Catalog overlay for `getTableSchema` (D83/D84).

See `app/semantic_catalog/loader.py` for the copy-in provenance note and
`app/semantic_catalog/overlay.py` for the merge + scope-filter pipeline.
"""

from app.semantic_catalog.loader import (
    BACKEND_SCOPED_NOTE_KEYS,
    get_catalog_sha,
    get_semantic_catalog,
    invalidate_semantic_catalog_cache,
    load_semantic_catalog,
    scoped_notes_for_backend,
)
from app.semantic_catalog.overlay import (
    build_table_schema_response,
    hidden_columns_for,
)

__all__ = [
    "BACKEND_SCOPED_NOTE_KEYS",
    "build_table_schema_response",
    "get_catalog_sha",
    "get_semantic_catalog",
    "hidden_columns_for",
    "invalidate_semantic_catalog_cache",
    "load_semantic_catalog",
    "scoped_notes_for_backend",
]