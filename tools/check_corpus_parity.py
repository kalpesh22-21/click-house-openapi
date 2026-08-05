#!/usr/bin/env python3
"""Integrity + staleness checks for the MCP-owned corpus YAML (Phase 1).

Direction: the **MCP (`clickhouse-api`) is the source of truth** for the blueprint
and knowledge corpus. `app/corpus/data/{blueprints,knowledge}/*.yaml` is
authored/edited HERE, one file per entry, and consumers DERIVE their corpus from
`build_blueprints_export()` / `build_knowledge_export()`.

Blueprints and knowledge are versioned INDEPENDENTLY: each corpus has its own
`MANIFEST.sha256` sidecar and its own content-fingerprint sidecar
(`BLUEPRINTS_SHA` / `KNOWLEDGE_SHA`). This tool regenerates and checks BOTH.

Two things per corpus:

1. **Local tamper/drift check (always runs, no cross-repo access needed):**
   recomputes the sha256 of every `*.yaml` file and compares it against the
   recorded `MANIFEST.sha256` sidecar. A mismatch means the YAML changed but the
   sidecar was not regenerated — either a stray/accidental write, or an
   intentional edit that forgot `--write`. Editing the YAML directly on the MCP is
   the CORRECT workflow (the MCP owns the corpus); the fix for an intentional edit
   is simply to run `--write` so `MANIFEST.sha256` + the SHA sidecar are
   regenerated in lockstep.

2. **Sidecar fingerprint check:** the recorded SHA sidecar must equal
   `sha1(MANIFEST.sha256)` for that corpus — exactly the `blueprints_sha` /
   `knowledge_sha` the export builders emit.

The `*_SHA` sidecars are deterministic CONTENT fingerprints — `sha1(MANIFEST.sha256)`,
a pure function of the per-file sha256 map, with no dependency on git history.

Usage:
    python tools/check_corpus_parity.py           # check only (CI mode)
    python tools/check_corpus_parity.py --write     # regenerate MANIFEST.sha256 + *_SHA
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

_CORPUS_DIR = Path(__file__).resolve().parent.parent / "app" / "corpus" / "data"


@dataclass(frozen=True)
class Corpus:
    """One independently-versioned corpus (its data dir + its two sidecars)."""

    name: str
    data_dir: Path
    manifest_path: Path
    sha_path: Path


CORPORA: tuple[Corpus, ...] = (
    Corpus(
        name="blueprints",
        data_dir=_CORPUS_DIR / "blueprints",
        manifest_path=_CORPUS_DIR / "blueprints" / "MANIFEST.sha256",
        sha_path=_CORPUS_DIR / "blueprints" / "BLUEPRINTS_SHA",
    ),
    Corpus(
        name="knowledge",
        data_dir=_CORPUS_DIR / "knowledge",
        manifest_path=_CORPUS_DIR / "knowledge" / "MANIFEST.sha256",
        sha_path=_CORPUS_DIR / "knowledge" / "KNOWLEDGE_SHA",
    ),
)


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compute_manifest(corpus: Corpus) -> dict[str, str]:
    """Return {filename: sha256} for every *.yaml file in the corpus, sorted by name."""
    return {p.name: _sha256_of(p) for p in sorted(corpus.data_dir.glob("*.yaml"))}


def _read_manifest(corpus: Corpus) -> dict[str, str]:
    if not corpus.manifest_path.exists():
        return {}
    manifest: dict[str, str] = {}
    for line in corpus.manifest_path.read_text(encoding="utf-8").splitlines():
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
    """Return the deterministic content fingerprint for a manifest.

    ``sha1(MANIFEST.sha256)`` — a pure function of the per-file sha256 map, with no
    dependency on git history. This is exactly the ``blueprints_sha`` /
    ``knowledge_sha`` the export builders emit.
    """
    return hashlib.sha1(_render_manifest(manifest).encode("utf-8")).hexdigest()


def _live_fingerprint(corpus: Corpus) -> str:
    """The content fingerprint this MCP currently emits for the corpus."""
    return _content_fingerprint(_compute_manifest(corpus))


def _write_manifest(corpus: Corpus, manifest: dict[str, str]) -> None:
    corpus.manifest_path.write_text(_render_manifest(manifest), encoding="utf-8")


def _write_sha(corpus: Corpus, fingerprint: str) -> None:
    """Write the corpus SHA sidecar so it stays in lockstep with MANIFEST.sha256."""
    corpus.sha_path.write_text(fingerprint + "\n", encoding="utf-8")


def check_local_drift(corpus: Corpus) -> list[str]:
    """Return a list of error strings; empty means the YAML matches MANIFEST.sha256."""
    errors: list[str] = []
    recorded = _read_manifest(corpus)
    current = _compute_manifest(corpus)

    if not recorded:
        errors.append(
            f"[{corpus.name}] MANIFEST.sha256 not found at {corpus.manifest_path}. "
            "Run with --write to generate it."
        )
        return errors

    missing = set(recorded) - set(current)
    added = set(current) - set(recorded)
    for name in sorted(missing):
        errors.append(
            f"[{corpus.name}] File recorded in MANIFEST.sha256 but missing on disk: {name}"
        )
    for name in sorted(added):
        errors.append(
            f"[{corpus.name}] File on disk but not recorded in MANIFEST.sha256: {name}"
        )
    for name in sorted(set(recorded) & set(current)):
        if recorded[name] != current[name]:
            errors.append(
                f"[{corpus.name}] Content hash mismatch for {name}: "
                f"recorded={recorded[name]} actual={current[name]} — the YAML changed "
                "but MANIFEST.sha256 was not regenerated. If the edit was intentional "
                "(the MCP owns the corpus), run --write to regenerate MANIFEST.sha256 "
                "+ the SHA sidecar; otherwise revert the stray change."
            )
    return errors


def check_sidecar_fingerprint(corpus: Corpus) -> list[str]:
    """Return errors if the recorded SHA sidecar != sha1(MANIFEST.sha256)."""
    errors: list[str] = []
    if not corpus.sha_path.exists():
        errors.append(
            f"[{corpus.name}] SHA sidecar not found at {corpus.sha_path}. "
            "Run with --write to generate it."
        )
        return errors
    recorded = corpus.sha_path.read_text(encoding="utf-8").strip()
    live = _live_fingerprint(corpus)
    if recorded != live:
        errors.append(
            f"[{corpus.name}] SHA sidecar out of date: recorded={recorded} "
            f"live-fingerprint={live}. Run --write to regenerate it in lockstep with "
            "MANIFEST.sha256."
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "Regenerate MANIFEST.sha256 + the SHA sidecar for BOTH the blueprints and "
            "knowledge corpus from the current YAML files."
        ),
    )
    args = parser.parse_args()

    if args.write:
        for corpus in CORPORA:
            manifest = _compute_manifest(corpus)
            _write_manifest(corpus, manifest)
            fingerprint = _content_fingerprint(manifest)
            _write_sha(corpus, fingerprint)
            print(
                f"[{corpus.name}] Wrote {corpus.manifest_path} with {len(manifest)} "
                f"entries; {corpus.sha_path.name} = {fingerprint}"
            )
        return 0

    errors: list[str] = []
    for corpus in CORPORA:
        errors.extend(check_local_drift(corpus))
        errors.extend(check_sidecar_fingerprint(corpus))

    if errors:
        print("Corpus parity check FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("Corpus parity check OK.")
    for corpus in CORPORA:
        recorded_sha = corpus.sha_path.read_text(encoding="utf-8").strip()
        print(f"  {corpus.sha_path.name} = {recorded_sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
