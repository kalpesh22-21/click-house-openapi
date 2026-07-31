"""OpenAI Agents SDK demo — LOCAL client path.

This script connects to the ClickHouse MCP server using the OpenAI Agents SDK
(openai-agents >= 0.17.x) and asks a natural-language question that the agent
answers by calling MCP tools (listDatabases, getTableSchema, runQuery, etc.).

LOCAL CLIENT PATH: the Agents SDK connects to the MCP server directly from
*this process*, so the MCP_URL only needs to be reachable from the machine
running this script.  http://localhost:18090/mcp works perfectly for the local
docker-compose stack.  You do NOT need a public HTTPS URL here — that is only
required by the Responses API hosted path (see responses_api_demo.py).

Prerequisites
-------------
1. The docker-compose stack must be running:
       docker compose up -d --build
   This starts the MCP server on http://localhost:18090/mcp with Bearer token
   "test-key-abc123" and seeds the ClickHouse "analytics" database.

2. Create and activate a dedicated venv for the examples:
       python3.10+ -m venv .venv-examples
       .venv-examples/bin/pip install -r examples/requirements.txt

3. Set your OpenAI API key:
       export OPENAI_API_KEY=sk-...

Usage
-----
    # Default question:
    OPENAI_API_KEY=sk-... .venv-examples/bin/python examples/openai_agent_demo.py

    # Custom question:
    OPENAI_API_KEY=sk-... .venv-examples/bin/python examples/openai_agent_demo.py \
        "How many unique users are in analytics.events?"

    # Override MCP URL or model:
    MCP_URL=https://your-deployed-mcp.example.com/mcp \
    MCP_API_KEY=your-production-key \
    OPENAI_MODEL=gpt-4o \
    OPENAI_API_KEY=sk-... .venv-examples/bin/python examples/openai_agent_demo.py

Environment Variables
---------------------
OPENAI_API_KEY   Required.  Your OpenAI API key (sk-...).
MCP_URL          MCP server URL.  Default: http://localhost:18090/mcp
MCP_API_KEY      Bearer token (a valid OIDC JWT) for the MCP server.  Default: test-key-abc123
OPENAI_MODEL     Model name.  Default: gpt-4.1
"""

from __future__ import annotations

import asyncio
import os
import sys

# ---------------------------------------------------------------------------
# Validate required env vars before importing the heavy SDK
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    print(
        "ERROR: OPENAI_API_KEY is not set.\n"
        "Export it before running:\n"
        "  export OPENAI_API_KEY=sk-...\n"
        "Or prefix the command:\n"
        "  OPENAI_API_KEY=sk-... python examples/openai_agent_demo.py",
        file=sys.stderr,
    )
    sys.exit(1)

MCP_URL = os.environ.get("MCP_URL", "http://localhost:18090/mcp")
MCP_API_KEY = os.environ.get("MCP_API_KEY", "test-key-abc123")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1")

DEFAULT_QUESTION = (
    "What is the total revenue (sum of amount) by country in the analytics.events "
    "table? Also how many users are on each plan?"
)

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

from agents import Agent, Runner  # noqa: E402  (after env-var check)
from agents.mcp import MCPServerStreamableHttp  # noqa: E402


# ---------------------------------------------------------------------------
# Agent instructions
# ---------------------------------------------------------------------------

AGENT_INSTRUCTIONS = """
You are a ClickHouse analytics assistant with read-only access to a ClickHouse
database via MCP tools.

Workflow — always follow this order:
1. Call listDatabases to confirm which databases are accessible.
2. Call listTables to find the tables relevant to the question.
3. Call getTableSchema BEFORE writing any SQL — this gives you exact column
   names, ClickHouse types (e.g. UInt64, Nullable(String), DateTime64), and
   comments.  Skipping this step leads to column-name errors.
4. Optionally call sampleRows to understand data distributions and formats.
5. Call explainQuery to validate your SQL before executing it.
6. Call runQuery to execute the validated query and retrieve results.

Rules:
- All queries are read-only.  INSERT, UPDATE, DELETE, and DDL are blocked by
  the server.
- If runQuery returns truncated=true, rewrite with a narrower WHERE clause or
  smaller LIMIT.
- If a tool call returns an error, read the error message carefully — it
  contains actionable instructions (e.g. "call listDatabases", "call
  explainQuery to diagnose").
- Present results clearly with column headers and formatted numbers.
""".strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    question = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else DEFAULT_QUESTION

    print(f"MCP URL  : {MCP_URL}")
    print(f"Model    : {OPENAI_MODEL}")
    print(f"Question : {question}")
    print()

    params = {
        "url": MCP_URL,
        "headers": {"Authorization": f"Bearer {MCP_API_KEY}"},
        "timeout": 30.0,
    }

    async with MCPServerStreamableHttp(
        params=params,
        name="clickhouse-mcp",
        cache_tools_list=True,
    ) as mcp_server:
        agent = Agent(
            name="ClickHouse Analyst",
            instructions=AGENT_INSTRUCTIONS,
            mcp_servers=[mcp_server],
            model=OPENAI_MODEL,
        )

        result = await Runner.run(agent, question)

    # Print the final answer
    print("=" * 60)
    print("ANSWER")
    print("=" * 60)
    print(result.final_output)

    # Optionally print which MCP tools were called.
    # new_items contains all ToolCallItems from the run; the attribute name and
    # structure changed slightly across SDK versions so we guard carefully.
    try:
        from agents.items import ToolCallItem

        tool_calls = [
            item for item in getattr(result, "new_items", [])
            if isinstance(item, ToolCallItem)
        ]
        if tool_calls:
            print()
            print("Tools called:")
            for item in tool_calls:
                name = getattr(item, "tool_name", None) or "(unknown)"
                print(f"  - {name}")
    except Exception:
        # If the SDK version doesn't expose new_items or ToolCallItem in the
        # expected shape, just skip the tool-call summary.
        pass


if __name__ == "__main__":
    asyncio.run(main())
