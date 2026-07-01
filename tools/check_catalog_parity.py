#!/usr/bin/env python3
"""Parity check for the copy-in'd Semantic Catalog YAML (D79a-style, D83/D84).

`app/semantic_catalog/data/*.yaml` is a **copy-in** of
`data-analysis-agent/databaseSchemaDocs/*.yaml` (see D79(a) for the precedent this
mirrors, and mcp-overlay-design.md OQ-1/OQ-2 for the rationale). The spec repo
(`data-analysis-agent`) is authoritative; this script gives two independent lines
of defence against the copy silently going stale:

1. **Local tamper/drift check (always runs, no cross-repo access needed):**
   recomputes the sha256 of every copied `*.yaml` file and compares it against
   the recorded `MANIFEST.sha256` sidecar. A mismatch means someone edited the
   copied YAML directly in `clickhouse-api` instead of mirroring a change from
   the source repo (or a stray write happened) — this is *always* wrong under
   the copy-in model and should fail CI.

2. **Cross-repo staleness check (best-effort, skipped if the source repo isn't
   checked out alongside this one):** if `--source-dir` (or the
   `DATA_ANALYSIS_AGENT_REPO` env var) points at a local clone of
   `data-analysis-agent`, this script (a) byte-diffs every copied YAML against
   the source repo's `databaseSchemaDocs/<file>` and (b) recomputes the source
   repo's current `git log -1 --format=%H -- databaseSchemaDocs` and compares it
   to the `CATALOG_SHA` sidecar recorded at copy-in time. Either check failing
   means the copy is stale and needs re-mirroring (same-sprint discipline, D79a).

Known limitation (documented, not fixed here): `clickhouse-api`'s own CI runner
has no access to the `data-analysis-agent` repo by default (they are separate
repos with no submodule/checkout relationship configured), so check #2 is
**best-effort local-dev-only** unless a future CI job explicitly checks out both
repos. Check #1 (tamper/drift) always runs in CI and catches the most common
failure mode (someone editing the copy in place). The cross-repo staleness gap
is accepted as a residual risk per mcp-overlay-design.md OQ-1, mitigated by:
  - same-sprint mirroring discipline (a human convention, not automated), and
  - `catalog_sha` being exposed in the `getTableSchema` response (D84), which
    makes any runtime/MCP skew observable at query time even if CI can't catch
    it at merge time.

Usage:
    python tools/check_catalog_parity.py                  # check only (CI mode)
    python tools/check_catalog_parity.py --write           # regenerate MANIFEST.sha256
    python tools/check_catalog_parity.py --source-dir /path/to/data-analysis-agent
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

_CATALOG_DIR = Path(__file__).resolve().parent.parent / "app" / "semantic_catalog" / "data"
_MANIFEST_PATH = _CATALOG_DIR / "MANIFEST.sha256"
_CATALOG_SHA_PATH = _CATALOG_DIR / "CATALOG_SHA"


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compute_manifest() -> dict[str, str]:
    """Return {filename: sha256} for every copied *.yaml file, sorted by name."""
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


def _write_manifest(manifest: dict[str, str]) -> None:
    lines = [f"{sha}  {name}" for name, sha in sorted(manifest.items())]
    _MANIFEST_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_local_drift() -> list[str]:
    """Return a list of error strings; empty means the copy matches MANIFEST.sha256."""
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
                f"actual={current[name]} — copied YAML was edited directly; "
                "re-mirror from data-analysis-agent instead."
            )
    return errors


def check_cross_repo(source_dir: Path) -> list[str]:
    """Return a list of error strings comparing against a local data-analysis-agent clone."""
    errors: list[str] = []
    source_schema_dir = source_dir / "databaseSchemaDocs"
    if not source_schema_dir.is_dir():
        errors.append(f"--source-dir does not contain databaseSchemaDocs/: {source_dir}")
        return errors

    current_files = {p.name for p in _CATALOG_DIR.glob("*.yaml")}
    source_files = {p.name for p in source_schema_dir.glob("*.yaml")}

    for name in sorted(source_files - current_files):
        errors.append(f"New table YAML in source repo not yet copied in: {name}")
    for name in sorted(current_files - source_files):
        errors.append(f"Copied YAML no longer exists in source repo: {name}")

    for name in sorted(current_files & source_files):
        copied_bytes = (_CATALOG_DIR / name).read_bytes()
        source_bytes = (source_schema_dir / name).read_bytes()
        if copied_bytes != source_bytes:
            errors.append(f"Copied YAML differs from source repo content: {name}")

    try:
        result = subprocess.run(
            ["git", "-C", str(source_dir), "log", "-1", "--format=%H", "--", "databaseSchemaDocs"],
            capture_output=True,
            text=True,
            check=True,
        )
        source_sha = result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        errors.append(f"Could not read source repo's git history: {exc}")
        return errors

    recorded_sha = _CATALOG_SHA_PATH.read_text(encoding="utf-8").strip()
    if source_sha and recorded_sha != source_sha:
        errors.append(
            f"CATALOG_SHA sidecar is stale: recorded={recorded_sha} "
            f"source-repo-HEAD={source_sha}. Re-copy databaseSchemaDocs/ and "
            "update CATALOG_SHA."
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Regenerate MANIFEST.sha256 from the current copied YAML files.",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help=(
            "Path to a local data-analysis-agent clone for the cross-repo staleness "
            "check. Falls back to the DATA_ANALYSIS_AGENT_REPO env var. Skipped "
            "entirely if neither is set (see module docstring: known CI limitation)."
        ),
    )
    args = parser.parse_args()

    if args.write:
        manifest = _compute_manifest()
        _write_manifest(manifest)
        print(f"Wrote {_MANIFEST_PATH} with {len(manifest)} entries.")
        return 0

    errors = check_local_drift()

    source_dir = args.source_dir or os.environ.get("DATA_ANALYSIS_AGENT_REPO")
    if source_dir:
        errors.extend(check_cross_repo(Path(source_dir)))
    else:
        print(
            "NOTE: cross-repo staleness check skipped (no --source-dir / "
            "DATA_ANALYSIS_AGENT_REPO). Local tamper/drift check only. "
            "See module docstring for the same-sprint mirroring discipline this "
            "relies on instead."
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
