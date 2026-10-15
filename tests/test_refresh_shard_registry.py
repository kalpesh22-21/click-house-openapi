"""Cache warmer shares the runtime pipeline and atomically publishes snapshots."""
import json
import sys

from app import shard_routing as routing
from scripts import refresh_shard_registry as refresh
from tests.test_shard_registry_itdba import row


def test_export_and_runtime_contract(tmp_path, monkeypatch):
    source, output = tmp_path / "export.json", tmp_path / "cache.json"
    source.write_text(json.dumps({"data": [row(), row(mysqlhost="replica.test")]}))
    monkeypatch.setattr(sys, "argv", ["refresh", "--input", str(source), "--output", str(output)])
    assert refresh.main() == 0
    payload = json.loads(output.read_text())
    assert routing.validate_shard_registry(payload) == routing.normalize_itdba_rows([row(mysqlhost="replica.test")])
    assert "catalog" not in payload["entries"][0]


def test_fetch_and_atomic_failure(tmp_path, monkeypatch):
    output = tmp_path / "cache.json"
    output.write_text("previous contents")
    monkeypatch.setattr(sys, "argv", ["refresh", "--output", str(output)])
    monkeypatch.setattr(refresh, "_fetch_itdba_rows", lambda *a: [row()])
    def fail(*a): raise OSError("write failed")
    monkeypatch.setattr(routing.os, "replace", fail)
    assert refresh.main() == 1
    assert output.read_text() == "previous contents"
    assert list(tmp_path.iterdir()) == [output]


def test_invalid_export_does_not_overwrite(tmp_path, monkeypatch):
    source, output = tmp_path / "export.json", tmp_path / "cache.json"
    source.write_text(json.dumps({"data": []}))
    output.write_text("previous contents")
    monkeypatch.setattr(sys, "argv", ["refresh", "--input", str(source), "--output", str(output)])
    assert refresh.main() == 1
    assert output.read_text() == "previous contents"
