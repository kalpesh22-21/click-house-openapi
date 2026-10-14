"""Tenant -> MySQL shard routing from a TRUSTED server-side registry snapshot.

The registry (``settings.trino_shard_registry_path``) is a JSON snapshot of the
paycompy/DBA shard registry, refreshed OFFLINE by
``scripts/refresh_shard_registry.py`` (ground truth: the DBA
instanceLocation.php document). There is deliberately no live registry fetch at
query time. The snapshot is trusted server-side configuration — never
caller-supplied input: no registry path, host, or shard data is accepted from
the request, and caller-facing errors never echo registry contents.

paycompy shard-resolution model (client-code RANGE matching):

  * each registry row owns a 5-char alphanumeric client-code RANGE
    (``clientstart`` .. ``clientend``, INCLUSIVE both ends, plain lexicographic
    string compare — no normalisation or uppercasing, matching the DBA
    registry's semantics exactly);
  * resolution keeps rows with ``env_type == settings.trino_env_type`` and
    ``database == settings.trino_mysql_schema`` (the MySQL schema setting
    doubles as the registry ``mysqldb`` filter) and a non-empty clientstart,
    then requires EXACTLY ONE range to contain the authenticated client code:
    zero matches -> MYSQL_SHARD_NOT_FOUND, more than one -> MYSQL_SHARD_AMBIGUOUS;
  * payroll shards (mysqldb=payroll / name=bigdata_shard, the hhckey-sharded
    store) are excluded at refresh time and never appear in the snapshot.

Routing is fail-closed: a tenant outside every configured range, an ambiguous
overlap, a malformed entry, or a missing/invalid snapshot all reject the query.
There is no default shard — an unknown tenant must never land on an arbitrary
source.

Caching mirrors ``app/semantic_catalog/loader.get_semantic_catalog``: the
snapshot only changes on a redeploy refresh, so the parsed registry is cached
per absolute path for the life of the process, keyed by path so tests pointing
at different snapshots stay isolated. Load failures are NOT cached (a missing
file that appears later self-heals). ``invalidate_shard_registry_cache()`` is
the explicit escape hatch for tests.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.config import Settings
from app.errors import QueryValidationError

logger = logging.getLogger(__name__)

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")


class ShardRegistryError(RuntimeError):
    """Internal load-time failure: the shard-registry snapshot is missing or malformed.

    Never surfaces to callers directly -- ``resolve_mysql_shard`` converts it to
    a generic fail-closed ``QueryValidationError`` (MYSQL_SHARD_REGISTRY_INVALID)
    while logging the detail server-side only.
    """


def validate_identifier(value: str) -> str:
    """Configuration identifiers, never caller-supplied SQL fragments."""
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise QueryValidationError(
            "Invalid Trino routing identifier in server configuration.",
            "INVALID_TRINO_ROUTING",
        )
    return value


def _require_str(entry: dict, field: str, where: str, *, allow_empty: bool = False) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ShardRegistryError(f"{where}: '{field}' must be a non-empty string")
    return value


def _validate_entry(index: int, raw: Any) -> dict[str, Any]:
    """Validate one ``mysql_shards`` entry and project the routing fields.

    Any structural problem poisons the whole snapshot (fail closed at load
    time): a silently-skipped entry could strand a tenant's range unnoticed.
    ``clientstart``/``clientend`` may be null (row retained but never matched);
    a non-empty clientstart REQUIRES a non-empty clientend so the inclusive
    range compare can never raise or half-match. Empty strings normalise to
    None, mirroring paycompy's ``replace("", NaN)`` registry hygiene.
    """
    where = f"mysql_shards[{index}]"
    if not isinstance(raw, dict):
        raise ShardRegistryError(f"{where}: entry must be an object")
    catalog = _require_str(raw, "catalog", where)
    host = _require_str(raw, "host", where)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ShardRegistryError(f"{where}: invalid host")
    database = _require_str(raw, "database", where)
    env_type = _require_str(raw, "env_type", where)
    try:
        validate_identifier(catalog)
        validate_identifier(database)
        validate_identifier(env_type.lower())
    except QueryValidationError as exc:
        raise ShardRegistryError(f"{where}: invalid routing identifier") from exc
    if env_type != env_type.upper():
        raise ShardRegistryError(f"{where}: 'env_type' must be uppercase")
    port = raw.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ShardRegistryError(f"{where}: 'port' must be an integer in 1..65535")
    clientstart = raw.get("clientstart")
    clientend = raw.get("clientend")
    for field, value in (("clientstart", clientstart), ("clientend", clientend)):
        if value is not None and not isinstance(value, str):
            raise ShardRegistryError(f"{where}: '{field}' must be a string or null")
    if not clientstart:
        clientstart = None
    if not clientend:
        clientend = None
    if clientstart is not None and clientend is None:
        raise ShardRegistryError(
            f"{where}: 'clientend' is required when 'clientstart' is set"
        )
    if clientstart is not None and clientstart > clientend:
        raise ShardRegistryError(f"{where}: reversed client range")
    for value in (clientstart, clientend):
        if value is not None and (not value.isprintable() or value != value.strip()):
            raise ShardRegistryError(f"{where}: invalid client range bound")
    return {
        "catalog": catalog,
        "host": host,
        "port": port,
        "database": database,
        "env_type": env_type,
        "clientstart": clientstart,
        "clientend": clientend,
    }


def validate_shard_registry(payload: Any) -> list[dict[str, Any]]:
    """Validate the shared refresh/runtime contract before a snapshot is used."""
    if not isinstance(payload, dict) or not isinstance(payload.get("mysql_shards"), list):
        raise ShardRegistryError("shard registry must contain a 'mysql_shards' list")
    entries = [_validate_entry(i, raw) for i, raw in enumerate(payload["mysql_shards"])]
    names = [entry["catalog"] for entry in entries]
    if len(names) != len(set(names)):
        raise ShardRegistryError("shard registry contains duplicate catalog names")
    return entries


def load_shard_registry(path: str | Path) -> list[dict[str, Any]]:
    """Parse and validate the registry snapshot at ``path``; fail closed on any defect.

    Returns the validated ``mysql_shards`` entry list (unsorted; resolution
    tolerates any order and a missing file, invalid JSON, a missing
    ``mysql_shards`` list, or any malformed entry raises ShardRegistryError.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ShardRegistryError(f"shard registry unreadable ({exc})") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ShardRegistryError(f"shard registry is not valid JSON ({exc})") from exc
    return validate_shard_registry(payload)


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


# Module-level cache, mirroring app/semantic_catalog/loader.py: loaded once per
# path per process (the snapshot changes only on a refresh+redeploy, unlike a
# live connection). Keyed by resolved absolute path so tests using distinct
# registry files stay isolated; failures are raised before caching.
_registry_cache: dict[str, list[dict[str, Any]]] = {}


def _get_shard_registry(path: str | Path) -> list[dict[str, Any]]:
    key = str(Path(path).expanduser().resolve())
    if key not in _registry_cache:
        entries = load_shard_registry(key)
        _registry_cache[key] = entries
        logger.debug("shard_routing: loaded %d shard entries from %s", len(entries), key)
    return _registry_cache[key]


def invalidate_shard_registry_cache() -> None:
    """Force the next resolution to reload from disk (test escape hatch)."""
    _registry_cache.clear()


def resolve_mysql_shard(client_code: str, settings: Settings) -> str:
    """Return the validated Trino catalog (e.g. mysql_shard_prod_01) for the tenant.

    The authenticated ``client_code`` (identity-validated upstream by
    ``trino_client.request_identity`` — the light non-empty check here is pure
    defence-in-depth) is range-matched against the registry snapshot filtered by
    ``settings.trino_env_type`` and ``settings.trino_mysql_schema``. The result
    passes through ``validate_identifier`` so a hostile or corrupted snapshot
    cannot smuggle a non-identifier catalog name into rewritten SQL.
    """
    if not isinstance(client_code, str) or not client_code:
        raise QueryValidationError(
            "No MySQL shard route is configured for the authenticated tenant.",
            "MYSQL_SHARD_NOT_FOUND",
        )
    try:
        entries = _get_shard_registry(settings.trino_shard_registry_path)
    except ShardRegistryError as exc:
        logger.error("shard registry unavailable: failure_class=%s", type(exc).__name__)
        raise QueryValidationError(
            "MySQL shard routing is unavailable (registry snapshot missing or invalid).",
            "MYSQL_SHARD_REGISTRY_INVALID",
        ) from exc
    catalog = _resolve_from_entries(
        client_code,
        entries,
        settings.trino_env_type,
        settings.trino_mysql_schema,
    )
    return validate_identifier(catalog)
