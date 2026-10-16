"""Authored schema contract used by validation; never performs metadata I/O."""

from __future__ import annotations

from app.config import get_settings


def get_catalog_schema() -> dict[str, dict[str, str]]:
    from app.trino_metadata import catalog_schema

    return catalog_schema(get_settings())


def invalidate_catalog_cache() -> None:
    """Compatibility hook for callers resetting the authored catalog in tests."""
    from app.semantic_catalog.loader import invalidate_semantic_catalog_cache

    invalidate_semantic_catalog_cache()
