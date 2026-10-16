"""Startup-only checks that deployed sources implement the authored contract."""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.config import Settings
from app.principal import Principal, current_principal
from app.trino_metadata import logical_catalog


class CatalogVerificationError(RuntimeError):
    """Safe diagnostics: never include backend exceptions or rendered source SQL."""


def require_verified_shard(shard: str, settings: Settings) -> None:
    verified = settings._verified_catalog_shards
    if verified is not None and shard not in verified:
        from app.errors import QueryValidationError

        raise QueryValidationError(
            "Source target has not passed catalog verification. Restart after provisioning it.",
            "CATALOG_TARGET_UNVERIFIED",
        )


def _unwrap(value: str) -> str:
    value = re.sub(r"\s+", "", value).lower()
    while value.startswith(("nullable(", "lowcardinality(")):
        value = value[value.index("(") + 1 : -1]
    return value


def compatible_type(logical: str, physical: str, *, trino: bool) -> bool:
    """Explicit logical-to-backend policy; unknown conversions fail closed.

    Trino does not carry ClickHouse Nullable/LowCardinality wrappers. Integer
    widening is accepted; decimals require at least the authored scale and
    integer capacity. Timestamp precision may be widened, never truncated.
    """
    expected, actual = _unwrap(logical), _unwrap(physical)
    if not trino:
        # LowCardinality is storage-only; Nullable is a real direct-CH contract.
        def strip_storage(value):
            return re.sub(
                r"lowcardinality\((.*)\)$", r"\1", re.sub(r"\s+", "", value).lower()
            )

        return strip_storage(logical) == strip_storage(physical)
    if expected == "string" or expected.startswith("fixedstring("):
        return bool(re.fullmatch(r"(?:varchar|char)(?:\(\d+\))?", actual))
    if expected in {"date", "date32"}:
        return actual == "date"
    integers = {"tinyint": 8, "smallint": 16, "integer": 32, "bigint": 64}
    match = re.fullmatch(r"(u?)int(8|16|32|64)", expected)
    if match:
        bits = int(match[2]) + bool(match[1])
        return integers.get(actual, 0) >= bits or (
            expected == "uint64" and actual == "decimal(20,0)"
        )
    if expected.startswith("datetime"):
        precision = re.match(r"datetime64\((\d+)", expected)
        required = int(precision[1]) if precision else 0
        observed = re.fullmatch(r"timestamp(?:\((\d+)\))?", actual)
        return bool(observed and int(observed[1] or 3) >= required)
    decimal = re.fullmatch(r"decimal\((\d+),(\d+)\)", expected)
    if decimal:
        observed = re.fullmatch(r"decimal\((\d+),(\d+)\)", actual)
        return bool(
            observed
            and int(observed[2]) >= int(decimal[2])
            and int(observed[1]) - int(observed[2]) >= int(decimal[1]) - int(decimal[2])
        )
    return actual == {"float32": "real", "float64": "double", "bool": "boolean"}.get(
        expected
    )


def verify_columns(
    key: str, entry: dict, columns: list[tuple[str, str]], *, trino: bool
) -> None:
    expected = entry["columns"]
    hidden = set(entry.get("mcp_projection", {}).get("hidden_columns", []))
    actual = dict(columns)
    if len(actual) != len(columns):
        raise CatalogVerificationError(f"{key}: duplicate output columns")
    missing = set(expected) - set(actual)
    extra = set(actual) - set(expected) - hidden
    incompatible = sorted(
        name
        for name in set(expected) & set(actual)
        if not compatible_type(expected[name]["type"], actual[name], trino=trino)
    )
    wrong_order = [name for name in actual if name in expected] != list(expected)
    if missing or extra or incompatible or wrong_order:
        raise CatalogVerificationError(
            f"{key}: missing={sorted(missing)}, unexpected={sorted(extra)}, incompatible={incompatible}, wrong_order={wrong_order}"
        )


def _verification_targets(settings: Settings, needs_mysql: bool) -> list[dict]:
    try:
        targets = json.loads(
            Path(settings.catalog_verification_targets_path).read_text()
        )
        if not isinstance(targets, list) or not targets:
            raise ValueError()
        for target in targets:
            if not isinstance(target, dict) or set(target) - {"shard", "claims"}:
                raise ValueError()
            if not isinstance(target.get("claims"), dict) or not target["claims"]:
                raise ValueError()
            if not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in target["claims"].items()
            ):
                raise ValueError()
            if needs_mysql and not isinstance(target.get("shard"), str):
                raise ValueError()
    except (OSError, ValueError, TypeError):
        raise CatalogVerificationError(
            "Configure CATALOG_VERIFICATION_TARGETS_PATH with verification identities and targets"
        ) from None
    if needs_mysql:
        from app.shard_routing import (
            _catalog_name_for,
            _catalog_name_verified,
            _get_shard_registry,
        )

        try:
            required = {
                _catalog_name_verified(_catalog_name_for(e), settings)
                for e in _get_shard_registry(settings)
                if e["env_type"] == settings.trino_env_type
                and e["database"] == settings.trino_mysql_schema
                and e["clientstart"]
            }
        except Exception:  # noqa: BLE001 — sanitize inventory provider failures
            raise CatalogVerificationError(
                "Unable to verify required shard inventory"
            ) from None
        provided = {t["shard"] for t in targets}
        if not required or provided != required:
            raise CatalogVerificationError(
                "Verification targets must exactly cover the served shard inventory"
            )
    return targets


def verify_catalog(settings: Settings) -> None:
    """Fail startup before serving traffic; bind and reset only configured identities."""
    settings._verified_catalog_shards = frozenset()
    entries = logical_catalog(settings)
    needs_mysql = settings.trino_enabled and any(
        e.get("source") == "mysql" for e in entries.values()
    )
    targets = _verification_targets(settings, needs_mysql)
    for target in targets:
        token = current_principal.set(
            Principal("catalog-verification", target["claims"])
        )
        conn = cursor = None
        try:
            if settings.trino_enabled:
                from app.trino_client import _connect, request_identity
                from app.trino_native_sources import source_relation

                identity = request_identity(settings)
                shard = target.get("shard", "")
                # Check the configured identity really routes to this target, without
                # the published-target gate (publication occurs after all checks).
                if needs_mysql:
                    from app.shard_routing import (
                        _catalog_name_for,
                        _catalog_name_verified,
                        _get_shard_registry,
                        _resolve_from_entries,
                    )

                    routes = [
                        dict(e, catalog=_catalog_name_for(e))
                        for e in _get_shard_registry(settings)
                    ]
                    resolved = _catalog_name_verified(
                        _resolve_from_entries(
                            identity[1],
                            routes,
                            settings.trino_env_type,
                            settings.trino_mysql_schema,
                        ),
                        settings,
                    )
                    if resolved != shard:
                        raise CatalogVerificationError(
                            "Verification identity does not route to its configured shard"
                        )
                conn = _connect(settings, "|".join(identity))
                cursor = conn.cursor()
                for key, entry in entries.items():
                    relation = source_relation(entry, shard, identity, settings)
                    cursor.execute(
                        f"SELECT * FROM {relation.sql(dialect='trino')} AS catalog_source LIMIT 0"
                    )
                    cursor.fetchall()
                    verify_columns(
                        key,
                        entry,
                        [(c[0], str(c[1])) for c in cursor.description or []],
                        trino=True,
                    )
            else:
                from app.clickhouse_client import execute_query
                from app.shard_routing import validate_identifier

                for key, entry in entries.items():
                    database, table = (
                        validate_identifier(entry["database"]),
                        validate_identifier(entry["table"]),
                    )
                    _, rows = execute_query(
                        f"DESCRIBE TABLE `{database}`.`{table}`", settings
                    )
                    verify_columns(
                        key, entry, [(r[0], r[1]) for r in rows], trino=False
                    )
                    execute_query(
                        f"SELECT * FROM `{database}`.`{table}` LIMIT 0", settings
                    )
        except CatalogVerificationError:
            raise
        except Exception:  # noqa: BLE001 — never expose backend SQL or identity values
            raise CatalogVerificationError(
                "Source catalog verification failed; check source availability, contracts and verification permissions"
            ) from None
        finally:
            if settings.trino_enabled:
                from app.trino_client import _close

                _close(cursor, conn)
            current_principal.reset(token)
    settings._verified_catalog_shards = frozenset(
        t["shard"] for t in targets if "shard" in t
    )
