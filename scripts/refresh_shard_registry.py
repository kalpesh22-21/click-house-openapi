#!/usr/bin/env python3
"""Refresh the MySQL shard-registry snapshot consumed by app/shard_routing.py.

Fetches the paycompy/DBA shard registry (instanceLocation.php) ONCE, OFFLINE,
and writes a validated snapshot JSON (default docker/trino/shards.json) that drives BOTH:

  * runtime client_code RANGE -> Trino catalog resolution (app/shard_routing.py), and
  * Trino connector-catalog generation (scripts/setup_trino_catalogs.py).

Ground truth:
    https://itdba.paycomhq.com/private/api/instanceLocation/instanceLocation.php
    (interactive explorer: https://itdba.paycomhq.com/public/instanceLocation/viewOnly.php)

Replica handling mirrors paycompy (modules/sql): registry rows are grouped per
shard by (env_type, database, clientstart, clientend) and ONE read-preferred
node is kept per shard, ranked by the registry's `used_for` role
(GR-Node3 -> GR-Node2 -> GR-Node1 -> DC1 -> anything else, deterministic
tiebreak on host then port). GR-Node1 is NEVER selected for PROD shards; a PROD
shard whose only row(s) are GR-Node1 is dropped with a warning on stderr.

Payroll shards (mysqldb=payroll, name=bigdata_shard — the hhckey-sharded store)
are EXCLUDED explicitly: hhckey sharding is out of scope. By default rows for
ALL env_types are kept (the runtime filters by TRINO_ENV_TYPE), so one snapshot
serves every environment; --env-type and --client narrow the snapshot BEFORE
replica dedupe and catalog naming (e.g. a QA2-only single-shard testing
snapshot for one tenant), and the resolved --mysqldb/--name/--env-type/--client
selection is recorded under the snapshot's "filters" metadata key.

Catalog names use mysql_shard_<env>_<range-hash>, derived from environment,
database and client bounds. Filters and replica changes do not rename a range.
MIGRATION from numbered catalogs: regenerate connector catalogs and render/apply
views from the SAME refreshed snapshot before restarting the API with that
snapshot. Existing numbered snapshots remain readable until migrated; restarting
the API is required because the runtime registry cache lasts for the process.

No credentials are fetched or stored: snapshot hosts/ports are connection
metadata only, and the script never prints anything but summary counts. TLS is
verified with the platform trust store (SSL_CERT_FILE/SSL_CERT_DIR are honoured
for corporate CAs). Use --input to build the snapshot from a locally saved
export when the endpoint is unreachable from this host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
# Direct CLI runs (`python scripts/refresh_shard_registry.py`) put scripts/ on
# sys.path, not the repo root; ensure `app` is importable either way.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.errors import QueryValidationError  # noqa: E402
from app.shard_routing import (ShardRegistryError, validate_identifier, validate_shard_registry)  # noqa: E402

DEFAULT_OUTPUT = ROOT / "docker/trino/shards.json"
SOURCE_URL = "https://itdba.paycomhq.com/private/api/instanceLocation/instanceLocation.php"
FETCH_TIMEOUT_SECONDS = 30

# Registry `used_for` read-preference ranking (lower wins). Mirrors the head of
# paycompy.modules.sql's used_for_priority_order; every other role ranks last.
_USED_FOR_RANK = {"GR-Node3": 0, "GR-Node2": 1, "GR-Node1": 2, "DC1": 3}
_OTHER_RANK = 4

# Payroll/hhckey-sharded rows are out of scope for client-code routing and are
# excluded explicitly no matter which filters the operator supplies.
_PAYROLL_MYSQLDBS = frozenset({"payroll", "payroll_master"})
_PAYROLL_NAME_PREFIX = "bigdata"

DEFAULT_MYSQLDBS = ["dbonline"]
ALLOWED_MYSQLDBS = ("dbonline", "dbpcm")


def _clean(value: Any) -> str | None:
    """Normalise registry strings: strip, and map empty to None (paycompy hygiene)."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_payroll_row(row: dict[str, Any]) -> bool:
    """True for normalised rows of the hhckey-sharded payroll store (out of scope)."""
    return (row.get("database") in _PAYROLL_MYSQLDBS) or (
        (row.get("name") or "").lower().startswith(_PAYROLL_NAME_PREFIX)
    )


def _normalise_env_types(env_types: Iterable[str] | None) -> list[str] | None:
    """Dedupe operator --env-type filters (case-insensitive); None = keep all envs.

    Matching is case-insensitive but the recorded/returned values keep the
    operator's original (stripped) casing for audit metadata. An empty result
    (no values, or only blanks) normalises to None, i.e. no env filter.
    """
    if env_types is None:
        return None
    seen: set[str] = set()
    out: list[str] = []
    for env in env_types:
        text = str(env).strip()
        key = text.casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(text)
    return out or None


def _normalise_client_codes(clients: Iterable[str] | None) -> list[str] | None:
    """Validate/dedupe operator --client codes; None = keep all shard groups.

    A code must survive the registry range format the rest of the pipeline
    treats as valid: a non-empty printable string (the same shape as the
    clientstart/clientend values it is range-matched against). Anything else is
    a hard ValueError so main() renders a clean `error:` exit instead of a
    silently empty snapshot. Dedupe preserves first-seen order. An empty input
    normalises to None (no client filter).
    """
    if clients is None:
        return None
    seen: set[str] = set()
    out: list[str] = []
    for code in clients:
        text = code.strip() if isinstance(code, str) else ""
        if not text or not text.isprintable():
            raise ValueError(
                f"invalid --client code {code!r}: must be a non-empty printable "
                "string matching the registry clientstart/clientend format"
            )
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out or None


def _normalise_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Project a raw instanceLocation row to snapshot fields; None if unusable.

    A row is unusable when it carries no resolvable host (mysqlhost, falling
    back to mysqlip), no env_type (null/blank -- grouping and catalog naming
    key off it, so an empty env would only surface later as a crash or an
    unmatchable entry), or its mysqlport cannot be coerced to an int -- such a
    row could never back a Trino catalog, so it is skipped (and counted)
    rather than poisoning the snapshot.
    """
    if not isinstance(raw, dict):
        raise ValueError("registry rows must be objects")
    host = _clean(raw.get("mysqlhost")) or _clean(raw.get("mysqlip"))
    if host is None:
        return None
    env_type = _clean(raw.get("env_type"))
    if env_type is None:
        return None
    try:
        port = int(str(raw.get("mysqlport")).strip())
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    return {
        "env_type": env_type.upper(),
        "database": _clean(raw.get("mysqldb")),
        "name": _clean(raw.get("name")),
        "host": host,
        "port": port,
        "clientstart": _clean(raw.get("clientstart")),
        "clientend": _clean(raw.get("clientend")),
        "used_for": _clean(raw.get("used_for")),
    }


def _node_rank(row: dict[str, Any]) -> tuple[int, str, int]:
    """Read-preference sort key: rank, then host, then port (deterministic)."""
    return (_USED_FOR_RANK.get(row.get("used_for") or "", _OTHER_RANK), row["host"], row["port"])


def _pick_read_node(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the read-preferred replica of one shard group; None when unschedulable.

    GR-Node1 rows are removed from consideration for PROD shards before ranking
    (paycompy: "Function won't use GR-Node1 for PROD shards"); a PROD shard left
    with no other replica is dropped by the caller rather than silently routed
    to a write-primary node. All rows in a group share env_type, so the PROD
    check uses the first row.
    """
    candidates = rows
    if rows and rows[0].get("env_type") == "PROD":
        candidates = [row for row in rows if row.get("used_for") != "GR-Node1"]
    if not candidates:
        return None
    return min(candidates, key=_node_rank)


def _group_key(row: dict[str, Any]) -> tuple[str, str, str | None, str | None]:
    return (row["env_type"], row["database"], row["clientstart"], row["clientend"])


def select_shard_entries(
    rows: list[dict[str, Any]],
    mysqldbs: Iterable[str],
    name: str,
    *,
    env_types: Iterable[str] | None = None,
    clients: Iterable[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter, dedupe and select read nodes from normalised registry rows.

    Pure: returns (selected entries WITHOUT catalog names, report). The report
    carries counts and the dropped-PROD-shard labels for the caller to print.

    Filter order is deliberate: payroll exclusion and the mysqldb/name row
    filter run first, then the optional --env-type row filter (case-insensitive)
    within those already-selected rows, then the optional --client group filter
    keeps only shard groups whose clientstart <= code <= clientend range
    contains at least one given code (inclusive lexicographic, the registry
    semantics app/shard_routing.py uses at runtime). Both run BEFORE replica
    dedupe and catalog naming so a narrowed run still produces deterministic
    range-derived catalog names independent of the selected subset.
    """
    wanted_dbs = set(mysqldbs)
    env_filter = _normalise_env_types(env_types)
    client_filter = _normalise_client_codes(clients)
    wanted_envs = {env.casefold() for env in env_filter} if env_filter else None
    report: dict[str, Any] = {
        "input_rows": len(rows),
        "payroll_excluded": 0,
        "unusable_rows": 0,
        "env_type_excluded_rows": 0,
        "client_excluded_groups": 0,
        "filtered_rows": 0,
        "shard_groups": 0,
        "prod_gr_node1_dropped": [],
        "per_env_db": {},
    }
    usable: list[dict[str, Any]] = []
    for raw in rows:
        row = _normalise_row(raw)
        if row is None:
            report["unusable_rows"] += 1
            continue
        usable.append(row)
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in usable:
        if _is_payroll_row(row):
            report["payroll_excluded"] += 1
            continue
        if row["database"] not in wanted_dbs or row["name"] != name:
            continue
        if wanted_envs is not None and row["env_type"].casefold() not in wanted_envs:
            report["env_type_excluded_rows"] += 1
            continue
        report["filtered_rows"] += 1
        groups.setdefault(_group_key(row), []).append(row)
    if client_filter:
        kept: dict[tuple, list[dict[str, Any]]] = {}
        for key, members in groups.items():
            _, _, clientstart, clientend = key
            # A group with an unbounded/half-bounded range can never positively
            # contain a code (and half-bounded rows are rejected at runtime
            # load), so it never survives a client filter.
            if (
                clientstart is not None
                and clientend is not None
                and any(clientstart <= code <= clientend for code in client_filter)
            ):
                kept[key] = members
        report["client_excluded_groups"] = len(groups) - len(kept)
        groups = kept
    selected: list[dict[str, Any]] = []
    for key, members in groups.items():
        chosen = _pick_read_node(members)
        if chosen is None:
            env_type, database, clientstart, clientend = key
            report["prod_gr_node1_dropped"].append(f"{env_type}/{database} {clientstart}-{clientend}")
            continue
        selected.append(
            {
                "host": chosen["host"],
                "port": chosen["port"],
                "database": chosen["database"],
                "env_type": chosen["env_type"],
                "clientstart": chosen["clientstart"],
                "clientend": chosen["clientend"],
            }
        )
        env_db = (chosen["env_type"], chosen["database"])
        report["per_env_db"][env_db] = report["per_env_db"].get(env_db, 0) + 1
    report["shard_groups"] = len(selected)
    return selected, report


def assign_catalog_names(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Name each range independently of filtering, ordering and replica choice.

    A versioned hash of (environment, database, start, end) identifies a range.
    Host/port are deliberately excluded so replica failover keeps the name.
    Range changes create new names, preventing stale catalogs from silently
    pointing newly assigned ranges at a different source.
    """
    named = []
    for entry in entries:
        env = entry["env_type"].upper()
        identity = ["v1", env, entry["database"], entry["clientstart"], entry["clientend"]]
        digest = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:16]
        catalog = f"mysql_shard_{env.lower()}_{digest}"
        try:
            validate_identifier(catalog)
        except QueryValidationError as exc:
            raise ValueError(f"registry env_type {env!r} produces an invalid catalog name") from exc
        named.append({**entry, "env_type": env, "catalog": catalog})
    return sorted(named, key=lambda e: (e["env_type"], e["database"], e["clientstart"] or "", e["clientend"] or ""))


def build_snapshot(
    rows: list[dict[str, Any]],
    *,
    mysqldbs: Iterable[str],
    name: str,
    source: str,
    env_types: Iterable[str] | None = None,
    clients: Iterable[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Full pure pipeline: rows -> snapshot dict (+ report), ready to serialise.

    The resolved filter selection is recorded under "filters" for auditability
    (which mysqldb/name and any env_type/client narrowing produced this
    snapshot); consumers read only "mysql_shards"/"clickhouse", so the block is
    metadata only and never changes routing behavior.
    """
    mysqldbs = list(mysqldbs)
    selected, report = select_shard_entries(rows, mysqldbs, name, env_types=env_types, clients=clients)
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "filters": {
            "mysqldb": list(dict.fromkeys(mysqldbs)),
            "name": name,
            "env_type": _normalise_env_types(env_types),
            "client": _normalise_client_codes(clients),
        },
        "mysql_shards": assign_catalog_names(selected),
        # Sibling ClickHouse connector entry consumed by setup_trino_catalogs.py.
        "clickhouse": {"catalog": "clickhouse", "host": "host.docker.internal", "port": 8123},
    }
    try:
        validate_shard_registry(snapshot)
    except ShardRegistryError as exc:
        raise ValueError(f"invalid shard snapshot: {exc}") from exc
    return snapshot, report


def _load_rows_from_payload(payload: Any, origin: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError(f"{origin}: expected a JSON object with a 'data' array")
    return payload["data"]


def fetch_rows(url: str) -> list[dict[str, Any]]:
    """Fetch the registry over verified TLS; raise ValueError with cause on failure."""
    request = urllib.request.Request(url, headers={"User-Agent": "clickhouse-api-refresh-shard-registry"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS, context=ssl.create_default_context()) as response:
            if response.status != 200:
                raise ValueError(f"registry endpoint returned HTTP {response.status}")
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise ValueError(f"registry endpoint returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ValueError(f"registry endpoint unreachable: {exc}") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"registry endpoint did not return valid JSON: {exc}") from exc
    return _load_rows_from_payload(payload, url)


def _describe_filters(
    mysqldbs: list[str],
    name: str,
    env_filter: list[str] | None,
    client_filter: list[str] | None,
) -> str:
    """Human-readable active-filter summary for refusal/summary messages."""
    parts = [f"--mysqldb {list(mysqldbs)}", f"--name {name!r}"]
    if env_filter:
        parts.append(f"--env-type {env_filter}")
    if client_filter:
        parts.append(f"--client {client_filter}")
    return ", ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Build the snapshot from a locally saved instanceLocation export instead of fetching "
        "(use when the endpoint is not reachable from this host).",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--mysqldb",
        action="append",
        choices=ALLOWED_MYSQLDBS,
        default=None,
        help="Registry mysqldb(s) to include (repeatable). Default: dbonline. "
        "'payroll' (hhckey sharding) is out of scope and always excluded.",
    )
    parser.add_argument("--name", default="online", help="Registry application name to include (default: online).")
    parser.add_argument(
        "--env-type",
        dest="env_type",
        action="append",
        default=None,
        metavar="ENV",
        help="Keep only registry rows with this env_type (case-insensitive, repeatable; e.g. "
        "--env-type QA2). Default: keep ALL env_types (the runtime filters by TRINO_ENV_TYPE).",
    )
    parser.add_argument(
        "--client",
        action="append",
        default=None,
        metavar="CODE",
        help="Keep only shard groups whose clientstart..clientend range contains this client code "
        "(repeatable; a group survives when it contains ANY listed code). Applied within the "
        "already-selected env/mysqldb rows. Default: keep all groups.",
    )
    args = parser.parse_args()

    mysqldbs = list(dict.fromkeys(args.mysqldb or DEFAULT_MYSQLDBS))

    try:
        # Validate/normalise filters BEFORE any fetch: an invalid --client code
        # is a clean `error:` exit, never a traceback or a wasted network call.
        env_filter = _normalise_env_types(args.env_type)
        client_filter = _normalise_client_codes(args.client)
        if args.input is not None:
            source = str(args.input)
            rows = _load_rows_from_payload(json.loads(args.input.read_text(encoding="utf-8")), source)
        else:
            source = SOURCE_URL
            rows = fetch_rows(SOURCE_URL)
        snapshot, report = build_snapshot(
            rows,
            mysqldbs=mysqldbs,
            name=args.name,
            source=source,
            env_types=env_filter,
            clients=client_filter,
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if report["shard_groups"] == 0:
        print(
            "error: no shard entries survived filtering; refusing to write an empty "
            f"registry snapshot (active filters: {_describe_filters(mysqldbs, args.name, env_filter, client_filter)}; "
            "check them against the source export).",
            file=sys.stderr,
        )
        return 1

    for label in report["prod_gr_node1_dropped"]:
        print(
            f"warning: PROD shard {label} has only GR-Node1 replica(s); GR-Node1 is "
            "never selected for PROD -- shard dropped from the snapshot",
            file=sys.stderr,
        )

    # Atomic publish: serialise beside the target and os.replace() it into
    # place, so a crash or I/O failure mid-write can never leave a truncated
    # snapshot behind (the consumers fail closed on partial JSON; this removes
    # the window entirely). The temp file is removed if publish fails.
    tmp_output = args.output.with_suffix(args.output.suffix + ".tmp")
    try:
        tmp_output.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_output, args.output)
    except OSError as exc:
        tmp_output.unlink(missing_ok=True)
        print(f"error: could not write snapshot to {args.output}: {exc}", file=sys.stderr)
        return 1

    per_env_db = ", ".join(
        f"{env}/{db}={count}"
        for (env, db), count in sorted(report["per_env_db"].items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1])))
    )
    print(f"source rows: {report['input_rows']} ({source})")
    print(f"excluded payroll rows: {report['payroll_excluded']} (hhckey sharding is out of scope)")
    print(f"skipped unusable rows: {report['unusable_rows']} (no host/IP, missing env_type, or invalid port)")
    if env_filter:
        print(f"env_type filter {env_filter}: excluded {report['env_type_excluded_rows']} row(s)")
    if client_filter:
        print(f"client filter {client_filter}: excluded {report['client_excluded_groups']} shard group(s)")
    print(f"filtered rows kept: {report['filtered_rows']} -> shard groups: {report['shard_groups']}")
    print(f"per env_type/database: {per_env_db}")
    print(f"dropped PROD GR-Node1-only shards: {len(report['prod_gr_node1_dropped'])}")
    print(f"wrote {len(snapshot['mysql_shards'])} mysql shard catalogs + clickhouse to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())