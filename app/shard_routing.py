"""ITDBA-backed routing to pre-existing Trino catalogs; never performs DDL."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.http_security import validate_https_endpoint, verified_opener
from app.config import Settings
from app.errors import QueryValidationError

logger = logging.getLogger(__name__)
MAX_REGISTRY_BYTES = 16 * 1024 * 1024
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")

class ShardRegistryError(RuntimeError):
    """Invalid registry contents; never expose contents to callers."""

class ShardRegistryUnavailable(ShardRegistryError):
    """No usable inventory after a fetch failure."""

def validate_identifier(value: str) -> str:
    """Configuration identifiers, never caller-supplied SQL fragments."""
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise QueryValidationError(
            "Invalid Trino routing identifier in server configuration.",
            "INVALID_TRINO_ROUTING",
        )
    return value


def _catalog_name_for(entry: dict) -> str:
    name = "__".join(entry[k] for k in ("name", "site", "level", "database")).replace("-", "_")
    # Validate the unchanged grammar without destroying DBA catalog casing.
    validate_identifier(name.lower())
    return name


def _validate_entry(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ShardRegistryError("entry must be an object")
    fields = ("env_type", "database", "clientstart", "clientend", "name", "site", "level")
    if any(not isinstance(raw.get(k), str) or not raw[k].strip() for k in fields):
        raise ShardRegistryError("missing routing field")
    entry = {k: raw[k].strip() for k in fields}
    if entry["env_type"] not in {"PROD", "QA2", "DEV"}:
        raise ShardRegistryError("unsupported environment")
    if entry["clientstart"] > entry["clientend"] or any(
        not entry[k].isprintable() for k in ("clientstart", "clientend")
    ):
        raise ShardRegistryError("invalid client range")
    try:
        validate_identifier(entry["database"])
        _catalog_name_for(entry)
    except QueryValidationError as exc:
        raise ShardRegistryError("invalid routing identifier") from exc
    host, port = raw.get("host", ""), raw.get("port", 3306)
    if not isinstance(host, str) or type(port) is not int or not 1 <= port <= 65535:
        raise ShardRegistryError("invalid informational host/port")
    entry.update(host=host.strip(), port=port)
    return entry


def _collapse(entries: list[dict]) -> list[dict]:
    groups = {}
    for entry in entries:
        key = tuple(entry[k] for k in ("env_type", "database", "clientstart", "clientend"))
        target = tuple(entry[k] for k in ("name", "site", "level"))
        group = groups.setdefault(key, {})
        group[target] = entry
    for key, group in groups.items():
        if len(group) != 1:
            logger.error("group conflict env_type=%s range=%s-%s distinct_targets=%d",
                         key[0], key[2], key[3], len(group))
            raise ShardRegistryError("conflicting range targets")
    result = [next(iter(group.values())) for group in groups.values()]
    if not 1 <= len(result) <= 10_000:
        raise ShardRegistryError("entry count outside 1..10000")
    return result


def normalize_itdba_rows(rows: list[dict], database: str = "dbonline", name: str = "online") -> list[dict]:
    if not isinstance(rows, list):
        raise ShardRegistryError("expected row list")
    entries = []
    for row in rows:
        if not isinstance(row, dict):
            raise ShardRegistryError("invalid API row")
        if str(row.get("active", "")).strip().lower() != "true":
            continue
        db, app = str(row.get("mysqldb", "")).strip(), str(row.get("name", "")).strip()
        if db in {"payroll", "payroll_master"} or app.startswith("bigdata"):
            continue
        if db != database or app != name or not str(row.get("clientstart") or "").strip():
            continue
        env = str(row.get("env_type", "")).strip().upper()
        if env not in {"PROD", "QA2", "DEV"}:
            continue
        if not str(row.get("level") or "").strip():
            continue
        try:
            port = int(row.get("mysqlport") or 3306)
        except (TypeError, ValueError) as exc:
            raise ShardRegistryError("invalid port") from exc
        entries.append(_validate_entry(dict(
            env_type=env, database=db, name=app, site=row.get("site"),
            level=row.get("level"), clientstart=row.get("clientstart"),
            clientend=row.get("clientend"), host=row.get("mysqlhost") or row.get("mysqlip") or "",
            port=port,
        )))
    return _collapse(entries)


def snapshot_for(entries: list[dict]) -> dict:
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "source": "itdba", "entries": entries}


def validate_shard_registry(payload: Any) -> list[dict]:
    if not isinstance(payload, dict) or payload.get("source") != "itdba" or not isinstance(payload.get("entries"), list):
        raise ShardRegistryError("invalid snapshot schema")
    try:
        stamp = datetime.fromisoformat(payload["generated_at"])
        if stamp.tzinfo is None or stamp.timestamp() > time.time() + 60:
            raise ValueError("invalid timestamp")
    except (KeyError, TypeError, ValueError) as exc:
        raise ShardRegistryError("invalid cache timestamp") from exc
    if not 1 <= len(payload["entries"]) <= 10_000:
        raise ShardRegistryError("entry count outside 1..10000")
    return _collapse([_validate_entry(raw) for raw in payload["entries"]])


def atomic_write_snapshot(path: str | Path, payload: dict) -> None:
    path = Path(path)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            tmp = stream.name
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def _fetch_itdba_rows(url: str, timeout: float) -> list[dict]:
    validate_https_endpoint(url)
    if not url:
        raise OSError("registry endpoint is not configured")
    request = urllib.request.Request(url, headers={"User-Agent": "clickhouse-api-shard-registry"})
    opener = verified_opener()
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError("unexpected registry status")
        body = response.read(MAX_REGISTRY_BYTES + 1)
        if len(body) > MAX_REGISTRY_BYTES:
            raise ShardRegistryError("registry response exceeds size limit")
        payload = json.loads(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ShardRegistryError("invalid API envelope")
    return payload["data"]


_registry_cache = {}
_catalog_cache = {}
_registry_lock = threading.RLock()
_catalog_lock = threading.Lock()


def invalidate_shard_registry_cache() -> None:
    with _registry_lock:
        _registry_cache.clear()
    with _catalog_lock:
        _catalog_cache.clear()


def _get_shard_registry(settings: Settings) -> list[dict]:
    key = (settings.trino_itdba_url, settings.trino_mysql_schema, settings.trino_itdba_name)
    with _registry_lock:
        cached = _registry_cache.get(key)
        if cached and time.monotonic() - cached[0] < settings.trino_itdba_cache_ttl_seconds:
            return cached[1]
        try:
            rows = _fetch_itdba_rows(settings.trino_itdba_url, settings.trino_itdba_timeout_seconds)
            entries = normalize_itdba_rows(rows, settings.trino_mysql_schema, settings.trino_itdba_name)
        except (OSError, ValueError, ShardRegistryError) as exc:
            # Bad live inventory is a routing defect, not an availability fallback.
            if isinstance(exc, ShardRegistryError):
                raise
            if settings.trino_itdba_allow_stale and settings.trino_itdba_cache_path:
                try:
                    with Path(settings.trino_itdba_cache_path).open("rb") as stream:
                        body = stream.read(MAX_REGISTRY_BYTES + 1)
                    if len(body) > MAX_REGISTRY_BYTES:
                        raise ShardRegistryError("cache exceeds size limit")
                    payload = json.loads(body)
                    entries = validate_shard_registry(payload)
                    if any(e["database"] != settings.trino_mysql_schema or e["name"] != settings.trino_itdba_name for e in entries):
                        raise ShardRegistryError("cache filters differ")
                    age = max(0, int(time.time() - datetime.fromisoformat(payload["generated_at"]).timestamp()))
                    logger.warning("shard registry serving stale entries: event=shard_registry_stale_served age_seconds=%d entries=%d", age, len(entries))
                    return entries
                except (OSError, ValueError, ShardRegistryError):
                    logger.error("shard registry cache unusable")
            logger.error("shard registry fetch failed and no usable cache: failure_class=%s", type(exc).__name__)
            raise ShardRegistryUnavailable("registry unavailable") from exc
        _registry_cache[key] = (time.monotonic(), entries)
        if settings.trino_itdba_cache_path:
            try:
                atomic_write_snapshot(settings.trino_itdba_cache_path, snapshot_for(entries))
            except OSError:
                logger.warning("shard registry cache write failed")
        return entries


def _catalog_name_verified(name: str, settings: Settings) -> str:
    if not settings.trino_catalog_check_enabled:
        return name
    key = (settings.trino_host, settings.trino_port, settings.trino_http_scheme, settings.trino_user, settings.trino_password)
    try:
        with _catalog_lock:
            cached = _catalog_cache.get(key)
            if not cached or time.monotonic() - cached[0] >= settings.trino_catalog_check_ttl_seconds:
                from app.trino_client import _connect
                conn = cursor = None
                try:
                    conn = _connect(settings, settings.trino_user)
                    cursor = conn.cursor()
                    cursor.execute("SHOW CATALOGS")
                    names = [row[0] for row in cursor.fetchall() if not row[0].lower().startswith("err__")]
                finally:
                    try:
                        if cursor is not None:
                            cursor.close()
                    finally:
                        if conn is not None:
                            conn.close()
                cached = (time.monotonic(), names)
                _catalog_cache[key] = cached
        if name in cached[1]:
            return name
        matches = [candidate for candidate in cached[1] if candidate.lower() == name.lower()]
        if len(matches) == 1:
            validate_identifier(matches[0].lower())
            return matches[0]
        logger.warning("Trino catalog check found no unique catalog; using derived name")
    except Exception:
        logger.warning("Trino catalog check unavailable; using derived name")
    return name


def _resolve_from_entries(
    client_code: str,
    entries: list[dict[str, Any]],
    env_type: str,
    mysqldb: str,
) -> str:
    """Range-match ``client_code`` against ``entries`` (pure; no I/O, no caching).

    Filters by env/database, skips rows with no clientstart, and compares with
    plain inclusive lexicographic range membership (the DBA registry semantics).
    Exactly one match is required; both zero and multiple matches fail closed
    with a generic message that does not echo registry contents.
    """
    matches = [
        entry
        for entry in entries
        if entry["env_type"] == env_type
        and entry["database"] == mysqldb
        and entry["clientstart"]  # non-empty clientstart; unbounded rows never match
        and entry["clientstart"] <= client_code <= entry["clientend"]
    ]
    if not matches:
        raise QueryValidationError(
            "No MySQL shard route is configured for the authenticated tenant.",
            "MYSQL_SHARD_NOT_FOUND",
        )
    if len(matches) > 1:
        # Registry hygiene defect (overlapping ranges). Server log carries the
        # diagnosis; the caller-facing message stays generic.
        logger.error(
            "shard registry: %d ranges match the tenant for env_type=%s database=%s; refusing to route",
            len(matches),
            env_type,
            mysqldb,
        )
        raise QueryValidationError(
            "Multiple MySQL shard routes match the authenticated tenant.",
            "MYSQL_SHARD_AMBIGUOUS",
        )
    return matches[0]["catalog"]


def resolve_mysql_shard(client_code: str, settings: Settings) -> str:
    if not isinstance(client_code, str) or not client_code:
        raise QueryValidationError("No MySQL shard route is configured for the authenticated tenant.", "MYSQL_SHARD_NOT_FOUND")
    try:
        entries = _get_shard_registry(settings)
    except ShardRegistryUnavailable as exc:
        raise QueryValidationError("MySQL shard routing is temporarily unavailable.", "MYSQL_SHARD_REGISTRY_UNAVAILABLE") from exc
    except ShardRegistryError as exc:
        logger.error("shard registry invalid: failure_class=%s", type(exc).__name__)
        raise QueryValidationError("MySQL shard routing is unavailable (registry snapshot missing or invalid).", "MYSQL_SHARD_REGISTRY_INVALID") from exc
    # Supply derived catalog names to the unchanged range matcher.
    candidates = [dict(entry, catalog=_catalog_name_for(entry)) for entry in entries]
    catalog = _resolve_from_entries(client_code, candidates, settings.trino_env_type, settings.trino_mysql_schema)
    catalog = _catalog_name_verified(catalog, settings)
    from app.catalog_verification import require_verified_shard
    require_verified_shard(catalog, settings)
    return catalog
