#!/usr/bin/env python3
"""Integrity + staleness checks for the Semantic Catalog YAML (D83/D84, Wave 1a).

Direction (post-Wave-1a): the **MCP (`clickhouse-api`) is the source of truth** for
the Semantic Catalog. `app/semantic_catalog/data/*.yaml` is authored/edited HERE,
and the data-analysis-agent's `tests/fixtures/catalog_export.json` is DERIVED from
this MCP via `build_catalog_export()`. (This inverts the earlier copy-in model in
which `data-analysis-agent/databaseSchemaDocs/*.yaml` was authoritative and the
MCP mirrored FROM it — that source directory has since been deleted.)

Two independent checks:

1. **Local tamper/drift check (always runs, no cross-repo access needed):**
   recomputes the sha256 of every `*.yaml` file and compares it against the
   recorded `MANIFEST.sha256` sidecar. A mismatch means the YAML changed but the
   sidecar was not regenerated — either a stray/accidental write, or an
   intentional edit that forgot `--write`. Editing the YAML directly on the MCP is
   now the CORRECT workflow (the MCP owns the catalog); the fix for an intentional
   edit is simply to run `--write` so `MANIFEST.sha256` + `CATALOG_SHA` are
   regenerated in lockstep.

2. **Agent-fixture staleness check (best-effort, skipped if the fixture path is
   not provided):** compares the `catalog_sha` recorded in the agent's committed
   `tests/fixtures/catalog_export.json` against the CONTENT fingerprint this MCP
   currently emits. A mismatch means the agent's derived fixture is stale relative
   to this MCP and must be regenerated from `build_catalog_export()`.

`CATALOG_SHA` is a deterministic CONTENT fingerprint — `sha1(MANIFEST.sha256)`, a
pure function of the per-file sha256 map, with no dependency on git history. The
`catalog_sha` field that `build_catalog_export()` emits is exactly this value, so
the fixture-staleness check compares fingerprint against fingerprint.

Known limitation (documented, not fixed here): `clickhouse-api`'s own CI runner
has no access to the `data-analysis-agent` repo by default, so check #2 is
best-effort local-dev-only unless a CI job explicitly provides the fixture path.
Check #1 always runs in CI and catches the most common failure mode (a YAML edit
that did not regenerate the sidecar). `catalog_sha` is also exposed in the
`getTableSchema` response (D84) and the `/catalog/export` payload, so any
runtime/MCP skew is observable at request time even if CI can't catch it at merge.

Usage:
    python tools/check_catalog_parity.py                       # check only (CI mode)
    python tools/check_catalog_parity.py --write                # regenerate MANIFEST.sha256 + CATALOG_SHA
    python tools/check_catalog_parity.py --fixture /path/to/data-analysis-agent/tests/fixtures/catalog_export.json
    python tools/check_catalog_parity.py --agent-repo /path/to/data-analysis-agent
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

_CATALOG_DIR = Path(__file__).resolve().parent.parent / "app" / "semantic_catalog" / "data"
_MANIFEST_PATH = _CATALOG_DIR / "MANIFEST.sha256"
_CATALOG_SHA_PATH = _CATALOG_DIR / "CATALOG_SHA"

# Location of the agent's derived catalog-export fixture, relative to the
# data-analysis-agent repo root — used to resolve --agent-repo / the env fallback.
_AGENT_FIXTURE_RELPATH = Path("tests") / "fixtures" / "catalog_export.json"


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compute_manifest() -> dict[str, str]:
    """Return {filename: sha256} for every catalog *.yaml file, sorted by name."""
    return {p.name: _sha256_of(p) for p in sorted(_CATALOG_DIR.glob("*.yaml"))}


def _read_manifest() -> dict[str, str]:
    if not _MANIFEST_PATH.exists():
        return {}
    manifest: dict[str, str] = {}
    for line in _MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sha, _, name = line.partition("  ")
        manifest[name.strip()] = sha.strip()
    return manifest


def _render_manifest(manifest: dict[str, str]) -> str:
    """Render a {filename: sha256} map to the exact MANIFEST.sha256 text format.

    Sorted by name, ``"{sha}  {name}"`` per line, newline-joined with a trailing
    newline — the single source of truth for how a manifest is serialized, shared
    by ``_write_manifest`` and the content-fingerprint computation so the
    fingerprint is derived consistently.
    """
    lines = [f"{sha}  {name}" for name, sha in sorted(manifest.items())]
    return "\n".join(lines) + "\n"


def _content_fingerprint(manifest: dict[str, str]) -> str:
    """Return the deterministic CATALOG_SHA content fingerprint for a manifest.

    ``CATALOG_SHA = sha1(MANIFEST.sha256)`` — a pure function of the per-file
    sha256 map, with no dependency on git history or a source-repo checkout. This
    is exactly the ``catalog_sha`` that ``build_catalog_export()`` emits.
    """
    return hashlib.sha1(_render_manifest(manifest).encode("utf-8")).hexdigest()


def _live_fingerprint() -> str:
    """The content fingerprint this MCP currently emits (== build_catalog_export()'s catalog_sha)."""
    return _content_fingerprint(_compute_manifest())


def _write_manifest(manifest: dict[str, str]) -> None:
    _MANIFEST_PATH.write_text(_render_manifest(manifest), encoding="utf-8")


def _write_catalog_sha(fingerprint: str) -> None:
    """Write the CATALOG_SHA sidecar so it stays in lockstep with MANIFEST.sha256."""
    _CATALOG_SHA_PATH.write_text(fingerprint + "\n", encoding="utf-8")


def check_local_drift() -> list[str]:
    """Return a list of error strings; empty means the YAML matches MANIFEST.sha256."""
    errors: list[str] = []
    recorded = _read_manifest()
    current = _compute_manifest()

    if not recorded:
        errors.append(
            f"MANIFEST.sha256 not found at {_MANIFEST_PATH}. "
            "Run with --write to generate it."
        )
        return errors

    missing = set(recorded) - set(current)
    added = set(current) - set(recorded)
    for name in sorted(missing):
        errors.append(f"File recorded in MANIFEST.sha256 but missing on disk: {name}")
    for name in sorted(added):
        errors.append(f"File on disk but not recorded in MANIFEST.sha256: {name}")
    for name in sorted(set(recorded) & set(current)):
        if recorded[name] != current[name]:
            errors.append(
                f"Content hash mismatch for {name}: recorded={recorded[name]} "
                f"actual={current[name]} — the YAML changed but MANIFEST.sha256 was "
                "not regenerated. If the edit was intentional (the MCP owns the "
                "catalog), run --write to regenerate MANIFEST.sha256 + CATALOG_SHA; "
                "otherwise revert the stray change."
            )
    return errors


def check_fixture_staleness(fixture_path: Path) -> list[str]:
    """Compare the agent's derived catalog-export fixture against the live MCP fingerprint.

    ``fixture_path`` points at the data-analysis-agent's committed
    ``tests/fixtures/catalog_export.json`` (derived from this MCP's
    ``build_catalog_export()``). Its ``catalog_sha`` must equal the fingerprint
    this MCP currently emits; a mismatch means the fixture is stale and must be
    regenerated from this MCP.
    """
    errors: list[str] = []
    if not fixture_path.is_file():
        errors.append(f"Agent catalog-export fixture not found: {fixture_path}")
        return errors

    try:
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"Could not read agent catalog-export fixture {fixture_path}: {exc}")
        return errors

    fixture_sha = data.get("catalog_sha") if isinstance(data, dict) else None
    if not fixture_sha:
        errors.append(
            f"Agent catalog-export fixture {fixture_path} has no 'catalog_sha' field."
        )
        return errors

    live_sha = _live_fingerprint()
    if fixture_sha != live_sha:
        errors.append(
            f"Agent catalog-export fixture is stale: fixture catalog_sha={fixture_sha} "
            f"live-mcp-fingerprint={live_sha}. Regenerate the agent's "
            "tests/fixtures/catalog_export.json from this MCP (build_catalog_export())."
        )
    return errors


def _resolve_fixture_path(args: argparse.Namespace) -> Path | None:
    """Resolve the agent-fixture path from --fixture / --agent-repo / env, or None."""
    if args.fixture is not None:
        return args.fixture
    env_fixture = os.environ.get("AGENT_CATALOG_FIXTURE")
    if env_fixture:
        return Path(env_fixture)
    agent_repo = args.agent_repo or os.environ.get("DATA_ANALYSIS_AGENT_REPO")
    if agent_repo:
        return Path(agent_repo) / _AGENT_FIXTURE_RELPATH
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Regenerate MANIFEST.sha256 + CATALOG_SHA from the current catalog YAML files.",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help=(
            "Path to the agent's derived tests/fixtures/catalog_export.json for the "
            "staleness check. Falls back to the AGENT_CATALOG_FIXTURE env var, or "
            "<agent-repo>/tests/fixtures/catalog_export.json when --agent-repo is set."
        ),
    )
    parser.add_argument(
        "--agent-repo",
        type=Path,
        default=None,
        help=(
            "Path to a local data-analysis-agent clone; the fixture is resolved at "
            "<agent-repo>/tests/fixtures/catalog_export.json. Falls back to the "
            "DATA_ANALYSIS_AGENT_REPO env var. Skipped if no fixture path resolves."
        ),
    )
    args = parser.parse_args()

    if args.write:
        manifest = _compute_manifest()
        _write_manifest(manifest)
        fingerprint = _content_fingerprint(manifest)
        _write_catalog_sha(fingerprint)
        print(f"Wrote {_MANIFEST_PATH} with {len(manifest)} entries.")
        print(f"Wrote {_CATALOG_SHA_PATH} = {fingerprint}")
        return 0

    errors = check_local_drift()

    fixture_path = _resolve_fixture_path(args)
    if fixture_path is not None:
        errors.extend(check_fixture_staleness(fixture_path))
    else:
        print(
            "NOTE: agent-fixture staleness check skipped (no --fixture / --agent-repo / "
            "AGENT_CATALOG_FIXTURE / DATA_ANALYSIS_AGENT_REPO). Local tamper/drift "
            "check only. See module docstring for the MCP-as-source-of-truth model."
        )

    if errors:
        print("Semantic Catalog parity check FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("Semantic Catalog parity check OK.")
    recorded_sha = _CATALOG_SHA_PATH.read_text(encoding="utf-8").strip()
    print(f"CATALOG_SHA = {recorded_sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
