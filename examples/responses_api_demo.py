"""OpenAI Responses API demo — PRODUCTION hosted-MCP path.

This script uses the OpenAI Python SDK's Responses API with a remote MCP tool
to query the ClickHouse MCP server.  In this mode OpenAI's servers connect
directly to the MCP server — the request is fully server-side.

PRODUCTION / HOSTED PATH: because OpenAI's infrastructure makes the outbound
request to the MCP server, the MCP_URL MUST be a publicly reachable HTTPS URL.
http://localhost:18090/mcp will NOT work here — OpenAI cannot reach your
laptop.  Use this demo after you have deployed the MCP server to a public
endpoint (e.g. a cloud VM, Kubernetes + Ingress, or a tunnel service such as
ngrok for testing).

Bearer token caveat (from the OpenAI documentation):
    The Responses API does NOT store the `authorization` value server-side
    between requests — it must be sent on every request.  This demo does that
    correctly by including the token in every responses.create() call.

Prerequisites
-------------
1. Deploy the MCP server to a public HTTPS endpoint and set MCP_URL.
2. Create and activate a dedicated venv for the examples:
       python3.10+ -m venv .venv-examples
       .venv-examples/bin/pip install -r examples/requirements.txt
3. Export your keys:
       export OPENAI_API_KEY=sk-...
       export MCP_URL=https://your-mcp-host.example.com/mcp
       export MCP_API_KEY=your-production-bearer-token

Usage
-----
    OPENAI_API_KEY=sk-... \
    MCP_URL=https://your-mcp-host.example.com/mcp \
    MCP_API_KEY=your-production-bearer-token \
    .venv-examples/bin/python examples/responses_api_demo.py

    # Custom question as first positional argument:
    OPENAI_API_KEY=sk-... \
    MCP_URL=https://your-mcp-host.example.com/mcp \
    MCP_API_KEY=your-production-bearer-token \
    .venv-examples/bin/python examples/responses_api_demo.py \
        "List the top 5 events by count from analytics.events"

Environment Variables
---------------------
OPENAI_API_KEY   Required.  Your OpenAI API key (sk-...).
MCP_URL          Required for production.  PUBLIC HTTPS MCP server URL.
                 Default: http://localhost:18090/mcp — but this will fail
                 because OpenAI servers cannot reach localhost.
MCP_API_KEY      Bearer token for the MCP server.  Default: test-key-abc123
OPENAI_MODEL     Model name.  Default: gpt-4.1
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# Validate required env vars before importing the SDK
# ---------------------------------------------------------------------------

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    print(
        "ERROR: OPENAI_API_KEY is not set.\n"
        "Export it before running:\n"
        "  export OPENAI_API_KEY=sk-...\n"
        "Or prefix the command:\n"
        "  OPENAI_API_KEY=sk-... python examples/responses_api_demo.py",
        file=sys.stderr,
    )
    sys.exit(1)

# MCP_URL must be a public HTTPS URL in this hosted mode.
MCP_URL = os.environ.get("MCP_URL", "http://localhost:18090/mcp")
MCP_API_KEY = os.environ.get("MCP_API_KEY", "test-key-abc123")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1")

# Warn if localhost is used — it will not work in hosted mode.
if "localhost" in MCP_URL or "127.0.0.1" in MCP_URL:
    print(
        "WARNING: MCP_URL appears to be a local address:\n"
        f"         {MCP_URL}\n"
        "OpenAI's servers connect to the MCP server from the internet.\n"
        "localhost is NOT reachable from OpenAI's infrastructure.\n"
        "Set MCP_URL to a public HTTPS URL, e.g.:\n"
        "  export MCP_URL=https://your-mcp-host.example.com/mcp\n",
        file=sys.stderr,
    )

DEFAULT_QUESTION = (
    "What is the total revenue (sum of amount) by country in the analytics.events "
    "table? Also how many users are on each plan?"
)

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

import openai  # noqa: E402  (after env-var check)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    question = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else DEFAULT_QUESTION

    print(f"MCP URL  : {MCP_URL}")
    print(f"Model    : {OPENAI_MODEL}")
    print(f"Question : {question}")
    print()

    client = openai.OpenAI(api_key=OPENAI_API_KEY)

    # The `authorization` field carries the Bearer token.  Per OpenAI's docs the
    # Responses API does NOT persist this value between requests, so it must be
    # included on every responses.create() call — this demo does so correctly.
    #
    # `require_approval="never"` lets the model invoke MCP tools without an
    # additional human-in-the-loop confirmation step.  All our tools are
    # read-only so this is safe here.
    mcp_tool = {
        "type": "mcp",
        "server_label": "clickhouse",
        "server_url": MCP_URL,
        "authorization": MCP_API_KEY,
        "require_approval": "never",
    }

    system_instructions = (
        "You are a ClickHouse analytics assistant with read-only access via MCP tools. "
        "Workflow: call listDatabases, then listTables, then getTableSchema BEFORE "
        "writing any SQL, optionally sampleRows, then explainQuery to validate, then "
        "runQuery to execute.  All queries are read-only."
    )

    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=system_instructions,
        input=question,
        tools=[mcp_tool],
    )

    print("=" * 60)
    print("ANSWER")
    print("=" * 60)
    print(response.output_text)


if __name__ == "__main__":
    main()
