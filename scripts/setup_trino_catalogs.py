#!/usr/bin/env python3
"""Generate static Trino connector catalogs from a shard-registry snapshot.

The inventory is the paycompy/DBA shard-registry snapshot produced by
scripts/refresh_shard_registry.py (default docker/trino/shards.json; the
committed docker/trino/shards.example.json shows the shape). Only each shard's
catalog/host/port (and optional user_env/password_env overrides) are consumed;
the routing fields (database/env_type/clientstart/clientend) are used for
client-code range resolution at query time, not here. The same snapshot file
also feeds app/shard_routing.py (TRINO_SHARD_REGISTRY_PATH).

The clickhouse catalog's connection URL is derived per-key from the app .env
(--env-file, default the repo-root .env): CLICKHOUSE_HOST / CLICKHOUSE_PORT
override the inventory clickhouse block when present and non-empty, and
CLICKHOUSE_DATABASE (path) / CLICKHOUSE_SECURE (ssl query) have no inventory
counterpart. A missing env file is silently skipped (CI, example-inventory
runs) and the inventory values then apply verbatim. Only those four keys are
read; credentials and every other .env entry are never written to catalogs.

No source DB connections or DDL. Passwords remain Trino environment references.
Existing files are protected unless --overwrite is explicitly supplied.

View templates (docker/trino/views/*.txt, e.g. employee.txt) are additionally
rendered ONCE PER mysql_shards entry into <views-dir>/rendered/<catalog>/
<stem>.sql, substituting {{SHARD_CATALOG}} with the entry's catalog and
{{PROC_CENTER}} with its proc-center (the entry's optional `proc_center` key,
else --proc-center; missing BOTH is a hard failure naming the shard — a
security-relevant value is never silently defaulted). When any view renders, a
memory-connector <view-catalog>.properties (default dbpcm_datawarehouse) is
emitted beside the source catalogs — DEV-ONLY RAM storage: coordinator restarts
drop the views, so re-apply with scripts/apply_trino_views.py after every
restart. A missing/empty --views-dir simply skips view rendering (no view
catalog). See app/semantic_catalog/data/employee.yaml for the logical column
contract the employee view must emit; the view's proc_center column is a
hidden identity pin ON TOP of that catalogued set — NOT declared in
employee.yaml, and live discovery never exposes non-catalogued columns.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Direct CLI runs (`python scripts/setup_trino_catalogs.py`) put scripts/ on
# sys.path, not the repo root; ensure `app` is importable either way (mirrors
# scripts/refresh_shard_registry.py).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.errors import QueryValidationError  # noqa: E402
from app.shard_routing import validate_identifier  # noqa: E402

_HOST_PATTERN = r"[A-Za-z0-9][A-Za-z0-9.-]*"
_PORT_PATTERN = r"[0-9]+"
_DATABASE_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"
_SECURE_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _value(value: object, pattern: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"Invalid {label} in shard inventory")
    return value


def _env_value(value: str, pattern: str, label: str) -> str:
    if not re.fullmatch(pattern, value):
        raise ValueError(f"Invalid {label} in .env")
    return value


def load_env_file(path: Path) -> dict[str, str]:
    """Minimal .env KEY=VALUE parser: no third-party deps, no shell execution.

    Handles blank lines, `#` comment lines, an optional `export ` prefix and
    optional single/double quotes around the value. The LAST occurrence wins
    on duplicate keys; lines without `=` or with an empty key are ignored.
    Callers decide which keys to consume — this returns everything, secrets
    included, so never interpolate the result wholesale into output.
    """
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _clickhouse_url(source: dict, env: Mapping[str, str]) -> tuple[str, bool]:
    """Build the clickhouse JDBC URL; non-empty .env CLICKHOUSE_* keys win.

    Returns (url, used_env): used_env is True when any of CLICKHOUSE_HOST /
    CLICKHOUSE_PORT / CLICKHOUSE_SECURE / CLICKHOUSE_DATABASE contributed, so
    the caller can note the URL source in the generated file's header.
    """
    used_env = False
    env_host = (env.get("CLICKHOUSE_HOST") or "").strip()
    if env_host:
        host = _env_value(env_host, _HOST_PATTERN, "CLICKHOUSE_HOST")
        used_env = True
    else:
        host = _value(source.get("host"), _HOST_PATTERN, "hostname")
    port = source.get("port", 8123)
    env_port = (env.get("CLICKHOUSE_PORT") or "").strip()
    if env_port:
        port = int(_env_value(env_port, _PORT_PATTERN, "CLICKHOUSE_PORT"))
        used_env = True
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError(
            "Invalid CLICKHOUSE_PORT in .env"
            if env_port
            else "Invalid port in shard inventory"
        )
    database = (env.get("CLICKHOUSE_DATABASE") or "").strip()
    if database:
        database = _env_value(database, _DATABASE_PATTERN, "CLICKHOUSE_DATABASE")
        used_env = True
    secure = (env.get("CLICKHOUSE_SECURE") or "").strip()
    if secure:
        used_env = True
    url = f"jdbc:clickhouse://{host}:{port}"
    if database:
        url += f"/{database}"
    if secure.lower() in _SECURE_TRUTHY:
        # Dev-coordinator TLS without truststore plumbing: ssl=true upgrades the
        # wire to TLS while sslmode=none skips certificate chain verification,
        # so the local unauthenticated coordinator can reach a TLS-enforcing
        # ClickHouse without baking CA certs into the container. Revisit with a
        # real truststore before any shared/non-local deployment.
        url += "?ssl=true&sslmode=none"
    return url, used_env


def render_catalogs(inventory: dict, env: Mapping[str, str] | None = None) -> dict[str, str]:
    files = {}
    sources = [("mysql", s) for s in inventory.get("mysql_shards", [])]
    if inventory.get("clickhouse"):
        sources.append(("clickhouse", inventory["clickhouse"]))
    if not sources:
        raise ValueError("Inventory must contain at least one source")
    for kind, source in sources:
        name = _value(source.get("catalog"), r"[a-z][a-z0-9_]*", "catalog name")
        if kind == "clickhouse":
            url, used_env = _clickhouse_url(source, env or {})
        else:
            host = _value(source.get("host"), _HOST_PATTERN, "hostname")
            port = source.get("port", 3306)
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("Invalid port in shard inventory")
            # zeroDateTimeBehavior=convertToNull: QA2 rows carry '0000-00-00'
            # date/datetime values the MySQL driver otherwise refuses to
            # materialize ("Zero date value prohibited"), aborting any query
            # that touches those rows; mapping them to NULL preserves the row.
            url = f"jdbc:{kind}://{host}:{port}?zeroDateTimeBehavior=convertToNull"
        if name in files:
            raise ValueError("Duplicate catalog name in shard inventory")
        prefix = f"TRINO_{kind.upper()}"
        user_env = _value(
            source.get("user_env", f"{prefix}_USER"), r"[A-Z][A-Z0-9_]*", "user_env"
        )
        password_env = _value(
            source.get("password_env", f"{prefix}_PASSWORD"),
            r"[A-Z][A-Z0-9_]*",
            "password_env",
        )
        lines = [
            "# Generated by scripts/setup_trino_catalogs.py; restart Trino after changes.",
        ]
        if kind == "clickhouse":
            lines.append(
                "# connection-url derived from .env CLICKHOUSE_HOST/PORT/SECURE/DATABASE"
                if used_env
                else "# connection-url derived from the inventory clickhouse block"
            )
        lines += [
            f"connector.name={kind}",
            f"connection-url={url}",
            f"connection-user=${{ENV:{user_env}}}",
            f"connection-password=${{ENV:{password_env}}}",
        ]
        if kind == "mysql":
            # Dev-coordinator metadata cache: collapses repeated remote JDBC
            # metadata lookups during analysis/planning (measured 5-9x an+plan
            # reduction on the 23-44-table views; P1A experiment). Metadata
            # only (schemas/tables/statistics TTLs inherit this value per
            # Trino 483); no data, plans, or RLS literals are cached.
            # Production TTL choice is deployment-local.
            lines += [
                "metadata.cache-ttl=10m",
                "metadata.cache-missing=true",
            ]
        if kind == "clickhouse":
            lines.append("clickhouse.map-string-as-varchar=true")
        files[name] = "\n".join(lines) + "\n"
    return files


def write_catalogs(
    inventory: dict,
    output_dir: Path,
    overwrite: bool = False,
    env: Mapping[str, str] | None = None,
) -> list[Path]:
    # Validate everything (inventory AND .env overrides) before writing.
    rendered = render_catalogs(inventory, env)
    paths = [output_dir / f"{name}.properties" for name in rendered]
    if not overwrite and any(path.exists() for path in paths):
        raise ValueError(
            "Catalog already exists; use --overwrite to replace generated catalogs"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    for path, contents in zip(paths, rendered.values()):
        path.write_text(contents, encoding="utf-8")
    return paths


# --- per-shard view rendering (memory-connector dbpcm_datawarehouse catalog) -
#
# Templates are plain SQL files (.txt) with two REQUIRED placeholders,
# substituted globally (INCLUDING the header comments, so a rendered file's
# header documents its own target catalog/proc-center):
#
#   {{SHARD_CATALOG}} - the shard's MySQL source catalog (the routing
#       boundary's catalog name, e.g. mysql_shard_qa2_01);
#   {{PROC_CENTER}}   - the proc-center literal emitted as the proc_center
#       output column (a hidden identity pin; NOT declared in
#       app/semantic_catalog/data/employee.yaml — live discovery never exposes
#       non-catalogued columns).
#
# The templates place views in the view catalog by literal name (the committed
# template uses `dbpcm_datawarehouse.`; --view-catalog must match the name
# inside the templates — _validate_view_templates enforces that agreement). RLS contract (owner decision): the field-1 proc_center WHERE
# guard is intentionally absent from the template; tenant isolation is the
# field-2 client_code pin plus the field-3 on_eeaccess username EXISTS pin
# (identity transport fields are proc_center|client_code|username — see
# docs/trino.md "Identity and row security"). The render-time assertions below
# (guard absent, both pins present, no leftovers) exist so this contract
# cannot silently rot.

VIEW_PLACEHOLDER_CATALOG = "{{SHARD_CATALOG}}"
VIEW_PLACEHOLDER_PROC_CENTER = "{{PROC_CENTER}}"
# Field-1 proc_center guard, dropped by owner decision: must survive neither
# the template conversion nor any render.
_VIEW_GUARD = "split_part(CURRENT_USER, '|', 1)"
# RLS pins that MUST survive every render (fields 2 and 3 of the identity
# transport): client_code pin and on_eeaccess username pin.
_VIEW_RLS_PINS = ("split_part(CURRENT_USER, '|', 2)", "split_part(CURRENT_USER, '|', 3)")
# Unconverted-template fingerprints: stale literals from the pre-template file.
_VIEW_STALE_LITERALS = ("mysql_shard_07", "'pc1'")


def _view_identifier(value: str, label: str) -> str:
    """validate_identifier, re-raised as ValueError for this CLI's clean error path."""
    try:
        return validate_identifier(value)
    except QueryValidationError as exc:
        raise ValueError(f"Invalid {label} for view rendering: {value!r}") from exc


def load_view_templates(views_dir: Path) -> dict[str, str]:
    """Load `<views_dir>/*.txt` view templates keyed by file stem ({} if none).

    The rendered/ output subdirectory lives under --views-dir but contains only
    .sql files, so re-runs never ingest their own output.
    """
    if not views_dir.is_dir():
        return {}
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(views_dir.glob("*.txt"))
    }


def _validate_view_templates(
    templates: Mapping[str, str], view_catalog: str = "dbpcm_datawarehouse"
) -> None:
    """Refuse stale/unconverted templates; validate the generic render contract.

    Includes a template/--view-catalog agreement check: every template's DDL
    must place its schema and view in the selected view catalog (checked on the
    post-placeholder form — the {{SHARD_CATALOG}} substitution never alters the
    `<view_catalog>.` prefix). Otherwise renders land in a catalog other than
    the one <view-catalog>.properties exposes, which only surfaces at query
    time; fail here naming the mismatch instead.
    """
    for name, text in templates.items():
        for stale in _VIEW_STALE_LITERALS:
            if stale in text:
                raise ValueError(
                    f"View template {name!r} still contains hardcoded {stale}: "
                    f"convert it to the {VIEW_PLACEHOLDER_CATALOG} / "
                    f"{VIEW_PLACEHOLDER_PROC_CENTER} placeholders before rendering"
                )
        if _VIEW_GUARD in text:
            raise ValueError(
                f"View template {name!r} still contains the dropped field-1 "
                "proc_center WHERE guard; owner decision keeps tenant isolation "
                "to the field-2 client_code pin plus field-3 on_eeaccess pin"
            )
        for pin in _VIEW_RLS_PINS:
            if pin not in text:
                raise ValueError(
                    f"View template {name!r} is missing the {pin} RLS pin; "
                    "tenant isolation must survive every render"
                )
        if not (VIEW_PLACEHOLDER_CATALOG in text and VIEW_PLACEHOLDER_PROC_CENTER in text):
            raise ValueError(
                f"View template {name!r} must contain both "
                f"{VIEW_PLACEHOLDER_CATALOG} and {VIEW_PLACEHOLDER_PROC_CENTER} "
                "placeholders"
            )
        for ddl in (
            f"CREATE SCHEMA IF NOT EXISTS {view_catalog}.",
            f"CREATE VIEW {view_catalog}.",
        ):
            if ddl not in text:
                raise ValueError(
                    f"View template {name!r} must target the selected view "
                    f"catalog {view_catalog!r} but its DDL lacks {ddl!r}; align "
                    "the template's catalog prefix with --view-catalog"
                )


def _render_view(
    template: str, shard_catalog: str, proc_center: str, stem: str
) -> str:
    """Substitute placeholders and assert the rendered RLS contract (pure)."""
    rendered = template.replace(VIEW_PLACEHOLDER_CATALOG, shard_catalog).replace(
        VIEW_PLACEHOLDER_PROC_CENTER, proc_center
    )
    if "{{" in rendered or "}}" in rendered:
        raise ValueError(
            f"View render of {stem!r} for shard {shard_catalog!r} left "
            "unresolved placeholders (unknown placeholder in template?)"
        )
    if _VIEW_GUARD in rendered:
        raise ValueError(
            f"View render of {stem!r} for shard {shard_catalog!r} contains the "
            "dropped field-1 proc_center WHERE guard"
        )
    missing_pins = [pin for pin in _VIEW_RLS_PINS if pin not in rendered]
    if missing_pins:
        raise ValueError(
            f"View render of {stem!r} for shard {shard_catalog!r} lost RLS "
            f"pin(s): {missing_pins}"
        )
    # The stale literals cannot survive a converted template, and the only
    # legitimate 'pc1' in a render is the substituted proc-center column.
    expected_pc1 = template.count(f"'{VIEW_PLACEHOLDER_PROC_CENTER}'")
    actual_pc1 = rendered.count("'pc1'")
    if actual_pc1 != (expected_pc1 if proc_center == "pc1" else 0):
        raise ValueError(
            f"View render of {stem!r} for shard {shard_catalog!r} has "
            f"{actual_pc1} 'pc1' literal(s); expected "
            + (f"{expected_pc1} (the proc_center column)" if proc_center == "pc1" else "0")
        )
    return rendered


def render_views(
    inventory: dict,
    templates: Mapping[str, str],
    *,
    view_catalog: str = "dbpcm_datawarehouse",
    proc_center: str | None = None,
) -> dict[tuple[str, str], str]:
    """Render every view template once per mysql_shards entry (pure).

    Returns {(shard_catalog, template_stem): rendered_sql}; {} when there are
    no templates (a missing/empty --views-dir simply skips view rendering).
    Per-shard proc-center resolution: the snapshot entry's optional
    `proc_center` key wins, else the render-time default (`--proc-center`);
    missing BOTH fails loud, naming the shard — proc_center feeds the RLS
    identity contract, so a default must never be silently invented.
    """
    if not templates:
        return {}
    _view_identifier(view_catalog, "view catalog")
    _validate_view_templates(templates, view_catalog)
    rendered: dict[tuple[str, str], str] = {}
    for index, entry in enumerate(inventory.get("mysql_shards", [])):
        if not isinstance(entry, dict):
            raise ValueError(f"mysql_shards[{index}]: entry must be an object")
        shard_catalog = _view_identifier(
            str(entry.get("catalog")), f"shard catalog for mysql_shards[{index}]"
        )
        shard_proc_center = entry.get("proc_center") or proc_center
        if not shard_proc_center:
            raise ValueError(
                f"mysql_shards[{index}] ({shard_catalog}): no proc_center "
                "available for view rendering; add a `proc_center` key to the "
                "snapshot entry or pass --proc-center (never silently defaulted)"
            )
        shard_proc_center = _view_identifier(
            str(shard_proc_center), f"proc_center for shard {shard_catalog}"
        )
        for stem, template in templates.items():
            rendered[(shard_catalog, stem)] = _render_view(
                template, shard_catalog, shard_proc_center, stem
            )
    return rendered


def render_view_catalog(view_catalog: str = "dbpcm_datawarehouse") -> str:
    """Properties file for the memory-connector view catalog (dev-only RAM store)."""
    name = _view_identifier(view_catalog, "view catalog")
    lines = [
        "# Generated by scripts/setup_trino_catalogs.py; restart Trino after changes.",
        "# DEV-ONLY: the memory connector stores schemas/views in coordinator RAM.",
        "# Views VANISH on every restart - re-apply after each up/restart with:",
        "#   python scripts/apply_trino_views.py",
        "connector.name=memory",
    ]
    return "\n".join(lines) + "\n"


def write_views(
    inventory: dict,
    templates: Mapping[str, str],
    rendered_root: Path,
    *,
    view_catalog: str = "dbpcm_datawarehouse",
    proc_center: str | None = None,
    overwrite: bool = False,
) -> list[Path]:
    """Write per-shard rendered views under <rendered_root>/<catalog>/<stem>.sql."""
    rendered = render_views(
        inventory, templates, view_catalog=view_catalog, proc_center=proc_center
    )
    if not rendered:
        return []
    paths = sorted(
        rendered_root / catalog / f"{stem}.sql" for catalog, stem in rendered
    )
    if not overwrite and any(path.exists() for path in paths):
        raise ValueError(
            "Rendered views already exist; use --overwrite to replace them"
        )
    for (catalog, stem), contents in rendered.items():
        path = rendered_root / catalog / f"{stem}.sql"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    return paths


def write_view_catalog(
    view_catalog: str, output_dir: Path, overwrite: bool = False
) -> Path:
    """Emit <view_catalog>.properties (memory connector) into the catalog dir."""
    path = output_dir / f"{_view_identifier(view_catalog, 'view catalog')}.properties"
    if path.exists() and not overwrite:
        raise ValueError(
            "Catalog already exists; use --overwrite to replace generated catalogs"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(render_view_catalog(view_catalog), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory", type=Path, default=ROOT / "docker/trino/shards.json"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "docker/trino/catalog"
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / ".env",
        help="App .env supplying CLICKHOUSE_HOST/PORT/SECURE/DATABASE overrides "
        "for the clickhouse catalog URL; a missing file is silently skipped "
        "(inventory values then apply).",
    )
    parser.add_argument(
        "--views-dir",
        type=Path,
        default=ROOT / "docker/trino/views",
        help="Directory of *.txt view templates rendered once per mysql_shards "
        "entry into <views-dir>/rendered/<shard_catalog>/<stem>.sql. A missing "
        "or empty directory skips view rendering entirely (no view catalog is "
        "emitted then either).",
    )
    parser.add_argument(
        "--view-catalog",
        default="dbpcm_datawarehouse",
        help="Memory-connector catalog hosting the rendered views; when any "
        "view renders, <view-catalog>.properties (connector.name=memory, "
        "DEV-ONLY RAM storage) is emitted into the catalog output dir. Must "
        "match the catalog the templates place views in (default "
        "dbpcm_datawarehouse).",
    )
    parser.add_argument(
        "--proc-center",
        default=None,
        metavar="VALUE",
        help="Render-time default proc-center for the {{PROC_CENTER}} "
        "placeholder. A snapshot entry's own `proc_center` key wins; when "
        "neither is present rendering FAILS naming the shard (never silently "
        "defaulted — it feeds the RLS identity contract).",
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    if not args.inventory.is_file():
        parser.exit(
            1,
            f"Shard-registry snapshot not found: {args.inventory}\n"
            "Run scripts/refresh_shard_registry.py first to generate "
            "docker/trino/shards.json (the committed "
            "docker/trino/shards.example.json shows the shape and can still be "
            "passed explicitly via --inventory).\n",
        )
    # Absent env file is normal (CI, example-inventory runs): skip silently.
    env = load_env_file(args.env_file) if args.env_file.is_file() else {}
    rendered_root = args.views_dir / "rendered"
    try:
        inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
        paths = write_catalogs(inventory, args.output_dir, args.overwrite, env=env)
        view_paths = write_views(
            inventory,
            load_view_templates(args.views_dir),
            rendered_root,
            view_catalog=args.view_catalog,
            proc_center=args.proc_center,
            overwrite=args.overwrite,
        )
        if view_paths:
            paths.append(
                write_view_catalog(args.view_catalog, args.output_dir, args.overwrite)
            )
    except (ValueError, OSError, TypeError, AttributeError) as exc:
        parser.exit(1, f"Catalog setup failed: {exc}\n")
    print(
        f"Generated {len(paths)} catalogs in {args.output_dir}. Restart Trino to load them."
    )
    if view_paths:
        print(
            f"Rendered {len(view_paths)} view(s) under {rendered_root}. "
            f"The {args.view_catalog} catalog is DEV-ONLY RAM storage: "
            "re-apply the views after every Trino restart with "
            "scripts/apply_trino_views.py."
        )


if __name__ == "__main__":
    main()