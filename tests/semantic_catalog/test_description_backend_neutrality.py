"""Description backend-neutrality lint.

Every authored ``description:`` field in the semantic-catalog YAML —
table-level, per-column, per-rule, and anywhere else the key appears — must
stay DATABASE-AGNOSTIC: it must never name a concrete serving backend
(``clickhouse`` / ``trino``, case-insensitive). Backend-specific text belongs
in the scoped mechanism keys (``trino_notes`` / ``trino_values`` / ...), not
in prose the model reads on every backend.

Why enforced here and not in a schema: the loader is deliberately unknown-key
tolerant (see loader.py), so nothing structurally prevents a backend name from
being authored into a description. A name in a description is not merely
stale prose risk — it leaks deployment topology into a payload that is meant
to read identically on every backend, and (once the scoped-values machinery
exists) invites authors to fork prose by hand instead of using the mechanism.

STRICT GATE (as of 2026-10-07): the 9 pre-existing violators this lint
originally allowlisted as a shrinking ratchet (employee.yaml table-level and
``annual_salary``; expense.yaml table-level, ``system_data_transaction``,
``submitted_kilometers``, ``approved_kilometers``, ``card_country``;
labour_allocation.yaml ``category_code`` / ``category_name``) have all been
normalized to backend-agnostic wording. The catalog is now clean, so the
allowlist/ratchet is retired: ANY backend name in ANY description fails the
suite, unconditionally.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

if __package__ in (None, ""):  # pragma: no cover - direct script run
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.semantic_catalog import loader

_BACKEND_NAME_RE = re.compile(r"clickhouse|trino", re.IGNORECASE)


def _iter_descriptions(node, path=()):
    """Yield (locator_path, text) for every ``description: <str>`` in *node*.

    List items carrying a stable identifier (rule ``id``, ambiguity ``term``,
    join-key ``column``/``column_pattern``, named entries) are keyed by that
    identifier instead of the fragile list index, so a reorder elsewhere in
    the file does not move the locator.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield path, value
            yield from _iter_descriptions(value, path + (str(key),))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            token: str = str(index)
            if isinstance(item, dict):
                for ident in ("id", "term", "column", "column_pattern", "name"):
                    if isinstance(item.get(ident), str) and item[ident]:
                        token = item[ident]
                        break
            yield from _iter_descriptions(item, path + (token,))


def _scan_all_catalog_descriptions() -> dict[str, str]:
    """{locator: text} for every backend-naming description in the real catalog.

    Scans EVERY ``.yaml`` file in the catalog data dir directly (not via the
    loader), so files the loader would skip (e.g. a table-declaration typo)
    are still linted.
    """
    violations: dict[str, str] = {}
    for yaml_path in sorted(loader._DEFAULT_SCHEMA_DIR.glob("*.yaml")):
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        for path, text in _iter_descriptions(raw):
            if _BACKEND_NAME_RE.search(text):
                tail = "::".join(path) if path else "table"
                violations[f"{yaml_path.name}::{tail}"] = text
    return violations


class TestDescriptionBackendNeutrality:
    def test_scanner_flags_synthetic_violation(self):
        """Positive control for the walker: table / column / rule / nested
        levels are all detected, case-insensitively."""
        synthetic = {
            "description": "Uses ClickHouse DDL.",  # table-level
            "columns": {"c": {"description": "viewed via trino"}},
            "rules": [
                {"id": "r1", "description": "TRINO only shape"},
                {"id": "r2", "description": "backend-agnostic"},
            ],
            "measures": {"m": {"description": "fine"}},
        }
        hits = {
            path: text
            for path, text in _iter_descriptions(synthetic)
            if _BACKEND_NAME_RE.search(text)
        }
        assert set(hits) == {(), ("columns", "c"), ("rules", "r1")}

    def test_no_backend_named_descriptions(self):
        """STRICT gate: the catalog has zero backend-named descriptions today
        (all 9 pre-existing violators were normalized 2026-10-07), and this
        test keeps it that way — ANY backend name in ANY description fails,
        with every offending locator and an excerpt listed for the fix."""
        violations = _scan_all_catalog_descriptions()
        assert violations == {}, (
            "Backend-named description(s) — descriptions must stay "
            "database-agnostic; use the scoped mechanism keys "
            "(trino_notes / trino_values / ...) instead: "
            f"{ {k: v[:80] for k, v in violations.items()} }"
        )

    def test_trip_expenses_rule_description_is_normalized(self):
        """Mileage guidance remains backend-neutral and matches its predicate."""
        violations = _scan_all_catalog_descriptions()
        assert "expense.yaml::rules::trip_expenses" not in violations
        expense = loader.load_semantic_catalog()["dbpcm_warehouse.expense"]
        rule = next(r for r in expense["rules"] if r["id"] == "trip_expenses")
        description = rule["description"]
        assert "mileage" in description and "greater than zero" in description
        # The machine predicate is the contract; don't freeze prose or claim
        # that a nullable numeric source can never use zero as an absent value.
        assert rule["predicate"] == "submitted_miles > 0 OR approved_miles > 0"


if __name__ == "__main__":  # pragma: no cover - ad-hoc scan helper
    for locator, text in sorted(_scan_all_catalog_descriptions().items()):
        print(f"{locator}\n    {text[:120]}...")
    raise SystemExit(0)
