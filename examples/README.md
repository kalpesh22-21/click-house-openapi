# OpenAI Agent Examples for the ClickHouse MCP Server

Two example scripts that show how to query the ClickHouse MCP server from an
OpenAI agent.  They share the same six MCP tools (`listDatabases`, `listTables`,
`getTableSchema`, `sampleRows`, `runQuery`, `explainQuery`) but use different
integration paths.

| Script | SDK Path | Needs public HTTPS? |
|---|---|---|
| `openai_agent_demo.py` | OpenAI Agents SDK (`openai-agents`) | **No** — connects directly from your local process |
| `responses_api_demo.py` | OpenAI Responses API (`openai`) | **Yes** — OpenAI's servers connect to the MCP server |

> **ChatGPT app connector** (the one you configure at chat.openai.com) requires
> OAuth — that path is out of scope for these examples, which use a static
> Bearer token only.

---

## Prerequisites

### 1. Running stack

```bash
# from the repo root
docker compose up -d --build
# wait ~10 seconds for services to become healthy
```

The MCP server will be available at `http://localhost:18090/mcp` with Bearer
token `test-key-abc123` and the ClickHouse `analytics` database seeded with
`events` and `users` tables.

### 2. OpenAI API key

You need an OpenAI account with API access:
```bash
export OPENAI_API_KEY=sk-...
```

### 3. Python 3.10+

`openai-agents` uses `X | Y` union syntax that requires Python 3.10 or newer.
The project's own `.venv` uses Python 3.12 and works fine.

---

## Install example dependencies

Keep examples in their own venv to avoid adding `openai-agents` to the server
image:

```bash
# from the repo root
python3 -m venv .venv-examples
.venv-examples/bin/pip install -r examples/requirements.txt
```

Verified against: **openai-agents 0.17.4**, **openai 2.38.0**, Python 3.12.

---

## Run the LOCAL Agents SDK demo (`openai_agent_demo.py`)

The Agents SDK connects to the MCP server **from your local process**, so
`localhost:18090` works as-is with the running docker-compose stack.

```bash
# Default question
OPENAI_API_KEY=sk-... .venv-examples/bin/python examples/openai_agent_demo.py

# Custom question
OPENAI_API_KEY=sk-... .venv-examples/bin/python examples/openai_agent_demo.py \
    "How many events occurred per day last week in analytics.events?"
```

Environment variables (all optional except `OPENAI_API_KEY`):

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | — (required) | Your OpenAI API key |
| `MCP_URL` | `http://localhost:18090/mcp` | MCP server URL (localhost works here) |
| `MCP_API_KEY` | `test-key-abc123` | Bearer token (a valid OIDC JWT) presented to the MCP server |
| `OPENAI_MODEL` | `gpt-4.1` | OpenAI model to use |

---

## Run the PRODUCTION Responses API demo (`responses_api_demo.py`)

In this mode **OpenAI's servers** reach out to the MCP server, so the
`MCP_URL` must be a **publicly reachable HTTPS URL**.  `localhost` will not
work because OpenAI cannot connect to your laptop.

Deploy the MCP server first (Helm, a cloud VM, or a tunnel like ngrok), then:

```bash
export OPENAI_API_KEY=sk-...
export MCP_URL=https://your-mcp-host.example.com/mcp
export MCP_API_KEY=your-production-bearer-token

.venv-examples/bin/python examples/responses_api_demo.py

# or with a custom question:
.venv-examples/bin/python examples/responses_api_demo.py \
    "List the top 5 countries by total revenue in analytics.events"
```

> **Bearer token note:** The OpenAI Responses API does not store the
> `authorization` value between calls — it must be sent on every request.
> The demo handles this correctly.

---

## Which path needs a public HTTPS URL?

| Path | Needs public HTTPS? | Why |
|---|---|---|
| Agents SDK (`openai_agent_demo.py`) | **No** | Your process connects to the MCP server directly |
| Responses API (`responses_api_demo.py`) | **Yes** | OpenAI's servers make the outbound request |
| ChatGPT app connector | Yes + OAuth | Out of scope — requires OAuth, not a static Bearer token |

---

## Deploying the MCP server publicly

See the main [README](../README.md) for Helm deployment instructions (`mode=mcp`)
and the Remote HTTP usage section.  Once deployed, use the public URL in
`MCP_URL` for the Responses API demo.
