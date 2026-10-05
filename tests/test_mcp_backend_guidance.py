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
settings = app.config.Settings(_env_file=None, trino_enabled=ENABLED)
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
