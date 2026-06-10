#!/usr/bin/env python3
"""Connect to the ClickHouse MCP server (streamable-HTTP) and list its tools.

The /mcp endpoint requires a valid OIDC JWT, so mint one from the token service
first, then pass it in MCP_TOKEN:

    # against the docker-compose stack (token service on :19000, MCP on :18090):
    TOKEN=$(curl -s -X POST -H "Authorization: Bearer issuer-key-abc123" \
        -H 'Content-Type: application/json' -d '{"user_name":"alice"}' \
        http://localhost:19000/token | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

    MCP_URL=http://localhost:18090/mcp MCP_TOKEN=$TOKEN python examples/mcp_list_tools.py

Environment:
    MCP_URL    MCP streamable-HTTP endpoint (default: http://localhost:18090/mcp)
    MCP_TOKEN  Bearer JWT for auth (omit only if the server has auth disabled)

Requires the `mcp` package:  pip install mcp
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def list_tools(url: str, token: str | None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else None

    # streamablehttp_client yields (read_stream, write_stream, get_session_id).
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            # MCP handshake — required before any other request.
            init = await session.initialize()
            server = init.serverInfo
            print(f"Connected to {server.name} v{server.version} at {url}\n")

            result = await session.list_tools()
            print(f"{len(result.tools)} tools:\n")
            for tool in result.tools:
                summary = (tool.description or "").strip().splitlines()
                first_line = summary[0] if summary else ""
                params = list((tool.inputSchema or {}).get("properties", {}).keys())
                print(f"  • {tool.name}({', '.join(params)})")
                if first_line:
                    print(f"      {first_line}")
            return 0


def main() -> int:
    url = os.environ.get("MCP_URL", "http://localhost:18090/mcp")
    token = os.environ.get("MCP_TOKEN") or None
    if token is None:
        print("warning: MCP_TOKEN not set — the request will likely 401.", file=sys.stderr)
    try:
        return asyncio.run(list_tools(url, token))
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
