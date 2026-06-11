# ClickHouse Query API

A read-only REST API **and MCP server** that sits in front of ClickHouse and is designed to be consumed by a **ChatGPT Custom GPT Action** or any **MCP-capable LLM client** (Claude Desktop, Claude Code, etc.). The LLM calls this API to discover schema and execute SQL without ever touching ClickHouse directly.

## OpenAI Agent Examples

Runnable examples showing how to drive the MCP server from an OpenAI agent
(Agents SDK local path and Responses API hosted path) are in
[`examples/`](examples/README.md).

## Architecture

```
ChatGPT GPT Action               MCP Client (Claude Desktop / Claude Code)
      │  Bearer token + SQL             │  MCP tools (stdio or HTTP + Bearer)
      ▼                                 ▼
ClickHouse Query API  (this service — same Docker image, same guardrails)
  • SQL guardrails (allowlist, denylist, LIMIT injection)  ← app/service.py
  • Read-only ClickHouse session settings (readonly=1, execution caps)
  • Bearer auth (constant-time comparison)
      │
      ▼
ClickHouse (your cluster)
```

Both the REST API and the MCP server use **exactly the same** guardrail and execution code in `app/service.py`. There is no weaker copy.

## REST API Endpoints

| Method | Path | operationId | Purpose |
|--------|------|-------------|---------|
| GET | `/databases` | `listDatabases` | List accessible databases |
| GET | `/tables?database=` | `listTables` | List tables in a database |
| GET | `/tables/schema?database=&table=` | `getTableSchema` | Column names, types, comments |
| GET | `/tables/sample?database=&table=&limit=` | `sampleRows` | Preview a few rows |
| POST | `/query` | `runQuery` | Execute a read-only SQL query |
| POST | `/query/explain` | `explainQuery` | EXPLAIN a query (validate without executing) |
| GET | `/health` | `healthCheck` | Liveness/readiness probe |

## MCP Mode

The same Docker image can run as an MCP server — exposing the same six operations as MCP tools callable by any MCP-capable client (Claude Desktop, Claude Code, the MCP Inspector, etc.).

### MCP tools

| Tool | Purpose |
|------|---------|
| `listDatabases` | Discover accessible databases |
| `listTables` | List tables in a database |
| `getTableSchema` | Column names, types, comments (call before writing a query) |
| `sampleRows` | Preview up to 50 real rows |
| `runQuery` | Execute a validated read-only SQL query |
| `explainQuery` | EXPLAIN a query without executing it |

### Recommended LLM workflow

```
listDatabases → listTables → getTableSchema → (sampleRows) → explainQuery → runQuery
```

Always call `getTableSchema` before writing a `SELECT` — it prevents column name and type errors.

### Shared guardrails

Both REST and MCP transports share the **same** guardrail code path in `app/service.py`:
- SQL allowlist (`SELECT`, `WITH`, `EXPLAIN`, `SHOW`, `DESCRIBE`, `DESC` only)
- SQL denylist (blocks `INSERT`, `DROP`, `ALTER`, `CREATE`, `SET`, external table functions `url()`, `s3()`, `file()`, `remote()`, etc.)
- Auto-`LIMIT` injection
- `readonly=1` + execution time + row caps applied on every ClickHouse call
- `ALLOWED_DATABASES` allowlist enforced on every schema operation

MCP guardrail rejections are returned as tool errors with descriptive messages so the model can self-correct.

### Local stdio usage (Claude Desktop / MCP Inspector)

`API_KEY` is **not required** for the stdio transport. The local subprocess trust model means no network authentication is needed. You may omit `API_KEY` entirely.

```bash
# 1. Set required env vars (API_KEY is NOT needed for stdio)
export CLICKHOUSE_HOST=clickhouse.example.com
export CLICKHOUSE_USER=default
export CLICKHOUSE_PASSWORD=your-password
export ALLOWED_DATABASES=analytics,reporting

# 2. Run the MCP server (stdio transport — default)
python -m app.mcp_server
# or explicitly:
python -m app.mcp_server --transport stdio
```

**Claude Desktop `claude_desktop_config.json`:**

```json
{
  "mcpServers": {
    "clickhouse": {
      "command": "python",
      "args": ["-m", "app.mcp_server", "--transport", "stdio"],
      "cwd": "/path/to/clickhouse-api",
      "env": {
        "CLICKHOUSE_HOST": "clickhouse.example.com",
        "CLICKHOUSE_PORT": "8443",
        "CLICKHOUSE_USER": "default",
        "CLICKHOUSE_PASSWORD": "your-clickhouse-password",
        "CLICKHOUSE_DATABASE": "default",
        "CLICKHOUSE_SECURE": "true",
        "ALLOWED_DATABASES": "analytics,reporting"
      }
    }
  }
}
```

`API_KEY` is never checked for the stdio transport and can be omitted from the config entirely.

### Remote HTTP usage (k8s / server)

```bash
# Start MCP server in HTTP transport (OIDC/JWT Bearer auth enforced per request)
MCP_TRANSPORT=http MCP_PORT=8000 MCP_PATH=/mcp \
  OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json \
  OIDC_ISSUER=https://idp.example.com/ \
  OIDC_AUDIENCE=clickhouse-api \
  python -m app.mcp_server --transport http
```

The server listens on `0.0.0.0:8000` and exposes:
- `POST /mcp` — MCP streamable-HTTP endpoint (requires a valid OIDC JWT as `Authorization: Bearer <token>`)
- `GET /health` — health probe (no auth required)
- `GET /.well-known/oauth-protected-resource` — OAuth 2.0 Protected Resource Metadata (no auth)

**OAuth for interactive clients.** The MCP HTTP transport is an OAuth 2.0
Protected Resource: OAuth-capable clients (Claude Desktop, ChatGPT, MCP
Inspector, VS Code) discover the authorization server via the well-known
endpoint and obtain a token through authorization-code + PKCE — no hand-minted
tokens. See **[docs/oauth.md](docs/oauth.md)** for the full flow and an Azure
Entra ID / Keycloak setup walkthrough.

Connect from an MCP client (manual token):
```
URL:   https://your-mcp-host.example.com/mcp
Auth:  Bearer <OIDC JWT>
```

### Deploy via Helm (mode=mcp)

```bash
helm install clickhouse-mcp ./helm/clickhouse-api \
  --namespace clickhouse-mcp \
  --create-namespace \
  --set mode=mcp \
  --set secrets.connection.CLICKHOUSE_HOST=clickhouse.prod.svc.cluster.local \
  --set secrets.clickhousePassword=<your-ch-password> \
  --set secrets.apiKey=<your-api-key> \
  --set config.ALLOWED_DATABASES=analytics,reporting \
  --set config.MCP_PORT=8000 \
  --set config.MCP_PATH=/mcp
```

When `mode=mcp` the chart automatically:
- Sets `MODE=mcp` and `MCP_TRANSPORT=http` in the pod environment
- Configures liveness/readiness probes on `GET /health`
- Uses the MCP port for the container port

To use the REST API mode (the default):
```bash
helm install clickhouse-api ./helm/clickhouse-api \
  --set mode=api \
  --set secrets.clickhousePassword=<password> \
  --set secrets.apiKey=<key>
```

## Environment Variables

Variables marked **Secret** are managed in the Kubernetes Secret (via `secrets.*` in values.yaml).
Variables marked **ConfigMap** are managed in the Kubernetes ConfigMap (via `config.*` in values.yaml).

| Variable | Required | Default | Source | Description |
|----------|----------|---------|--------|-------------|
| `CLICKHOUSE_HOST` | Yes | — | Secret | ClickHouse server hostname |
| `CLICKHOUSE_PORT` | No | `8443` | Secret | ClickHouse HTTP(S) port |
| `CLICKHOUSE_USER` | Yes | — | Secret | ClickHouse username |
| `CLICKHOUSE_PASSWORD` | Yes | — | Secret | ClickHouse password |
| `CLICKHOUSE_DATABASE` | No | `default` | Secret | Default database |
| `CLICKHOUSE_SECURE` | No | `true` | Secret | Use TLS for ClickHouse connection |
| `API_KEY` | REST/HTTP: Yes | `""` | Secret | Bearer token for REST API and MCP HTTP transport. Not required for MCP stdio. |
| `MAX_EXECUTION_TIME` | No | `30` | ConfigMap | Max query wall-clock seconds |
| `MAX_RESULT_ROWS` | No | `10000` | ConfigMap | ClickHouse-side hard row cap |
| `MAX_ROWS_TO_READ` | No | `100000000` | ConfigMap | ClickHouse-side max rows scanned |
| `DEFAULT_LIMIT` | No | `1000` | ConfigMap | Injected LIMIT when query has none |
| `MAX_RESPONSE_ROWS` | No | `1000` | ConfigMap | Max rows in JSON response |
| `ALLOWED_DATABASES` | No | `*` | ConfigMap | Comma-separated DB allowlist; `*` = all |
| `APP_PORT` | No | `8000` | ConfigMap | Port uvicorn listens on (REST mode) |
| `LOG_LEVEL` | No | `INFO` | ConfigMap | Python logging level |
| `PUBLIC_BASE_URL` | No | placeholder | ConfigMap | HTTPS base URL for OpenAPI servers block |
| `MODE` | No | `api` | ConfigMap | `api` = REST server, `mcp` = MCP server |
| `MCP_TRANSPORT` | No | `stdio` | ConfigMap | `stdio` or `http` |
| `MCP_PORT` | No | `8000` | ConfigMap | MCP HTTP server port |
| `MCP_PATH` | No | `/mcp` | ConfigMap | MCP streamable-HTTP mount path |

## Running Locally

### Prerequisites

- Python 3.10+ (the MCP SDK requires Python >= 3.10)
- A reachable ClickHouse instance

### Setup

```bash
# 1. Clone and enter the repo
cd clickhouse-api

# 2. Create a virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your ClickHouse credentials and API_KEY

# 5a. Start the REST API server
uvicorn app.main:app --reload --port 8000

# 5b. Or start the MCP server (stdio)
python -m app.mcp_server

# 5c. Or start the MCP server (HTTP with auth)
MCP_TRANSPORT=http python -m app.mcp_server
```

The interactive REST docs are at http://localhost:8000/docs.

The OpenAPI schema (for GPT Action import) is at http://localhost:8000/openapi.json.

## Docker

### Build

```bash
docker build -t clickhouse-api:latest .
```

### Run — REST API mode (default)

```bash
docker run --rm -p 8000:8000 \
  --env-file .env \
  clickhouse-api:latest
```

### Run — MCP stdio mode

```bash
docker run --rm -i \
  --env-file .env \
  -e MODE=mcp -e MCP_TRANSPORT=stdio \
  clickhouse-api:latest
```

### Run — MCP HTTP mode

```bash
docker run --rm -p 8000:8000 \
  --env-file .env \
  -e MODE=mcp -e MCP_TRANSPORT=http \
  clickhouse-api:latest
```

### Push to a registry

```bash
docker tag clickhouse-api:latest your-registry/clickhouse-api:1.0.0
docker push your-registry/clickhouse-api:1.0.0
```

## Helm Deployment

### Prerequisites

- Helm 3.x
- A Kubernetes cluster
- `kubectl` configured for the target cluster

### Install (REST API mode)

```bash
helm install clickhouse-api ./helm/clickhouse-api \
  --namespace clickhouse-api \
  --create-namespace \
  --set secrets.connection.CLICKHOUSE_HOST=clickhouse.prod.svc.cluster.local \
  --set config.PUBLIC_BASE_URL=https://clickhouse-api.example.com \
  --set secrets.clickhousePassword=<your-ch-password> \
  --set secrets.apiKey=<your-api-key>
```

### Install (MCP HTTP mode)

```bash
helm install clickhouse-mcp ./helm/clickhouse-api \
  --namespace clickhouse-mcp \
  --create-namespace \
  --set mode=mcp \
  --set secrets.connection.CLICKHOUSE_HOST=clickhouse.prod.svc.cluster.local \
  --set secrets.clickhousePassword=<your-ch-password> \
  --set secrets.apiKey=<your-api-key>
```

### Upgrade

```bash
helm upgrade clickhouse-api ./helm/clickhouse-api \
  --namespace clickhouse-api \
  -f my-production-values.yaml
```

### Using a pre-existing Secret (recommended for production)

```yaml
# production-values.yaml
secrets:
  existingSecret:
    name: clickhouse-api-credentials
```

```bash
helm upgrade clickhouse-api ./helm/clickhouse-api -f production-values.yaml
```

### Enable Ingress

```yaml
ingress:
  enabled: true
  className: nginx
  host: clickhouse-api.example.com
  tls:
    enabled: true
    secretName: clickhouse-api-tls
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
```

## Wiring Up as a ChatGPT Custom GPT Action

1. **Deploy the REST API** to a public HTTPS endpoint.

2. **Set `PUBLIC_BASE_URL`** to your public HTTPS URL (e.g. `https://clickhouse-api.example.com`).

3. **In the ChatGPT editor**: Configure → Actions → Add Action → Import from URL:
   ```
   https://your-public-host.example.com/openapi.json
   ```

4. **Set authentication**: API Key → Bearer → your `API_KEY` value.

5. **Test**: *"What databases are available?"* or *"Describe the schema of table X in database Y."*

### Recommended GPT System Prompt additions

```
You have access to a ClickHouse analytics database via the ClickHouse Query API.
Workflow:
1. Call listDatabases to see available databases.
2. Call listTables to find relevant tables.
3. ALWAYS call getTableSchema before writing a SELECT query.
4. Optionally call sampleRows to understand data formats.
5. Call explainQuery to validate complex queries before running them.
6. Call runQuery to execute the final query.

If a query returns truncated=true, rewrite with a narrower WHERE clause or smaller LIMIT.
```

## SQL Security Model

The API enforces defense-in-depth against LLM-generated SQL — identically across both REST and MCP transports:

1. **Single statement only** — semicolons in the middle of a query are rejected.
2. **Allowlist** — only `SELECT`, `WITH`, `EXPLAIN`, `SHOW`, `DESCRIBE`, `DESC` are accepted.
3. **Denylist** — blocks `INSERT`, `ALTER`, `DROP`, `CREATE`, `TRUNCATE`, `RENAME`, `ATTACH`, `DETACH`, `OPTIMIZE`, `GRANT`, `REVOKE`, `SET`, `KILL`, `SYSTEM`, `DELETE`, `UPDATE`, `INTO OUTFILE`, `FORMAT`.
4. **Table-function denylist** — blocks exfiltration via `url()`, `file()`, `remote()`, `s3()`, `gcs()`, `mysql()`, `postgresql()`, `executable()`, and 15 more.
5. **Auto-LIMIT** — queries without a LIMIT clause get one injected automatically.
6. **ClickHouse session settings** — every query runs with `readonly=1`, `max_execution_time`, `max_result_rows`, `result_overflow_mode='throw'`, `max_rows_to_read`.
7. **ALLOWED_DATABASES allowlist** — configurable per-deployment; enforced on every schema and sample operation.

## Testing

### Unit tests (no live stack required)

```bash
.venv/bin/python -m pytest tests/ -q
```

Runs 258 unit tests that mock ClickHouse. Integration tests are automatically skipped (shown as "s" in output).

### Integration tests (requires running docker-compose stack)

Integration tests exercise both the live REST API (port 18080) and the live MCP server (port 18090) against a real ClickHouse instance. They seed their own deterministic dataset (`it_integration` database) and tear it down after the session.

**1. Start the stack:**

```bash
docker compose up -d --build
# Wait until both services are healthy (usually ~10 seconds)
```

**2. Run the integration suite:**

```bash
RUN_INTEGRATION=1 .venv/bin/python -m pytest tests/integration -v
```

**Or use the helper script** (brings stack up, waits for /health, runs tests):

```bash
bash scripts/run-integration.sh
```

**Gate behaviour:** Without `RUN_INTEGRATION=1`, integration tests are always skipped — a plain `pytest tests/` always runs only the 258 unit tests and stays green regardless of whether the stack is running.

**Configurable endpoints** (override with env vars; defaults match docker-compose.yml):

| Variable | Default |
|---|---|
| `IT_API_BASE` | `http://localhost:18080` |
| `IT_MCP_URL` | `http://localhost:18090/mcp` |
| `IT_API_KEY` | `test-key-abc123` |
| `IT_CH_HOST` | `localhost` |
| `IT_CH_PORT` | `8123` |
