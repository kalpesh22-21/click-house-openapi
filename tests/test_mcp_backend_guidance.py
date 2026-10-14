"""Check startup guidance on the actual MCP initialization/tool surfaces."""
import json
import subprocess
import sys

import pytest


@pytest.mark.parametrize("trino_enabled", [False, True])
def test_backend_guidance_on_mcp_surfaces(trino_enabled):
    # Separate interpreter: tool registrations are built at startup, and reloading
    # the shared server in-process would disturb unrelated MCP tests.
    code = '''
import asyncio, json
import app.config
# employee_access_enabled=False mirrors the trino test-suite convention: bare
# Settings() construction validates EEACCESS_BASE_URL otherwise.
settings = app.config.Settings(
    _env_file=None, trino_enabled=ENABLED, employee_access_enabled=False
)
app.config.get_settings = lambda: settings
from app.mcp_server import mcp
async def inspect():
    tools = await mcp.list_tools()
    print(json.dumps({
        "instructions": mcp._mcp_server.create_initialization_options().instructions,
        "tools": {tool.name: tool.description for tool in tools},
    }))
asyncio.run(inspect())
'''.replace("ENABLED", repr(trino_enabled))
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    advertised = json.loads(result.stdout)
    instructions = advertised["instructions"]
    tools = advertised["tools"]
    if trino_enabled:
        for text in [instructions, tools["runQuery"], tools["explainQuery"]]:
            assert "Execution backend: Trino" in text
            assert "input dialect remains ClickHouse-compatible" in text
            assert "dbpcm_datawarehouse.employee" in text
            assert "not universally supported" in text
            assert "DDL is blocked" in text
        assert "live Trino types" in tools["getTableSchema"]
        assert "resolve" in tools["listTables"].lower() or "resolving" in tools["listTables"]
        assert "dbpcm_datawarehouse" in tools["listDatabases"]
        assert "against ClickHouse" not in tools["runQuery"]
    else:
        for text in [instructions, tools["runQuery"], tools["explainQuery"]]:
            assert "Execution backend and SQL dialect: ClickHouse" in text
            assert "Execution backend: Trino" not in text
        assert "ClickHouse column types" in tools["getTableSchema"]
        assert "live Trino types" not in tools["getTableSchema"]


@pytest.mark.parametrize(
    "code,backend",
    [
        ("TRINO_DISCOVERY_UNAVAILABLE", "Trino"),
        ("TRINO_UNAVAILABLE", "Trino"),
        ("CLICKHOUSE_UNAVAILABLE", "ClickHouse"),
    ],
)
def test_unavailable_suffix_names_the_failing_backend(code, backend):
    """The unreachable-server tool suffix names the backend that actually failed.

    The branch keys off the error CODE prefix (stamped at raise time), so it is
    correct even if TRINO_ENABLED changes between import and the failure.
    """
    from app.errors import ClickHouseUnavailableError
    from app.mcp_server import _domain_to_tool_error

    other = "ClickHouse" if backend == "Trino" else "Trino"
    err = _domain_to_tool_error(ClickHouseUnavailableError("backend down", code))
    message = str(err)
    assert f"[{code}]" in message
    assert message.endswith(f"The {backend} server is temporarily unreachable.")
    assert f"The {other} server" not in message