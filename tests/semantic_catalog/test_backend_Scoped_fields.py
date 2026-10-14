"""Backend-scoped values + rule backend membership (the sibling-key machinery).

The convention lives in app/semantic_catalog/loader.py:

  * per-column enumeration fields in ``BACKEND_SCOPED_VALUE_FIELDS`` (today:
    only ``values``) may carry ``trino_<field>`` / ``clickhouse_<field>``
    sibling-key OVERRIDES; the plain key is the ClickHouse default
    (``scoped_field_for_backend``);
  * a ``rules[]`` entry may restrict itself to one serving backend via
    ``backends: [clickhouse|trino]`` — absent means BOTH backends
    (``rule_applies_to_backend`` / ``resolve_rules_for_backend``).

The loader stays unknown-key tolerant (the keys ride the raw YAML dict
untouched); the two serialization surfaces resolve and hide the mechanism:

  * app/semantic_catalog/overlay.py::build_table_schema_response (getTableSchema)
  * app/semantic_catalog/export.py::build_catalog_export_from (/catalog/export)

Contract:
  * the ACTIVE backend's override wins over the plain default; a None-valued
    scoped key is NOT an override (falls back to the plain field);
  * an orphan scoped key (no plain default to override) raises ValueError,
    checked for BOTH spellings regardless of the active backend;
  * unknown backend names / mis-shaped ``backends`` values raise ValueError
    LOUDLY — never silent mis-visibility;
  * mechanism keys (``trino_values`` / ``clickhouse_values`` / ``backends``)
    are NEVER serialized on either surface;
  * a catalog without any scoped content serializes byte-identically on both
    flavors (the no-mechanism-leak invariant).
"""

from __future__ import annotations

import json

import pytest

from app.semantic_catalog import export, loader
from app.semantic_catalog.loader import (
    BACKEND_SCOPED_VALUE_FIELDS,
    BACKEND_SCOPED_VALUE_KEYS,
    KNOWN_BACKENDS,
    resolve_rules_for_backend,
    rule_applies_to_backend,
    scoped_field_for_backend,
)
from app.semantic_catalog.overlay import build_table_schema_response


@pytest.fixture(autouse=True)
def _reset_cache():
    loader.invalidate_semantic_catalog_cache()
    yield
    loader.invalidate_semantic_catalog_cache()


# ---------------------------------------------------------------------------
# loader: scoped_field_for_backend — values-resolution unit tests
# ---------------------------------------------------------------------------


class TestScopedFieldForBackend:
    def test_override_wins_per_flavor(self):
        col = {
            "values": ["Paid", "Approved"],
            "trino_values": ["paid", "approved"],
            "clickhouse_values": ["Paid-CH"],
        }
        assert scoped_field_for_backend(col, "values", trino_enabled=True) == [
            "paid",
            "approved",
        ]
        assert scoped_field_for_backend(col, "values", trino_enabled=False) == ["Paid-CH"]

    def test_plain_default_when_no_scoped_key(self):
        col = {"values": ["A", "B"]}
        for flag in (True, False):
            assert scoped_field_for_backend(col, "values", trino_enabled=flag) == [
                "A",
                "B",
            ]

    def test_clickhouse_values_spelling_applies_only_on_clickhouse_flavor(self):
        """An override authored with the ``clickhouse_values`` spelling is a
        no-op on a Trino deployment (the plain ClickHouse default rides there
        too — it IS the ClickHouse contract) and won only on ClickHouse."""
        col = {"values": ["D"], "clickhouse_values": ["D-CH"]}
        assert scoped_field_for_backend(col, "values", trino_enabled=False) == ["D-CH"]
        assert scoped_field_for_backend(col, "values", trino_enabled=True) == ["D"]

    def test_none_valued_scoped_key_is_not_an_override(self):
        """``trino_values: null`` means ABSENT, not "override to null" — the
        plain default still rides on the Trino flavor."""
        col = {"values": ["A"], "trino_values": None}
        assert scoped_field_for_backend(col, "values", trino_enabled=True) == ["A"]
        assert scoped_field_for_backend(col, "values", trino_enabled=False) == ["A"]

    @pytest.mark.parametrize("scoped_key", ["trino_values", "clickhouse_values"])
    @pytest.mark.parametrize("trino_enabled", [True, False])
    def test_orphan_scoped_key_raises_for_both_spellings_on_both_flavors(
        self, scoped_key, trino_enabled
    ):
        """A scoped override without the plain ``values`` default is an
        authoring error — loud regardless of which spelling is orphaned and
        regardless of which backend is ACTIVE (the error can never hide
        behind a deployment flag)."""
        col = {"type": "String", scoped_key: ["X"]}
        with pytest.raises(ValueError, match="orphan|no plain 'values'"):
            scoped_field_for_backend(col, "values", trino_enabled=trino_enabled)

    def test_missing_field_everywhere_returns_none(self):
        assert scoped_field_for_backend({"type": "String"}, "values", trino_enabled=True) is None

    def test_non_mapping_input_returns_none(self):
        for bad in (None, "not-a-mapping", 42, ["values"]):
            assert scoped_field_for_backend(bad, "values", trino_enabled=True) is None

    def test_mechanism_is_generic_over_the_registered_fields(self):
        """The helper is field-generic; the registry pins it to ``values``."""
        mapping = {"other": "plain", "trino_other": "scoped"}
        assert scoped_field_for_backend(mapping, "other", trino_enabled=True) == "scoped"
        assert scoped_field_for_backend(mapping, "other", trino_enabled=False) == "plain"


# ---------------------------------------------------------------------------
# loader: rule_applies_to_backend — membership unit tests
# ---------------------------------------------------------------------------


class TestRuleAppliesToBackend:
    def test_absent_backends_applies_everywhere(self):
        rule = {"id": "r", "predicate": "x IS NOT NULL"}
        for flag in (True, False):
            assert rule_applies_to_backend(rule, trino_enabled=flag) is True

    def test_both_backends_listed_applies_everywhere(self):
        rule = {"id": "r", "backends": ["clickhouse", "trino"]}
        for flag in (True, False):
            assert rule_applies_to_backend(rule, trino_enabled=flag) is True

    def test_clickhouse_only(self):
        rule = {"id": "r", "backends": ["clickhouse"]}
        assert rule_applies_to_backend(rule, trino_enabled=False) is True
        assert rule_applies_to_backend(rule, trino_enabled=True) is False

    def test_trino_only(self):
        rule = {"id": "r", "backends": ["trino"]}
        assert rule_applies_to_backend(rule, trino_enabled=True) is True
        assert rule_applies_to_backend(rule, trino_enabled=False) is False

    @pytest.mark.parametrize("trino_enabled", [True, False])
    def test_unknown_backend_name_is_loud(self, trino_enabled):
        """An unknown name raises even when the active backend would match."""
        rule = {"id": "bad", "backends": ["trino", "postgre"]}
        with pytest.raises(ValueError, match="postgre"):
            rule_applies_to_backend(rule, trino_enabled=trino_enabled)

    @pytest.mark.parametrize(
        "bad",
        [
            "trino",  # scalar string
            {"backend": "trino"},  # mapping
            [],  # empty list
            42,  # non-list
            [1, 2],  # non-string members
            [None],
        ],
    )
    def test_mis_shaped_backends_is_loud(self, bad):
        rule = {"id": "bad", "backends": bad}
        with pytest.raises(ValueError, match="'backends'"):
            rule_applies_to_backend(rule, trino_enabled=True)

    def test_non_mapping_rule_passes_through(self):
        """Shape handling of non-dict rules is the scope filter's concern."""
        assert rule_applies_to_backend("not-a-rule", trino_enabled=True) is True


class TestResolveRulesForBackend:
    def test_none_passthrough(self):
        assert resolve_rules_for_backend(None, trino_enabled=True) is None

    def test_identity_passthrough_when_nothing_scoped(self):
        """No ``backends`` key anywhere -> the SAME list object, untouched."""
        rules = [{"id": "a", "predicate": "x"}, {"id": "b", "predicate": "y"}]
        for flag in (True, False):
            assert resolve_rules_for_backend(rules, trino_enabled=flag) is rules

    def test_exclusion_and_key_stripping(self):
        rules = [
            {"id": "both", "predicate": "x"},
            {"id": "tr_only", "predicate": "y", "backends": ["trino"]},
            {"id": "ch_only", "predicate": "z", "backends": ["clickhouse"]},
        ]
        tr = resolve_rules_for_backend(rules, trino_enabled=True)
        assert [r["id"] for r in tr] == ["both", "tr_only"]
        assert all("backends" not in r for r in tr)
        ch = resolve_rules_for_backend(rules, trino_enabled=False)
        assert [r["id"] for r in ch] == ["both", "ch_only"]
        assert all("backends" not in r for r in ch)

    def test_unknown_backend_raises_through_the_pipeline(self):
        rules = [{"id": "bad", "predicate": "x", "backends": ["db2"]}]
        with pytest.raises(ValueError, match="db2"):
            resolve_rules_for_backend(rules, trino_enabled=True)


# ---------------------------------------------------------------------------
# Synthetic fixture shared by the two per-surface contract suites
# ---------------------------------------------------------------------------

_SCOPED_ENTRY = {
    "table": "t",
    "database": "dbpcm_warehouse",
    "description": "Synthetic scoped table.",
    "columns": {
        "status": {
            "type": "String",
            "description": "Status.",
            "values": ["PAID", "APPROVED"],  # ClickHouse default
            "trino_values": ["Paid", "Approved"],  # Trino override
            "observed_values": ["DRAFT"],  # plain pass-through, NEVER scoped
        },
        "plain_col": {"type": "UInt64", "description": "No scoped content."},
    },
    "rules": [
        {"id": "both", "predicate": "status IS NOT NULL"},
        {"id": "tr_only", "predicate": "status = 'Paid'", "backends": ["trino"]},
        {"id": "ch_only", "predicate": "status = 'PAID'", "backends": ["clickhouse"]},
    ],
}


def _catalog_with(entry):
    return {f"dbpcm_warehouse.{entry['table']}": entry}


def _schema(entry, *, trino_enabled, scope=None):
    return build_table_schema_response(
        database="dbpcm_warehouse",
        table=entry["table"],
        introspected_columns=[
            {"name": "status", "type": "String", "comment": ""},
            {"name": "plain_col", "type": "UInt64", "comment": ""},
        ],
        catalog=_catalog_with(entry),
        scope=scope,
        trino_enabled=trino_enabled,
    )


def _mechanism_keys(payload) -> list[str]:
    """Every serialized mechanism-key spelling anywhere in the payload text."""
    blob = json.dumps(payload, sort_keys=True)
    return [key for key in (*BACKEND_SCOPED_VALUE_KEYS, "backends") if key in blob]


# ---------------------------------------------------------------------------
# getTableSchema surface (build_table_schema_response), function-level
# ---------------------------------------------------------------------------


class TestGetTableSchemaScopedValues:
    def test_per_column_shape(self):
        for flag in (True, False):
            response = _schema(_SCOPED_ENTRY, trino_enabled=flag)
            status = next(c for c in response["columns"] if c["name"] == "status")
            assert set(status) == {
                "name",
                "type",
                "comment",
                "description",
                "synonyms",
                "unit",
                "values",
                "observed_values",
                "client_defined",
                "sensitive",
            }

    def test_override_substitution_per_flavor(self):
        tr = _schema(_SCOPED_ENTRY, trino_enabled=True)
        ch = _schema(_SCOPED_ENTRY, trino_enabled=False)
        tr_status = next(c for c in tr["columns"] if c["name"] == "status")
        ch_status = next(c for c in ch["columns"] if c["name"] == "status")
        assert tr_status["values"] == ["Paid", "Approved"]
        assert ch_status["values"] == ["PAID", "APPROVED"]
        # observed_values is NOT part of the machinery: pass-through both ways.
        assert tr_status["observed_values"] == ["DRAFT"]
        assert ch_status["observed_values"] == ["DRAFT"]

    def test_rule_exclusion_per_flavor(self):
        tr = _schema(_SCOPED_ENTRY, trino_enabled=True)
        ch = _schema(_SCOPED_ENTRY, trino_enabled=False)
        assert [r["id"] for r in tr["rules"]] == ["both", "tr_only"]
        assert [r["id"] for r in ch["rules"]] == ["both", "ch_only"]

    def test_rule_exclusion_also_applies_under_scope_enforcement(self):
        """Backend membership is resolved BEFORE and INDEPENDENT of
        _filter_rules: a non-matching rule must not reappear when scope
        enforcement runs."""
        scope = frozenset(
            {"dbpcm_warehouse.t.status", "dbpcm_warehouse.t.plain_col"}
        )
        tr = _schema(_SCOPED_ENTRY, trino_enabled=True, scope=scope)
        ch = _schema(_SCOPED_ENTRY, trino_enabled=False, scope=scope)
        assert [r["id"] for r in tr["rules"]] == ["both", "tr_only"]
        assert [r["id"] for r in ch["rules"]] == ["both", "ch_only"]

    def test_mechanism_keys_are_never_serialized(self):
        for flag in (True, False):
            response = _schema(_SCOPED_ENTRY, trino_enabled=flag)
            assert _mechanism_keys(response) == []
            # The emitted rule dicts carry no `backends` remnant.
            assert all("backends" not in r for r in response["rules"])

    def test_unscoped_table_is_byte_identical_across_flavors(self):
        plain = {
            "table": "t",
            "database": "dbpcm_warehouse",
            "columns": {"status": {"type": "String", "values": ["A", "B"]}},
            "rules": [{"id": "r", "predicate": "status IS NOT NULL"}],
        }
        tr = _schema(plain, trino_enabled=True)
        ch = _schema(plain, trino_enabled=False)
        assert json.dumps(tr, sort_keys=True) == json.dumps(ch, sort_keys=True)
        status = next(c for c in tr["columns"] if c["name"] == "status")
        assert status["values"] == ["A", "B"]
        assert [r["id"] for r in tr["rules"]] == ["r"]

    def test_orphan_override_is_loud_at_the_surface(self):
        bad = {
            "table": "t",
            "database": "dbpcm_warehouse",
            "columns": {"status": {"type": "String", "trino_values": ["X"]}},
        }
        with pytest.raises(ValueError, match="no plain 'values'"):
            _schema(bad, trino_enabled=True)


# ---------------------------------------------------------------------------
# /catalog/export surface (build_catalog_export_from), per flavor
# ---------------------------------------------------------------------------


class TestExportScopedValues:
    def test_override_substitution_per_flavor(self):
        tr = export.build_catalog_export_from(
            _catalog_with(_SCOPED_ENTRY), "sha", trino_enabled=True
        )["catalog"]["dbpcm_warehouse.t"]
        ch = export.build_catalog_export_from(
            _catalog_with(_SCOPED_ENTRY), "sha", trino_enabled=False
        )["catalog"]["dbpcm_warehouse.t"]
        assert tr["columns"]["status"]["values"] == ["Paid", "Approved"]
        assert ch["columns"]["status"]["values"] == ["PAID", "APPROVED"]
        # observed_values pass-through on both flavors.
        assert tr["columns"]["status"]["observed_values"] == ["DRAFT"]
        assert ch["columns"]["status"]["observed_values"] == ["DRAFT"]

    def test_rule_exclusion_per_flavor(self):
        tr = export.build_catalog_export_from(
            _catalog_with(_SCOPED_ENTRY), "sha", trino_enabled=True
        )["catalog"]["dbpcm_warehouse.t"]
        ch = export.build_catalog_export_from(
            _catalog_with(_SCOPED_ENTRY), "sha", trino_enabled=False
        )["catalog"]["dbpcm_warehouse.t"]
        assert [r["id"] for r in tr["rules"]] == ["both", "tr_only"]
        assert [r["id"] for r in ch["rules"]] == ["both", "ch_only"]

    def test_mechanism_keys_are_never_serialized(self):
        for flag in (True, False):
            result = export.build_catalog_export_from(
                _catalog_with(_SCOPED_ENTRY), "sha", trino_enabled=flag
            )
            entry = result["catalog"]["dbpcm_warehouse.t"]
            assert _mechanism_keys(entry) == []
            assert all("backends" not in r for r in entry["rules"])

    def test_unscoped_table_is_byte_identical(self):
        """The byte-identical-output invariant: no scoped content -> the
        emitted entry equals the authored entry on BOTH flavors."""
        plain = {
            "table": "t",
            "database": "dbpcm_warehouse",
            "columns": {
                "status": {"type": "String", "values": ["A", "B"]},
                "n": {"type": "UInt64"},
            },
            "rules": [{"id": "r", "predicate": "status IS NOT NULL"}],
        }
        for flag in (True, False):
            entry = export.build_catalog_export_from(
                _catalog_with(plain), "sha", trino_enabled=flag
            )["catalog"]["dbpcm_warehouse.t"]
            assert json.dumps(entry, sort_keys=True) == json.dumps(plain, sort_keys=True)

    def test_none_valued_scoped_key_falls_back_and_is_stripped(self):
        entry = {
            "table": "t",
            "database": "dbpcm_warehouse",
            "columns": {
                "status": {
                    "type": "String",
                    "values": ["A"],
                    "trino_values": None,
                    "clickhouse_values": None,
                }
            },
        }
        for flag in (True, False):
            out = export.build_catalog_export_from(
                _catalog_with(entry), "sha", trino_enabled=flag
            )["catalog"]["dbpcm_warehouse.t"]
            assert out["columns"]["status"] == {"type": "String", "values": ["A"]}

    def test_orphan_override_is_loud_at_the_surface(self):
        bad = {
            "table": "t",
            "database": "dbpcm_warehouse",
            "columns": {"status": {"type": "String", "clickhouse_values": ["X"]}},
        }
        with pytest.raises(ValueError, match="no plain 'values'"):
            export.build_catalog_export_from(
                _catalog_with(bad), "sha", trino_enabled=False
            )

    def test_unknown_rule_backend_is_loud_at_the_surface(self):
        bad = {
            "table": "t",
            "database": "dbpcm_warehouse",
            "columns": {"a": {"type": "String"}},
            "rules": [{"id": "r", "predicate": "x", "backends": ["db2"]}],
        }
        with pytest.raises(ValueError, match="db2"):
            export.build_catalog_export_from(
                _catalog_with(bad), "sha", trino_enabled=True
            )


# ---------------------------------------------------------------------------
# Real-catalog canary: machinery is WIRED, and nothing uses it yet (deliberate)
# ---------------------------------------------------------------------------


class TestRealCatalogCanary:
    """Pins CURRENT truth: the real catalog contains no scoped values / scoped
    rules today (the machinery ships unused-but-tested; adoption is a future,
    deliberate authoring act). If a future task introduces the first real
    ``trino_values`` / ``backends:`` user, these pins must be updated in the
    same commit as the yaml change — never relaxed silently."""

    def test_registries_wired(self):
        assert BACKEND_SCOPED_VALUE_FIELDS == ("values",)
        assert KNOWN_BACKENDS == ("clickhouse", "trino")
        assert set(BACKEND_SCOPED_VALUE_KEYS) == {"trino_values", "clickhouse_values"}

    def test_no_current_scoped_values_users(self):
        catalog = loader.load_semantic_catalog()
        offenders = [
            f"{qualified_key}.{column_name}.{key}"
            for qualified_key, entry in catalog.items()
            for column_name, column_def in (entry.get("columns") or {}).items()
            if isinstance(column_def, dict)
            for key in BACKEND_SCOPED_VALUE_KEYS
            if key in column_def
        ]
        assert offenders == [], (
            "Backend-scoped values acquired a real catalog user — update this "
            f"canary deliberately: {offenders}"
        )

    def test_no_current_scoped_rule_users(self):
        catalog = loader.load_semantic_catalog()
        offenders = [
            f"{qualified_key}.rules[{rule.get('id')}]"
            for qualified_key, entry in catalog.items()
            for rule in (entry.get("rules") or [])
            if isinstance(rule, dict) and "backends" in rule
        ]
        assert offenders == [], (
            "Rule backend membership acquired a real catalog user — update "
            f"this canary deliberately: {offenders}"
        )

    def test_expense_flavors_are_byte_identical_and_rules_all_ride(self):
        """expense.yaml (the table that once diverged) now resolves identically
        on both flavors: the status label ``values`` ride everywhere and every
        rule is backend-shared — the whole point of the P2 revert + machinery
        restoration split. (The pre-existing scoped-NOTES mechanism gates
        ``trino_notes`` per flavor by design, so the byte-identity comparison
        excludes the ``notes`` key and pins everything ELSE.)"""
        catalog = loader.load_semantic_catalog()
        # Exercise note gating independently of mutable production prose.
        from copy import deepcopy
        catalog = deepcopy(catalog)
        catalog["dbpcm_warehouse.expense"]["trino_notes"] = ["Synthetic backend note."]
        cols = [{"name": "expense_status", "type": "Nullable(FixedString(50))",
                 "comment": ""}]
        responses = {}
        for flag in (True, False):
            responses[flag] = build_table_schema_response(
                database="dbpcm_warehouse",
                table="expense",
                introspected_columns=cols,
                catalog=catalog,
                scope=None,
                trino_enabled=flag,
            )
        # Scoped-notes gate sanity: notes ride only on the Trino flavor
        # (pre-existing mechanism, orthogonal to scoped values/rules).
        assert "notes" in responses[True] and "notes" not in responses[False]
        sans_notes = {
            flag: {k: v for k, v in response.items() if k != "notes"}
            for flag, response in responses.items()
        }
        assert json.dumps(sans_notes[True], sort_keys=True) == json.dumps(
            sans_notes[False], sort_keys=True
        )
        for flag in (True, False):
            status = responses[flag]["columns"][0]
            assert status["values"] == catalog["dbpcm_warehouse.expense"]["columns"]["expense_status"]["values"]
            assert {r["id"] for r in responses[flag]["rules"]} == {
                r["id"] for r in catalog["dbpcm_warehouse.expense"]["rules"]
            }
            assert _mechanism_keys(responses[flag]) == []
