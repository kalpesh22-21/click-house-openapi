#!/usr/bin/env python3

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from rich.console import Console
from rich.json import JSON
from rich.table import Table


QUERY = """
SELECT COUNT(*) AS employee_count
FROM dbpcm_warehouse.employee
""".strip()

# Target the same table the QUERY hits, so listTables / sampleRows / explainQuery
# exercise the tools against a real, known object.
DATABASE = "dbpcm_warehouse"
TABLE = "employee"
SAMPLE_LIMIT = 5

console = Console()


def build_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    return headers


def get_tool_input_schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None)

    if schema is None:
        schema = getattr(tool, "input_schema", None)

    return schema or {}


def build_run_query_arguments(tool: Any, query: str) -> dict[str, Any]:
    """
    Inspect the runQuery input schema and determine whether the server
    expects a field named query, sql, statement, or sqlQuery.
    """
    schema = get_tool_input_schema(tool)
    properties = schema.get("properties", {})

    preferred_fields = [
        "query",
        "sql",
        "statement",
        "sqlQuery",
        "sql_query",
    ]

    for field_name in preferred_fields:
        if field_name in properties:
            return {field_name: query}

    required = schema.get("required", [])

    if len(required) == 1:
        return {required[0]: query}

    raise ValueError(
        "Could not determine the runQuery argument name.\n"
        f"runQuery input schema:\n{json.dumps(schema, indent=2)}"
    )


def content_to_python(content: Any) -> Any:
    """
    Convert MCP content objects to printable Python values.
    """
    if hasattr(content, "text"):
        text = content.text

        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    if hasattr(content, "data"):
        return content.data

    if hasattr(content, "model_dump"):
        return content.model_dump()

    return str(content)


def print_query_result(result: Any, label: str = "runQuery") -> None:
    if getattr(result, "isError", False):
        console.print(f"[red]{label} returned an error.[/red]")

    structured_content = getattr(result, "structuredContent", None)

    if structured_content is None:
        structured_content = getattr(result, "structured_content", None)

    if structured_content is not None:
        console.print("\n[bold]Structured result[/bold]")
        console.print(JSON.from_data(structured_content))
        return

    contents = getattr(result, "content", [])

    if not contents:
        console.print(f"[yellow]{label} returned no content.[/yellow]")
        return

    parsed_contents = [content_to_python(item) for item in contents]

    for parsed in parsed_contents:
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            columns = list(parsed[0].keys())

            table = Table(title="Query Result")

            for column in columns:
                table.add_column(str(column))

            for row in parsed:
                table.add_row(
                    *[
                        str(row.get(column, ""))
                        for column in columns
                    ]
                )

            console.print(table)

        elif isinstance(parsed, dict):
            console.print(JSON.from_data(parsed))

        else:
            console.print(parsed)


def find_tool(tools_result: Any, name: str) -> Any:
    """Return the tool named *name* from a list_tools() result, or None."""
    return next(
        (tool for tool in tools_result.tools if tool.name == name),
        None,
    )


async def call_and_print(
    session: ClientSession,
    tool_name: str,
    arguments: dict[str, Any],
) -> bool:
    """Call *tool_name*, print its arguments and result. Return True on error."""
    console.print(f"\n[bold]{tool_name}[/bold]")
    console.print(JSON.from_data(arguments))

    result = await session.call_tool(tool_name, arguments=arguments)

    console.print()
    print_query_result(result, label=tool_name)

    return bool(getattr(result, "isError", False))


async def run_mcp_query(
    mcp_url: str,
    token: str | None,
    timeout_seconds: float,
) -> int:
    headers = build_headers(token)

    timeout = httpx.Timeout(
        connect=timeout_seconds,
        read=timeout_seconds,
        write=timeout_seconds,
        pool=timeout_seconds,
    )

    console.print(f"Connecting to: [bold]{mcp_url}[/bold]")

    try:
        async with httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        ) as http_client:

            async with streamable_http_client(
                url=mcp_url,
                http_client=http_client,
            ) as transport:

                read_stream = transport[0]
                write_stream = transport[1]

                async with ClientSession(
                    read_stream,
                    write_stream,
                ) as session:

                    initialization_result = await session.initialize()

                    server_info = initialization_result.serverInfo

                    console.print(
                        "Connected to: "
                        f"[green]{getattr(server_info, 'name', 'Unknown')}[/green] "
                        f"{getattr(server_info, 'version', '')}"
                    )

                    tools_result = await session.list_tools()

                    run_query_tool = find_tool(tools_result, "runQuery")
                    explain_query_tool = find_tool(tools_result, "explainQuery")

                    if run_query_tool is None:
                        available_tools = [
                            tool.name
                            for tool in tools_result.tools
                        ]

                        console.print(
                            "[red]The MCP server does not expose "
                            "a runQuery tool.[/red]"
                        )
                        console.print(
                            "Available tools: "
                            + ", ".join(available_tools)
                        )
                        return 1

                    console.print("\n[bold]Query under test[/bold]")
                    console.print(QUERY)

                    any_error = False

                    # listTables — enumerate the tables in the target database.
                    any_error |= await call_and_print(
                        session,
                        "listTables",
                        {"database": DATABASE},
                    )

                    # sampleRows — pull a few raw rows from the target table.
                    any_error |= await call_and_print(
                        session,
                        "sampleRows",
                        {
                            "database": DATABASE,
                            "table": TABLE,
                            "limit": SAMPLE_LIMIT,
                        },
                    )

                    # explainQuery — validate the plan without executing (same SQL
                    # field name as runQuery, so reuse the schema-based detector).
                    if explain_query_tool is not None:
                        any_error |= await call_and_print(
                            session,
                            "explainQuery",
                            build_run_query_arguments(
                                explain_query_tool,
                                QUERY,
                            ),
                        )
                    else:
                        console.print(
                            "\n[yellow]explainQuery tool not exposed — "
                            "skipping.[/yellow]"
                        )

                    # runQuery — execute the query for real.
                    any_error |= await call_and_print(
                        session,
                        "runQuery",
                        build_run_query_arguments(
                            run_query_tool,
                            QUERY,
                        ),
                    )

                    return 1 if any_error else 0

    except httpx.TimeoutException:
        console.print(
            f"[red]Request timed out after "
            f"{timeout_seconds} seconds.[/red]"
        )
        return 2

    except Exception as exc:
        console.print(
            f"[red]MCP request failed:[/red] "
            f"{type(exc).__name__}: {exc}"
        )
        return 2


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Exercise the MCP tools (listTables, sampleRows, explainQuery, "
            "runQuery) against a streamable-HTTP MCP endpoint."
        )
    )

    parser.add_argument(
        "--url",
        default=os.getenv("MCP_URL"),
        help="Streamable HTTP MCP endpoint, or set MCP_URL.",
    )

    parser.add_argument(
        "--token",
        default=os.getenv("MCP_TOKEN"),
        help="Optional bearer token, or set MCP_TOKEN.",
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout in seconds. Default: 60.",
    )

    args = parser.parse_args()

    if not args.url:
        parser.error(
            "MCP URL is required. Pass --url or set MCP_URL."
        )

    return args


def main() -> None:
    args = parse_arguments()

    exit_code = asyncio.run(
        run_mcp_query(
            mcp_url=args.url,
            token=args.token,
            timeout_seconds=args.timeout,
        )
    )

    sys.exit(exit_code)


if __name__ == "__main__":
    main()