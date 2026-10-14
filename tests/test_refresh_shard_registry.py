"""Replica selection, payroll exclusion, deterministic catalog naming, filters.

Covers the pure functions of scripts/refresh_shard_registry.py (no network):
the paycompy read-preference ranking (GR-Node3 -> GR-Node2 -> GR-Node1 -> DC1
-> other, host/port tiebreak), the GR-Node1-is-never-PROD rule, explicit
payroll (hhckey shard) exclusion, client-range-ordered catalog naming, and the
--env-type/--client snapshot-narrowing filters (applied BEFORE replica dedupe
and catalog naming, recorded in the snapshot "filters" metadata).

Also covers refresh-time hygiene failures (identifier-unsafe or null/blank
env_type values, invalid client-filter codes), the atomic snapshot publish, and
the missing-inventory guardrail of scripts/setup_trino_catalogs.py -- all
hermetic (no network).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.shard_routing import validate_identifier
from app.shard_routing import ShardRegistryError, load_shard_registry, validate_shard_registry
from scripts import refresh_shard_registry as refresh
from scripts import setup_trino_catalogs
from scripts.refresh_shard_registry import (
    _is_payroll_row,
    _normalise_client_codes,
    _normalise_row,
    _pick_read_node,
    assign_catalog_names,
    build_snapshot,
    select_shard_entries,
)


def row(
    env="PROD",
    db="dbonline",
    start="00000",
    end="00999",
    host="h1",
    used_for="GR-Node3",
    port="3306",
    name="online",
):
    """A raw instanceLocation.php row (ports/ranges are strings on the wire)."""
    return {
        "env_type": env,
        "mysqldb": db,
        "name": name,
        "mysqlhost": host,
        "mysqlip": "10.0.0.1",
        "mysqlport": port,
        "clientstart": start,
        "clientend": end,
        "used_for": used_for,
        "site": "okc",
        "level": "00",
    }


def node(**kwargs):
    normalised = _normalise_row(row(**kwargs))
    assert normalised is not None
    return normalised


def test_normalise_row_coerces_and_falls_back():
    entry = _normalise_row(
        {
            "env_type": "PROD",
            "mysqldb": "dbonline",
            "name": "online",
            "mysqlhost": "  ",  # blank -> mysqlip fallback
            "mysqlip": "10.1.2.3",
            "mysqlport": "3306",  # string coerced to int
            "clientstart": "  ",  # blank -> None (never matched at runtime)
            "clientend": "  ",
            "used_for": " GR-Node2 ",  # stripped
        }
    )
    assert entry["host"] == "10.1.2.3"
    assert entry["port"] == 3306
    assert entry["clientstart"] is None and entry["clientend"] is None
    assert entry["used_for"] == "GR-Node2"


@pytest.mark.parametrize(
    "row_data",
    [
        {"mysqlhost": "", "mysqlip": ""},  # no resolvable host
        {"mysqlport": "not-a-port"},
        {"mysqlport": "0"},
        {"mysqlport": "70000"},
    ],
)
def test_normalise_row_rejects_unusable_rows(row_data):
    raw = row()
    raw.update(row_data)
    assert _normalise_row(raw) is None


def test_read_preference_ranking_order():
    nodes = [
        node(used_for="DC1", host="h-dc1"),
        node(used_for="GR-Node2", host="h-n2"),
        node(used_for="Standalone", host="h-other"),
        node(used_for="GR-Node3", host="h-n3"),
        node(used_for="GR-Node1", host="h-n1"),
    ]
    # QA2 keeps GR-Node1 eligible; full ranking applies.
    for n in nodes:
        n["env_type"] = "QA2"
    assert _pick_read_node(nodes)["host"] == "h-n3"
    assert _node_order_without(nodes, "GR-Node3") == "h-n2"
    assert _node_order_without(nodes, "GR-Node3", "GR-Node2") == "h-n1"
    assert _node_order_without(nodes, "GR-Node3", "GR-Node2", "GR-Node1") == "h-dc1"
    assert _node_order_without(nodes, "GR-Node3", "GR-Node2", "GR-Node1", "DC1") == "h-other"


def _node_order_without(nodes, *excluded):
    remaining = [n for n in nodes if n["used_for"] not in excluded]
    return _pick_read_node(remaining)["host"]


def test_gr_node1_is_never_selected_for_prod():
    n1 = node(used_for="GR-Node1", host="h-n1")
    n2 = node(used_for="GR-Node2", host="h-n2")
    dc1 = node(used_for="DC1", host="h-dc1")
    assert _pick_read_node([n1, n2])["host"] == "h-n2"
    assert _pick_read_node([n1, dc1])["host"] == "h-dc1"
    # A PROD shard with ONLY GR-Node1 replica(s) is unschedulable.
    assert _pick_read_node([n1, node(used_for="GR-Node1", host="h-n1b")]) is None


def test_non_prod_selects_gr_node1_only_if_nothing_better():
    qa_node1 = node(env="QA2", used_for="GR-Node1", host="h-n1")
    qa_node3 = node(env="QA2", used_for="GR-Node3", host="h-n3")
    qa_dc1 = node(env="QA2", used_for="DC1", host="h-dc1")
    assert _pick_read_node([qa_node1, qa_node3])["host"] == "h-n3"
    assert _pick_read_node([qa_node1, qa_dc1])["host"] == "h-n1"
    assert _pick_read_node([qa_node1])["host"] == "h-n1"


def test_tiebreak_is_host_then_port():
    later_port = node(used_for="GR-Node2", host="a-host", port="3307")
    earlier = node(used_for="GR-Node2", host="a-host", port="3306")
    later_host = node(used_for="GR-Node2", host="b-host", port="3306")
    assert _pick_read_node([later_port, later_host, earlier])["port"] == 3306
    assert _pick_read_node([later_host, earlier])["host"] == "a-host"


def test_replica_rows_collapse_to_one_read_node():
    rows = [
        row(used_for="GR-Node2", host="h-n2"),
        row(used_for="GR-Node3", host="h-n3"),
        row(used_for="GR-Node1", host="h-n1"),
    ]
    snapshot, report = build_snapshot(rows, mysqldbs=["dbonline"], name="online", source="test")
    assert report["shard_groups"] == 1
    assert len(snapshot["mysql_shards"]) == 1
    assert snapshot["mysql_shards"][0]["host"] == "h-n3"


def test_prod_shard_with_only_gr_node1_is_dropped_and_reported():
    rows = [row(used_for="GR-Node1", host="h-n1"), row(used_for="GR-Node3", host="ok")]
    # First row is in its own range so both ranges exist; the GR-Node1-only one drops.
    rows[0]["clientstart"], rows[0]["clientend"] = "11000", "11999"
    snapshot, report = build_snapshot(rows, mysqldbs=["dbonline"], name="online", source="test")
    assert [e["clientstart"] for e in snapshot["mysql_shards"]] == ["00000"]
    assert report["prod_gr_node1_dropped"] == ["PROD/dbonline 11000-11999"]


def test_payroll_rows_are_excluded_explicitly_even_when_requested():
    rows = [
        row(db="payroll", name="bigdata_shard"),  # the hhckey-sharded payroll store
        row(db="payroll_master", name="bigdata_master"),
        row(db="dbonline", name="bigdata_shard_stg"),  # bigdata family by name
        row(db="dbonline", name="online"),
    ]
    snapshot, report = build_snapshot(
        rows, mysqldbs=["dbonline", "payroll", "payroll_master"], name="online", source="test"
    )
    assert [e["database"] for e in snapshot["mysql_shards"]] == ["dbonline"]
    # payroll mysqldbs (payroll/payroll_master) AND the bigdata name family all count.
    assert report["payroll_excluded"] == 3


def test_is_payroll_row_matches_store_and_family():
    assert _is_payroll_row(node(db="payroll", name="online"))
    assert _is_payroll_row(node(db="dbonline", name="bigdata_demo"))
    assert not _is_payroll_row(node(db="dbonline", name="online"))


def test_catalog_naming_is_deterministic_and_range_ordered():
    rows = [
        row(env="PROD", start="11000", end="1ZZZZ", host="b"),
        row(env="QA2", start="00000", end="ZZZZZ", host="q"),
        row(env="PROD", start="00000", end="10ZZZ", host="a"),
    ]
    selected, _ = select_shard_entries(rows, ["dbonline"], "online")
    named = assign_catalog_names(selected)
    assert [(e["env_type"], e["clientstart"]) for e in named] == [
        ("PROD", "00000"), ("PROD", "11000"), ("QA2", "00000")
    ]
    assert len({e["catalog"] for e in named}) == 3
    # Input order must not matter.
    reselected, _ = select_shard_entries(list(reversed(rows)), ["dbonline"], "online")
    assert assign_catalog_names(reselected) == named
    for entry in named:
        assert validate_identifier(entry["catalog"]) == entry["catalog"]


def test_catalog_identity_includes_database():
    # Equal client ranges in distinct databases must produce distinct catalogs.
    rows = [row(db="dbpcm", start="00000", end="00999"), row(db="dbonline", start="00000", end="00999")]
    snapshot, _ = build_snapshot(rows, mysqldbs=["dbonline", "dbpcm"], name="online", source="test")
    assert len({e["catalog"] for e in snapshot["mysql_shards"]}) == 2


def test_snapshot_shape():
    snapshot, report = build_snapshot([row()], mysqldbs=["dbonline"], name="online", source="test-source")
    assert snapshot["source"] == "test-source"
    assert "generated_at" in snapshot
    assert snapshot["clickhouse"] == {"catalog": "clickhouse", "host": "host.docker.internal", "port": 8123}
    entry = snapshot["mysql_shards"][0]
    assert set(entry) == {"catalog", "host", "port", "database", "env_type", "clientstart", "clientend"}
    assert report["input_rows"] == 1 and report["shard_groups"] == 1


# --- --env-type / --client snapshot-narrowing filters -----------------------


def test_env_type_filter_narrows_snapshot_to_that_env():
    rows = [
        row(env="PROD", start="00000", end="ZZZZZ", host="p1"),
        row(env="QA2", start="00000", end="10ZZZ", host="q1"),
        row(env="QA2", start="11000", end="1ZZZZ", host="q2"),
    ]
    # Matching is case-insensitive; stored env_type is canonical uppercase.
    snapshot, report = build_snapshot(
        rows, mysqldbs=["dbonline"], name="online", source="test", env_types=["qa2"]
    )
    assert [(e["env_type"], e["clientstart"]) for e in snapshot["mysql_shards"]] == [
        ("QA2", "00000"), ("QA2", "11000")
    ]
    assert report["env_type_excluded_rows"] == 1
    # Repetition and blank values dedupe away; omitting the flag keeps ALL envs.
    snapshot_all, _ = build_snapshot(
        rows, mysqldbs=["dbonline"], name="online", source="test", env_types=[" qa2 ", "QA2", ""]
    )
    assert snapshot_all["mysql_shards"] == snapshot["mysql_shards"]
    full, _ = build_snapshot(rows, mysqldbs=["dbonline"], name="online", source="test")
    assert {e["env_type"] for e in full["mysql_shards"]} == {"PROD", "QA2"}


def test_client_filter_selects_exactly_the_containing_shard():
    rows = [
        row(start="00000", end="10R36", host="h-a"),
        row(start="10R37", end="10R99", host="h-b"),
        row(start="10S00", end="1ZZZZ", host="h-c"),
    ]
    # 10R37 is the clientstart BOUNDARY of shard b (inclusive both ends).
    snapshot, report = build_snapshot(
        rows, mysqldbs=["dbonline"], name="online", source="test", clients=["10R37"]
    )
    assert [e["host"] for e in snapshot["mysql_shards"]] == ["h-b"]
    # clientend boundary is inclusive too.
    snapshot_end, _ = build_snapshot(
        rows, mysqldbs=["dbonline"], name="online", source="test", clients=["10R99"]
    )
    assert [e["host"] for e in snapshot_end["mysql_shards"]] == ["h-b"]
    assert snapshot_end["mysql_shards"] == snapshot["mysql_shards"]
    assert report["client_excluded_groups"] == 2
    # One code just outside every range selects nothing (main() would refuse).
    empty, empty_report = build_snapshot(
        rows, mysqldbs=["dbonline"], name="online", source="test", clients=["10S0"])
    assert empty["mysql_shards"] == [] and empty_report["shard_groups"] == 0


def test_client_filter_is_repeatable_with_union_semantics():
    rows = [
        row(start="00000", end="00999", host="h-a"),
        row(start="11000", end="11999", host="h-b"),
        row(start="22000", end="22999", host="h-c"),
    ]
    snapshot, _ = build_snapshot(
        rows, mysqldbs=["dbonline"], name="online", source="test", clients=["00500", "22500"]
    )
    assert [e["host"] for e in snapshot["mysql_shards"]] == ["h-a", "h-c"]


def test_combined_qa2_and_10r37_yields_single_deterministically_named_entry():
    rows = [
        row(env="PROD", start="10R37", end="10R99", host="p-range-owner"),  # same range, wrong env
        row(env="QA2", start="00000", end="10R36", host="q-before"),  # QA2 but outside
        row(env="QA2", start="10R37", end="10R99", host="q-n3", used_for="GR-Node3"),
        row(env="QA2", start="10R37", end="10R99", host="q-dc1", used_for="DC1"),  # replica, deduped
        row(env="QA2", start="10S00", end="ZZZZZ", host="q-after"),  # QA2 but outside
    ]
    snapshot, report = build_snapshot(
        rows, mysqldbs=["dbonline"], name="online", source="test", env_types=["QA2"], clients=["10R37"]
    )
    assert len(snapshot["mysql_shards"]) == 1
    entry = snapshot["mysql_shards"][0]
    assert entry["catalog"].startswith("mysql_shard_qa2_")
    assert entry["env_type"] == "QA2"
    assert entry["host"] == "q-n3"  # read-preferred replica of the ONE surviving group
    assert (entry["clientstart"], entry["clientend"]) == ("10R37", "10R99")
    assert report["shard_groups"] == 1


def test_filters_are_recorded_in_snapshot_metadata():
    snapshot, _ = build_snapshot(
        [row(env="QA2", start="00000", end="ZZZZZ")],
        mysqldbs=["dbonline"],
        name="online",
        source="test",
        env_types=["qa2"],
        clients=["10R37"],
    )
    assert snapshot["filters"] == {
        "mysqldb": ["dbonline"],
        "name": "online",
        "env_type": ["qa2"],  # operator casing kept; matching is case-insensitive
        "client": ["10R37"],
    }
    # Default run: no narrowing -- mysqldb/name still recorded, filters null.
    full, _ = build_snapshot([row()], mysqldbs=["dbonline", "dbpcm"], name="online", source="test")
    assert full["filters"] == {
        "mysqldb": ["dbonline", "dbpcm"],
        "name": "online",
        "env_type": None,
        "client": None,
    }


@pytest.mark.parametrize("bad", ["", "   ", "10R3\x00", "1\n2", 103, None])
def test_invalid_client_codes_raise_value_error(bad):
    with pytest.raises(ValueError, match="invalid --client code"):
        _normalise_client_codes([bad])


def test_main_invalid_client_code_is_a_clean_error_exit_1(monkeypatch, tmp_path, capsys):
    export = _write_export(tmp_path, [row(env="QA2")])
    output = tmp_path / "shards.json"
    monkeypatch.setattr(
        sys, "argv", _refresh_argv(export, output) + ["--env-type", "QA2", "--client", "  "]
    )
    assert refresh.main() == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "--client" in err  # the offending code is named, not swallowed
    assert "Traceback" not in err
    assert not output.exists()


def test_main_unknown_client_under_filter_refuses_and_names_active_filters(monkeypatch, tmp_path, capsys):
    export = _write_export(tmp_path, [row(env="QA2", start="00000", end="00999")])
    output = tmp_path / "shards.json"
    monkeypatch.setattr(
        sys, "argv", _refresh_argv(export, output) + ["--env-type", "QA2", "--client", "9Z9Z9"]
    )
    assert refresh.main() == 1
    err = capsys.readouterr().err
    assert "no shard entries survived" in err
    assert "QA2" in err and "9Z9Z9" in err  # the active filters are named
    assert "--env-type" in err and "--client" in err
    assert "Traceback" not in err
    assert not output.exists()


def test_main_combined_filters_write_single_catalog_snapshot(monkeypatch, tmp_path, capsys):
    export = _write_export(
        tmp_path,
        [
            row(env="PROD", start="10R37", end="10R99", host="p"),
            row(env="QA2", start="10R37", end="10R99", host="q"),
            row(env="QA2", start="10S00", end="ZZZZZ", host="q2"),
        ],
    )
    output = tmp_path / "shards.json"
    monkeypatch.setattr(
        sys, "argv", _refresh_argv(export, output) + ["--env-type", "QA2", "--client", "10R37"]
    )
    assert refresh.main() == 0
    out = capsys.readouterr().out
    assert "env_type filter ['QA2']" in out
    assert "client filter ['10R37']" in out
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    assert len(snapshot["mysql_shards"]) == 1
    assert snapshot["mysql_shards"][0]["catalog"].startswith("mysql_shard_qa2_")
    assert snapshot["filters"]["env_type"] == ["QA2"]
    assert snapshot["filters"]["client"] == ["10R37"]


def _write_export(tmp_path: Path, rows: list) -> Path:
    """A locally saved instanceLocation export for refresh main() runs via --input."""
    export = tmp_path / "instanceLocation.json"
    export.write_text(json.dumps({"data": rows}), encoding="utf-8")
    return export


def _refresh_argv(export: Path, output: Path) -> list[str]:
    return ["refresh_shard_registry.py", "--input", str(export), "--output", str(output)]


def test_assign_catalog_names_fails_loud_on_identifier_unsafe_env_type():
    # "PROD-2" lowercases to "prod-2": the '-' is not identifier-safe (Trino
    # catalog name / file name), so the refresh must refuse it instead of
    # letting every routed tenant 400 at query time.
    with pytest.raises(ValueError, match="PROD-2"):
        build_snapshot([row(env="PROD-2")], mysqldbs=["dbonline"], name="online", source="test")


def test_main_renders_identifier_unsafe_env_type_as_clean_error(monkeypatch, tmp_path, capsys):
    export = _write_export(tmp_path, [row(env="PROD-2")])
    output = tmp_path / "shards.json"
    monkeypatch.setattr(sys, "argv", _refresh_argv(export, output))
    assert refresh.main() == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "PROD-2" in err  # the offending env_type is named
    assert "Traceback" not in err
    assert not output.exists()


@pytest.mark.parametrize("env", ["", "   ", None])
def test_normalise_row_rejects_missing_env_type(env):
    # Null/blank env_type is unusable, mirroring the host/port rule: grouping
    # and catalog naming key off it, so it can never back a catalog.
    assert _normalise_row(row(env=env)) is None


def test_empty_env_type_rows_are_counted_unusable_and_never_selected():
    selected, report = select_shard_entries([row(env=""), row(env=None)], ["dbonline"], "online")
    assert selected == []
    assert report["unusable_rows"] == 2

    selected, report = select_shard_entries([row(env=""), row()], ["dbonline"], "online")
    assert report["unusable_rows"] == 1
    assert [e["env_type"] for e in selected] == ["PROD"]


def test_main_counts_empty_env_rows_as_unusable_without_traceback(monkeypatch, tmp_path, capsys):
    export = _write_export(tmp_path, [row(env=""), row()])
    output = tmp_path / "shards.json"
    monkeypatch.setattr(sys, "argv", _refresh_argv(export, output))
    assert refresh.main() == 0
    assert "skipped unusable rows: 1" in capsys.readouterr().out
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    assert [e["env_type"] for e in snapshot["mysql_shards"]] == ["PROD"]


def test_main_all_rows_unusable_is_a_clean_refusal(monkeypatch, tmp_path, capsys):
    export = _write_export(tmp_path, [row(env="")])
    output = tmp_path / "shards.json"
    monkeypatch.setattr(sys, "argv", _refresh_argv(export, output))
    assert refresh.main() == 1
    err = capsys.readouterr().err
    assert "no shard entries survived" in err
    assert "Traceback" not in err and "AttributeError" not in err
    assert not output.exists()


def test_failed_write_leaves_previous_snapshot_byte_identical(monkeypatch, tmp_path):
    export = _write_export(tmp_path, [row()])
    output = tmp_path / "shards.json"
    previous = b'{"previous": "still-good snapshot"}\n'
    output.write_bytes(previous)

    def explode(self, *args, **kwargs):
        raise OSError("simulated mid-write failure")

    monkeypatch.setattr(Path, "write_text", explode)
    monkeypatch.setattr(sys, "argv", _refresh_argv(export, output))
    assert refresh.main() == 1
    assert output.read_bytes() == previous
    assert not (tmp_path / "shards.json.tmp").exists()  # temp file cleaned up


def test_setup_catalogs_missing_default_inventory_points_at_refresh(monkeypatch, tmp_path, capsys):
    # Hermetic: a ROOT with no docker/trino/shards.json under it. The default
    # inventory must point at the refreshed snapshot, and its absence must
    # exit 1 naming scripts/refresh_shard_registry.py as the way to make one.
    monkeypatch.setattr(setup_trino_catalogs, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["setup_trino_catalogs.py"])
    with pytest.raises(SystemExit) as excinfo:
        setup_trino_catalogs.main()
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "not found" in err
    assert "scripts/refresh_shard_registry.py" in err


def test_names_survive_filtering_insertion_and_replica_failover():
    original = [row(start="10000", end="19999", host="a"), row(start="30000", end="39999", host="b")]

    def names(rows, **filters):
        snapshot, _ = build_snapshot(rows, mysqldbs=["dbonline"], name="online", source="test", **filters)
        return {e["clientstart"]: e["catalog"] for e in snapshot["mysql_shards"]}

    full = names(original)
    assert names(original, clients=["35000"]) == {"30000": full["30000"]}
    expanded = names([row(start="00000", end="09999"), *original])
    assert all(expanded[start] == catalog for start, catalog in full.items())
    failover = [{**r, "mysqlhost": "replacement", "mysqlport": "3307"} for r in original]
    assert names(failover) == full


def test_mixed_case_environments_share_replica_group_and_prod_policy():
    rows = [row(env="prod", used_for="GR-Node1"), row(env="PrOd", used_for="DC1", host="replica")]
    snapshot, _ = build_snapshot(rows, mysqldbs=["dbonline"], name="online", source="test")
    assert len(snapshot["mysql_shards"]) == 1
    assert snapshot["mysql_shards"][0]["env_type"] == "PROD"
    assert snapshot["mysql_shards"][0]["host"] == "replica"
    assert snapshot["mysql_shards"] == build_snapshot(
        [{**r, "env_type": "PROD"} for r in rows], mysqldbs=["dbonline"], name="online", source="test"
    )[0]["mysql_shards"]


@pytest.mark.parametrize("bad_row", [row(end=None), row(start="ZZZZZ", end="00000"), row(host="bad\nhost"), "not an object"])
def test_invalid_refresh_preserves_previous_snapshot(monkeypatch, tmp_path, bad_row):
    good, _ = build_snapshot([row()], mysqldbs=["dbonline"], name="online", source="test")
    output = tmp_path / "shards.json"
    previous = json.dumps(good).encode()
    output.write_bytes(previous)
    export = _write_export(tmp_path, [bad_row])
    monkeypatch.setattr(sys, "argv", _refresh_argv(export, output))
    assert refresh.main() == 1
    assert output.read_bytes() == previous
    assert load_shard_registry(output) == good["mysql_shards"]
    assert not output.with_suffix(".json.tmp").exists()


def test_runtime_rejects_duplicate_catalogs():
    snapshot, _ = build_snapshot([row(), row(start="10000", end="19999")], mysqldbs=["dbonline"], name="online", source="test")
    snapshot["mysql_shards"][1]["catalog"] = snapshot["mysql_shards"][0]["catalog"]
    with pytest.raises(ShardRegistryError, match="duplicate"):
        validate_shard_registry(snapshot)


def test_refreshed_catalogs_and_runtime_routes_agree(tmp_path):
    from app.shard_routing import _resolve_from_entries

    snapshot, _ = build_snapshot([row(start="10000", end="19999")], mysqldbs=["dbonline"], name="online", source="test")
    entries = validate_shard_registry(snapshot)
    catalog = _resolve_from_entries("15000", entries, "PROD", "dbonline")
    catalogs = setup_trino_catalogs.render_catalogs(snapshot)
    assert catalog in catalogs
    assert "jdbc:mysql://h1:3306" in catalogs[catalog]
