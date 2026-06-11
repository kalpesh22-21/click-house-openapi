#!/usr/bin/env python3
"""Minimal MCP client for the ClickHouse MCP server (streamable-HTTP).

Subcommands:
    list                          list the available tools
    query "<SQL>"                 run a read-only SQL query via the runQuery tool
    query --table db.table        shortcut for: SELECT count(*) FROM db.table

The /mcp endpoint requires a JWT. Mint one from the token service and pass it via
--token or MCP_TOKEN:

    TOKEN=$(curl -s -X POST -H "Authorization: Bearer issuer-key-abc123" \
        -H 'Content-Type: application/json' -d '{"user_name":"alice"}' \
        http://localhost:19000/token | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

    python examples/mcp_client.py --token "$TOKEN" list
    python examples/mcp_client.py --token "$TOKEN" query "SELECT count(*) FROM analytics.events"
    python examples/mcp_client.py --token "$TOKEN" query --table analytics.events

Environment fallbacks: MCP_URL (default http://localhost:18090/mcp), MCP_TOKEN.
Requires the `mcp` package:  pip install mcp
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


@contextlib.asynccontextmanager
async def mcp_session(url: str, token: str | None):
    """Open an initialized MCP session (handles the streamable-HTTP handshake)."""
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def cmd_list(session: ClientSession) -> int:
    result = await session.list_tools()
    print(f"{len(result.tools)} tools:\n")
    for tool in result.tools:
        params = list((tool.inputSchema or {}).get("properties", {}).keys())
        summary = (tool.description or "").strip().splitlines()
        print(f"  • {tool.name}({', '.join(params)})")
        if summary:
            print(f"      {summary[0]}")
    return 0


def _payload(result) -> dict | None:
    """Extract the runQuery tool's {columns, rows, row_count, truncated} payload."""
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    for block in result.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    return None


async def cmd_query(session: ClientSession, sql: str) -> int:
    result = await session.call_tool("runQuery", {"sql": sql})
    if result.isError:
        msg = result.content[0].text if result.content else "unknown error"
        print(f"tool error: {msg}", file=sys.stderr)
        return 1
    data = _payload(result)
    if not data:
        print(f"no result payload: {result}", file=sys.stderr)
        return 1
    print(f"SQL:       {sql}")
    print(f"columns:   {data['columns']}")
    print(f"row_count: {data['row_count']}  truncated: {data['truncated']}")
    for row in data["rows"]:
        print(f"  {row}")
    return 0


async def run(args: argparse.Namespace) -> int:
    async with mcp_session(args.url, args.token) as session:
        if args.command == "list":
            return await cmd_list(session)
        return await cmd_query(session, args.sql)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal MCP client for the ClickHouse MCP server.")
    parser.add_argument(
        "--url",
        default=os.environ.get("MCP_URL", "http://localhost:18090/mcp"),
        help="MCP endpoint (default: $MCP_URL or http://localhost:18090/mcp)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("MCP_TOKEN"),
        help="Bearer JWT for auth (default: $MCP_TOKEN)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list available tools")
    q = sub.add_parser("query", help="run a read-only SQL query via runQuery")
    q.add_argument("sql", nargs="?", help="SQL to run (omit when using --table)")
    q.add_argument("--table", help="shortcut: SELECT count(*) FROM <table>")

    args = parser.parse_args(argv)
    if args.command == "query":
        if not args.sql and args.table:
            args.sql = f"SELECT count(*) AS n FROM {args.table}"
        if not args.sql:
            parser.error("query requires a SQL string or --table")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not args.token:
        print("warning: no token (--token / MCP_TOKEN) — the request will likely 401.", file=sys.stderr)
    try:
        return asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
