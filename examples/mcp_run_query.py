#!/usr/bin/env python3
"""Run COUNT(*) on a table via the ClickHouse MCP server's runQuery tool.

Usage (against the docker-compose stack):
    TOKEN=$(curl -s -X POST -H "Authorization: Bearer issuer-key-abc123" \
        -H 'Content-Type: application/json' -d '{"user_name":"alice"}' \
        http://localhost:19000/token | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

    MCP_URL=http://localhost:18090/mcp MCP_TOKEN=$TOKEN TABLE=analytics.events \
        python examples/mcp_run_query.py

Environment:
    MCP_URL    MCP streamable-HTTP endpoint (default: http://localhost:18090/mcp)
    MCP_TOKEN  Bearer JWT for auth
    TABLE      Table to count rows in (default: analytics.events)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _result_payload(result) -> dict | None:
    """Pull the tool's {columns, rows, row_count, truncated} payload out.

    FastMCP returns the dict as structuredContent; fall back to parsing the JSON
    text block for older servers.
    """
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    for block in result.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    return None


async def run(url: str, token: str | None, table: str) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    sql = f"SELECT count(*) AS n FROM {table}"

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("runQuery", {"sql": sql})

            if result.isError:
                msg = result.content[0].text if result.content else "unknown error"
                print(f"tool error: {msg}", file=sys.stderr)
                return 1

            data = _result_payload(result)
            if not data:
                print(f"no result payload: {result}", file=sys.stderr)
                return 1

            print(f"SQL:     {sql}")
            print(f"columns: {data['columns']}")
            print(f"rows:    {data['rows']}")
            count = data["rows"][0][0] if data["rows"] else None
            print(f"\ncount(*) = {count}")
            return 0


def main() -> int:
    url = os.environ.get("MCP_URL", "http://localhost:18090/mcp")
    token = os.environ.get("MCP_TOKEN") or None
    table = os.environ.get("TABLE", "analytics.events")
    if token is None:
        print("warning: MCP_TOKEN not set — the request will likely 401.", file=sys.stderr)
    try:
        return asyncio.run(run(url, token, table))
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
