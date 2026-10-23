import json
import stat

import pytest

from app.config import Settings
from app.errors import QueryValidationError
from app.trino_diagnostics import trace_execution, record_physical, record_query_id, redacted_sql


@pytest.mark.parametrize("fail", [False, True])
def test_correlated_diagnostics_exclude_literals_and_rows(tmp_path, fail):
    settings = Settings().model_copy(update={"trino_enabled": True, "trino_diagnostics_dir": str(tmp_path / "diagnostics")})

    @trace_execution
    def execute(sql, settings, parameters):
        record_physical("SELECT * FROM TABLE(clickhouse.system.query(query => 'SELECT * FROM x SETTINGS jti=secret'))")
        record_query_id("20261023_123456_00001_test")
        if fail:
            raise QueryValidationError("private exception secret", "TRINO_UNSUPPORTED_SQL")
        return ["value"], [["private result secret"]]

    if fail:
        with pytest.raises(QueryValidationError):
            execute("SELECT x FROM staff WHERE name='private name' AND n=123 /* private comment */", settings)
    else:
        assert execute("SELECT x FROM staff WHERE name='private name' AND n=123 /* private comment */", settings)[1] == [["private result secret"]]
    path = next((tmp_path / "diagnostics").glob("*.jsonl"))
    text = path.read_text()
    assert "private" not in text and "secret" not in text
    record = json.loads(text)
    assert record["logical_sql"] and record["trino_sql"]
    assert record["trino_query_id"] == "20261023_123456_00001_test"
    assert record["status"] == ("error" if fail else "ok")
    assert record["duration_ms"] >= 0 and len(record["correlation_id"]) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_disabled_backend_does_not_write(tmp_path):
    settings = Settings(trino_enabled=False, trino_diagnostics_dir=str(tmp_path / "absent"))

    @trace_execution
    def execute(sql, settings, parameters):
        return "ok"

    assert execute("SELECT 1", settings) == "ok"
    assert not (tmp_path / "absent").exists()


def test_parse_failure_never_falls_back_to_raw_sql():
    assert redacted_sql("SELECT 'secret", "trino") == "[unavailable: unparseable SQL]"


def test_diagnostic_failure_does_not_replace_query_result(tmp_path):
    target = tmp_path / "not_directory"
    target.write_text("occupied")
    settings = Settings().model_copy(update={"trino_enabled": True, "trino_diagnostics_dir": str(target)})

    @trace_execution
    def execute(sql, settings, parameters):
        return "ok"

    assert execute("SELECT 1", settings) == "ok"
