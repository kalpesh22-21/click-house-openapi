"""Authoring gate: every blueprint `uses_rules` id must exist in the semantic catalog.

Why this test exists
--------------------
Blueprint `uses_rules` ids are not decorative. Downstream (data-analysis-agent
learning pipeline) the blueprint corpus is shown to an extractor model as PRIOR
ART, and the rule ids the model then cites are validated against the SEMANTIC
CATALOG's rule namespace. A blueprint that cites a private alias — an id that
reads plausibly but exists nowhere in the catalog — therefore teaches the model a
name that is guaranteed to be rejected (`missing_rule: unknown rule '<alias>'`).

That is exactly what happened: `bp-total-earnings-by-department` declared the rule
id `earnings_only` for the concept the catalog names `gross_earnings`
(`dbpcm_warehouse.payroll`, `register_type = 'EARN'`). Nothing on either side of
the corpus/catalog boundary checked that the id resolved, so the drift shipped.
This test is that missing check.

Direction (deliberately one-way)
--------------------------------
Only blueprint -> catalog is asserted. The reverse (a catalog rule that no
blueprint cites) is NOT an error and is deliberately NOT asserted: the catalog is
the full semantic vocabulary of the warehouse and is expected to be much larger
than the handful of concepts the seed blueprints happen to use.

Both authored shapes of `uses_rules` are covered, matching what the consumer's
prior-art mapper accepts:

    uses_rules:
    - gross_earnings                       # bare string

    uses_rules:
    - id: gross_earnings                   # mapping with an `id` key
      resolve_via: resolveValues(register_type, 'earnings')
      table: dbpcm_warehouse.payroll
      binds: earn_codes

Nothing here is hardcoded: the blueprint set is globbed from the real corpus dir
and the rule ids come from the real catalog loader, so adding a blueprint or a
catalog rule needs no edit to this file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.corpus import loader as corpus_loader
from app.semantic_catalog import loader as catalog_loader

# The real blueprint corpus dir, taken from the loader rather than re-spelled here
# so a corpus relocation can't leave this gate silently scanning an empty dir.
_BLUEPRINTS_DIR: Path = corpus_loader._BLUEPRINTS_DIR


@pytest.fixture(autouse=True)
def _reset_caches():
    corpus_loader.invalidate_corpus_cache()
    catalog_loader.invalidate_semantic_catalog_cache()
    yield
    corpus_loader.invalidate_corpus_cache()
    catalog_loader.invalidate_semantic_catalog_cache()


def _blueprint_files() -> list[Path]:
    """Every blueprint YAML on disk, sorted. Sidecars (MANIFEST/*_SHA) are not *.yaml."""
    return sorted(_BLUEPRINTS_DIR.glob("*.yaml"))


def _catalog_rule_ids() -> set[str]:
    """The semantic catalog's whole rule namespace, across every table YAML."""
    catalog = catalog_loader.load_semantic_catalog()
    rule_ids: set[str] = set()
    for entry in catalog.values():
        for rule in entry.get("rules") or []:
            if isinstance(rule, dict) and isinstance(rule.get("id"), str):
                rule_ids.add(rule["id"])
    return rule_ids


def _cited_rule_ids(uses_rules: Any, filename: str) -> list[str]:
    """Normalize one blueprint's `uses_rules` to the list of rule ids it cites.

    Accepts a bare string entry or a mapping carrying an `id`. Any other shape is
    a failure, not a silent skip — an unreadable entry is precisely how an
    unchecked id would slip through this gate.
    """
    if uses_rules is None:
        return []
    assert isinstance(uses_rules, list), (
        f"{filename}: `uses_rules` must be a list, got {type(uses_rules).__name__}"
    )

    ids: list[str] = []
    for position, entry in enumerate(uses_rules):
        if isinstance(entry, str):
            ids.append(entry)
        elif isinstance(entry, dict):
            rule_id = entry.get("id")
            assert isinstance(rule_id, str) and rule_id, (
                f"{filename}: `uses_rules[{position}]` is a mapping with no usable "
                f"string 'id' ({entry!r})"
            )
            ids.append(rule_id)
        else:
            pytest.fail(
                f"{filename}: `uses_rules[{position}]` has an unsupported shape "
                f"{type(entry).__name__} ({entry!r}) — expected a rule id string or a "
                "mapping with an 'id' key."
            )
    return ids


class TestBlueprintRuleIdsExistInCatalog:
    def test_corpus_dir_is_populated(self):
        """Guard: an empty glob would make the parity assertion vacuously pass."""
        assert _blueprint_files(), f"No blueprint YAML found under {_BLUEPRINTS_DIR}"

    def test_scan_covers_the_same_corpus_the_loader_serves(self):
        """The globbed files and the loaded corpus must be the same set of blueprints."""
        globbed_ids = {
            yaml.safe_load(path.read_text(encoding="utf-8"))["id"]
            for path in _blueprint_files()
        }
        assert globbed_ids == set(corpus_loader.load_blueprints())

    def test_catalog_exposes_a_rule_namespace(self):
        """Guard: an empty rule set would fail every blueprint for the wrong reason."""
        assert _catalog_rule_ids(), "Semantic catalog exposes no rule ids at all"

    def test_every_blueprint_rule_id_exists_in_the_catalog(self):
        known_rule_ids = _catalog_rule_ids()

        unknown: list[str] = []
        for path in _blueprint_files():
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            for rule_id in _cited_rule_ids(raw.get("uses_rules"), path.name):
                if rule_id not in known_rule_ids:
                    unknown.append(
                        f"{path.name}: uses_rules id {rule_id!r} is not a semantic "
                        "catalog rule id"
                    )

        assert not unknown, (
            "Blueprint(s) cite rule ids that do not exist in the semantic catalog. "
            "A private alias here is shown to the extractor as prior art and then "
            "declined downstream as an unknown rule — rename the blueprint id to the "
            "catalog's id (or add the rule to the catalog):\n  "
            + "\n  ".join(unknown)
            + f"\nKnown catalog rule ids ({len(known_rule_ids)}): "
            + ", ".join(sorted(known_rule_ids))
        )
