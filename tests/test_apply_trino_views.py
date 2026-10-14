"""View-apply contract: statement splitting, strict CREATE, --or-replace, dry-run.

`trino.dbapi.connect` is monkeypatched throughout — no live coordinator, no
network. The dev-only memory catalog means views vanish on restart, so this
script is the re-apply path after every coordinator up/restart; these tests
pin its behaviour without a stack.
"""

from __future__ import annotations

import pytest

from scripts import apply_trino_views as applyv
from scripts.setup_trino_catalogs import render_views

from tests.view_fixtures import TINY_VIEW

SYNTHETIC_TEMPLATES = {
    name: TINY_VIEW.replace(".mini AS", f".{name} AS")
    for name in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")
}
SYNTHETIC_RENDER = render_views(
    {"mysql_shards": [{"catalog": "mysql_shard_qa2_01", "host": "h", "port": 3306}]},
    {"mini": TINY_VIEW},
    proc_center="pc1",
)[("mysql_shard_qa2_01", "mini")]


def _write_view(rendered_dir, shard, view, sql_text):
    path = rendered_dir / shard / f"{view}.sql"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sql_text, encoding="utf-8")
    return path


def _render_tiny(rendered_dir, *shards):
    for shard in shards:
        _write_view(
            rendered_dir,
            shard,
            "mini",
            TINY_VIEW.replace("{{SHARD_CATALOG}}", shard).replace(
                "{{PROC_CENTER}}", "pc1"
            ),
        )


class FakeCursor:
    def __init__(self, recorder, fail_pred=None):
        self.recorder = recorder
        self.fail_pred = fail_pred
        self.closed = False

    def execute(self, statement):
        self.recorder.append(statement)
        if self.fail_pred and self.fail_pred(statement):
            raise RuntimeError("line 1:1: View already exists")

    def fetchone(self):
        return [1]

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, recorder, fail_pred=None):
        self.recorder = recorder
        self.fail_pred = fail_pred
        self.closed = False

    def cursor(self):
        return FakeCursor(self.recorder, self.fail_pred)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_trino(monkeypatch):
    """Patch trino.dbapi.connect; returns (recorder, connect, set_fail_pred)."""

    class Harness:
        recorder = []
        fail_pred = None

    def connect(**kwargs):
        harness.kwargs = kwargs
        return FakeConnection(harness.recorder, harness.fail_pred)

    harness = Harness()
    import trino.dbapi

    monkeypatch.setattr(trino.dbapi, "connect", connect)
    return harness


def test_split_statements_two_statements_comments_stripped():
    statements = applyv.split_statements(TINY_VIEW)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE SCHEMA IF NOT EXISTS dbpcm_datawarehouse.")
    assert not statements[0].lstrip().startswith("--")
    assert statements[1].startswith("CREATE VIEW dbpcm_datawarehouse.")
    assert "{{SHARD_CATALOG}}" in statements[1]  # splitting never substitutes


def test_split_statements_render_is_exactly_two():
    statements = applyv.split_statements(SYNTHETIC_RENDER)
    assert len(statements) == 2
    assert "CREATE SCHEMA IF NOT EXISTS dbpcm_datawarehouse.mysql_shard_qa2_01" in statements[0]
    assert statements[1].startswith(
        "CREATE VIEW dbpcm_datawarehouse.mysql_shard_qa2_01.mini"
    )
    assert "'pc1' AS proc_center" in statements[1]
    # Dropped field-1 guard never resurfaces; retained pins did.
    assert "split_part(CURRENT_USER, '|', 1)" not in statements[1]
    assert "split_part(CURRENT_USER, '|', 2)" in statements[1]
    assert "split_part(CURRENT_USER, '|', 3)" in statements[1]


def test_apply_six_synthetic_views_one_shard(
    tmp_path, fake_trino, capsys
):
    """Multiple synthetic views render and apply in a single shard."""
    assert len(SYNTHETIC_TEMPLATES) == 6
    rendered = render_views(
        {"mysql_shards": [{"catalog": "mysql_shard_qa2_01", "host": "h", "port": 3306}]},
        SYNTHETIC_TEMPLATES,
        proc_center="pc1",
    )
    rendered_dir = tmp_path / "rendered"
    for (shard, stem), sql in rendered.items():
        _write_view(rendered_dir, shard, stem, sql)
    plans = applyv.collect_plans(rendered_dir)
    assert sorted(plans) == ["mysql_shard_qa2_01"]
    assert sorted(plans["mysql_shard_qa2_01"]) == sorted(SYNTHETIC_TEMPLATES)
    rc = applyv.main(["--rendered-dir", str(rendered_dir)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "done: 6/6 view(s) applied" in captured.err
    joined = "\n".join(fake_trino.recorder)
    # Each synthetic view emits a schema statement and a view statement.
    assert joined.count("CREATE SCHEMA IF NOT EXISTS dbpcm_datawarehouse.mysql_shard_qa2_01") == 6
    assert joined.count("CREATE VIEW dbpcm_datawarehouse.mysql_shard_qa2_01.") == 6
    assert "CREATE VIEW dbpcm_datawarehouse.mysql_shard_qa2_01.zeta" in joined


def test_collect_plans_walks_tree(tmp_path):
    _render_tiny(tmp_path, "mysql_shard_b", "mysql_shard_a")
    plans = applyv.collect_plans(tmp_path)
    assert sorted(plans) == ["mysql_shard_a", "mysql_shard_b"]
    for views in plans.values():
        assert sorted(views) == ["mini"] and len(views["mini"]) == 2


def _render_tiny_multi(rendered_dir, shard, *view_names):
    for view in view_names:
        _write_view(
            rendered_dir,
            shard,
            view,
            TINY_VIEW.replace("{{SHARD_CATALOG}}", shard)
            .replace("{{PROC_CENTER}}", "pc1")
            .replace(f".mini AS", f".{view} AS"),
        )


def test_apply_handles_multiple_view_files_per_shard(tmp_path, fake_trino, capsys):
    """A shard dir may hold several rendered views (employee + expense):
    collect_plans groups them under one shard and the apply loop executes
    every file's two statements, counting views across files."""
    _render_tiny_multi(tmp_path, "mysql_shard_a", "mini", "mini2")
    plans = applyv.collect_plans(tmp_path)
    assert sorted(plans["mysql_shard_a"]) == ["mini", "mini2"]
    assert all(len(statements) == 2 for statements in plans["mysql_shard_a"].values())
    rc = applyv.main(["--rendered-dir", str(tmp_path)])
    assert rc == 0
    executed = fake_trino.recorder
    joined = "\n".join(executed[1:])  # executed[0] is the SELECT 1 probe
    assert joined.count("CREATE SCHEMA IF NOT EXISTS dbpcm_datawarehouse.mysql_shard_a") == 2
    assert "CREATE VIEW dbpcm_datawarehouse.mysql_shard_a.mini " in joined + " "
    assert "CREATE VIEW dbpcm_datawarehouse.mysql_shard_a.mini2 " in joined + " "
    captured = capsys.readouterr()
    assert "created mysql_shard_a/mini" in captured.out
    assert "created mysql_shard_a/mini2" in captured.out
    # Summary counts 2 views from the ONE shard dir, not 1 file.
    assert "done: 2/2 view(s) applied" in captured.err


def test_apply_creates_views_and_connection_args(tmp_path, fake_trino):
    _render_tiny(tmp_path, "mysql_shard_a")
    rc = applyv.main(["--rendered-dir", str(tmp_path)])
    assert rc == 0
    assert fake_trino.kwargs == {
        "host": "127.0.0.1",
        "port": 18081,
        "user": "view_admin",
        "http_scheme": "http",
    }
    executed = fake_trino.recorder
    assert executed[0] == "SELECT 1"  # readiness probe on the reused connection
    joined = "\n".join(executed[1:])
    assert "CREATE SCHEMA IF NOT EXISTS dbpcm_datawarehouse.mysql_shard_a" in joined
    assert "CREATE VIEW dbpcm_datawarehouse.mysql_shard_a.mini" in joined
    assert "OR REPLACE" not in joined


def test_strict_create_existing_view_fails_shard_but_continues(
    tmp_path, fake_trino, capsys
):
    _render_tiny(tmp_path, "mysql_shard_a", "mysql_shard_b")
    # mysql_shard_a sorts first: its CREATE VIEW failing must NOT stop shard b.
    fake_trino.fail_pred = (
        lambda stmt: stmt.startswith("CREATE VIEW") and "mysql_shard_a" in stmt
    )
    rc = applyv.main(["--rendered-dir", str(tmp_path)])
    assert rc == 1
    joined = "\n".join(fake_trino.recorder)
    assert "CREATE SCHEMA IF NOT EXISTS dbpcm_datawarehouse.mysql_shard_a" in joined
    assert "CREATE SCHEMA IF NOT EXISTS dbpcm_datawarehouse.mysql_shard_b" in joined
    assert "CREATE VIEW dbpcm_datawarehouse.mysql_shard_b.mini" in joined
    captured = capsys.readouterr()
    assert "FAILED  mysql_shard_a/mini" in captured.out
    assert "RuntimeError" in captured.out and "View already exists" not in captured.out
    assert "created mysql_shard_b/mini" in captured.out
    # Summary counts views (2), not (shard, views) tuples (would print 4).
    assert "done: 1/2 view(s) applied" in captured.err


def test_or_replace_rewrites_at_apply_time_leaving_files_untouched(
    tmp_path, fake_trino, capsys
):
    path = _write_view(
        tmp_path,
        "mysql_shard_a",
        "mini",
        TINY_VIEW.replace("{{SHARD_CATALOG}}", "mysql_shard_a").replace(
            "{{PROC_CENTER}}", "pc1"
        ),
    )
    rc = applyv.main(["--rendered-dir", str(tmp_path), "--or-replace"])
    assert rc == 0
    joined = "\n".join(fake_trino.recorder)
    assert "CREATE OR REPLACE VIEW dbpcm_datawarehouse.mysql_shard_a.mini" in joined
    assert "CREATE SCHEMA IF NOT EXISTS dbpcm_datawarehouse." in joined
    assert "\nCREATE VIEW dbpcm_datawarehouse." not in joined
    # The strict template stance survives: on-disk file still plain CREATE.
    on_disk = path.read_text(encoding="utf-8")
    assert "CREATE VIEW dbpcm_datawarehouse." in on_disk
    assert "OR REPLACE" not in on_disk
    assert "created mysql_shard_a/mini" in capsys.readouterr().out


def test_summary_counts_views_not_statement_tuples(tmp_path, fake_trino, capsys):
    # Regression: plans.items() yields (shard, {view: statements}) tuples —
    # summing len() over .items() counted those tuples (always 2), printing
    # "2/2 view(s)" for a single applied view. The summary must count views.
    _render_tiny(tmp_path, "mysql_shard_a")
    rc = applyv.main(["--rendered-dir", str(tmp_path)])
    assert rc == 0
    out, err = capsys.readouterr()
    assert "created mysql_shard_a/mini" in out
    assert "done: 1/1 view(s) applied" in err


def test_dry_run_prints_plan_and_executes_nothing(tmp_path, fake_trino, capsys):
    _render_tiny(tmp_path, "mysql_shard_a")
    rc = applyv.main(["--rendered-dir", str(tmp_path), "--dry-run", "--or-replace"])
    assert rc == 0
    assert fake_trino.recorder == [] and not hasattr(fake_trino, "kwargs")
    out = capsys.readouterr().out
    assert "skipped mysql_shard_a/mini (dry-run)" in out
    assert "would execute: CREATE SCHEMA IF NOT EXISTS dbpcm_datawarehouse." in out
    assert "would execute: CREATE OR REPLACE VIEW dbpcm_datawarehouse." in out


def test_ready_timeout_is_bounded_with_clear_error(tmp_path, monkeypatch, capsys):
    _render_tiny(tmp_path, "mysql_shard_a")

    def refusing_connect(**kwargs):
        raise OSError("connection refused")

    import trino.dbapi

    monkeypatch.setattr(trino.dbapi, "connect", refusing_connect)
    rc = applyv.main(
        [
            "--rendered-dir",
            str(tmp_path),
            "--ready-timeout",
            "0.2",
            "--ready-interval",
            "0.05",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "not ready" in err and "docker compose -f docker-compose.trino.yml" in err


def test_probe_closes_connection_when_execute_raises(tmp_path, monkeypatch, capsys):
    """Probe-leak regression: connect() succeeded but the SELECT 1 probe raised.

    The half-open connection must be closed before the retry loop continues —
    otherwise every attempt leaks one coordinator-side session. Bounded retry
    semantics (rc 1 with the not-ready remediation hint) are unchanged.
    """
    _render_tiny(tmp_path, "mysql_shard_a")
    opened = []

    class FailingCursor:
        def execute(self, statement):
            raise RuntimeError("coordinator still starting")

        def fetchone(self):
            raise AssertionError("unreachable: execute already raised")

        def close(self):
            pass

    class TrackedConnection:
        def __init__(self):
            self.closed = False

        def cursor(self):
            return FailingCursor()

        def close(self):
            self.closed = True

    def connect(**kwargs):
        conn = TrackedConnection()
        opened.append(conn)
        return conn

    import trino.dbapi

    monkeypatch.setattr(trino.dbapi, "connect", connect)
    rc = applyv.main(
        [
            "--rendered-dir",
            str(tmp_path),
            "--ready-timeout",
            "0.2",
            "--ready-interval",
            "0.05",
        ]
    )
    assert rc == 1
    assert len(opened) > 1  # bounded retries did attempt re-probes
    assert all(conn.closed for conn in opened)
    assert "not ready" in capsys.readouterr().err


def test_missing_or_empty_rendered_dir_fails_with_pointer(tmp_path, capsys):
    rc = applyv.main(["--rendered-dir", str(tmp_path / "missing")])
    assert rc == 1
    assert "setup_trino_catalogs.py" in capsys.readouterr().err
    empty = tmp_path / "rendered"
    empty.mkdir()
    rc = applyv.main(["--rendered-dir", str(empty)])
    assert rc == 1
    assert "setup_trino_catalogs.py" in capsys.readouterr().err


def test_rendered_file_without_statements_is_failed_not_fatal(
    tmp_path, fake_trino, capsys
):
    _write_view(tmp_path, "mysql_shard_a", "empty", "-- only comments\n")
    _render_tiny(tmp_path, "mysql_shard_b")
    rc = applyv.main(["--rendered-dir", str(tmp_path)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAILED  mysql_shard_a/empty: no statements" in out
    assert "created mysql_shard_b/mini" in out
