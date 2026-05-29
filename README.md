# ClickHouse Query API

A read-only REST API that sits in front of ClickHouse and is designed to be consumed by a **ChatGPT Custom GPT Action**. The LLM calls this API to discover schema and execute SQL without ever touching ClickHouse directly.

## Architecture

```
ChatGPT GPT Action
      │  Bearer token + SQL
      ▼
ClickHouse Query API  (this service)
  • SQL guardrails (allowlist, denylist, LIMIT injection)
  • Read-only ClickHouse session settings (readonly=1, execution caps)
  • Bearer auth (constant-time comparison)
      │
      ▼
ClickHouse (your cluster)
```

## Endpoints

| Method | Path | operationId | Purpose |
|--------|------|-------------|---------|
| GET | `/databases` | `listDatabases` | List accessible databases |
| GET | `/tables?database=` | `listTables` | List tables in a database |
| GET | `/tables/schema?database=&table=` | `getTableSchema` | Column names, types, comments |
| GET | `/tables/sample?database=&table=&limit=` | `sampleRows` | Preview a few rows |
| POST | `/query` | `runQuery` | Execute a read-only SQL query |
| POST | `/query/explain` | `explainQuery` | EXPLAIN a query (validate without executing) |
| GET | `/health` | `healthCheck` | Liveness/readiness probe |

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
| `API_KEY` | Yes | — | Secret | Bearer token for API authentication |
| `MAX_EXECUTION_TIME` | No | `30` | ConfigMap | Max query wall-clock seconds |
| `MAX_RESULT_ROWS` | No | `10000` | ConfigMap | ClickHouse-side hard row cap |
| `MAX_ROWS_TO_READ` | No | `100000000` | ConfigMap | ClickHouse-side max rows scanned |
| `DEFAULT_LIMIT` | No | `1000` | ConfigMap | Injected LIMIT when query has none |
| `MAX_RESPONSE_ROWS` | No | `1000` | ConfigMap | Max rows in API JSON response |
| `ALLOWED_DATABASES` | No | `*` | ConfigMap | Comma-separated DB allowlist; `*` = all |
| `APP_PORT` | No | `8000` | ConfigMap | Port uvicorn listens on |
| `LOG_LEVEL` | No | `INFO` | ConfigMap | Python logging level |
| `PUBLIC_BASE_URL` | No | placeholder | ConfigMap | HTTPS base URL for OpenAPI servers block |

## Running Locally

### Prerequisites

- Python 3.11+
- A reachable ClickHouse instance

### Setup

```bash
# 1. Clone and enter the repo
cd clickhouse-api

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your ClickHouse credentials and API_KEY

# 5. Start the server
uvicorn app.main:app --reload --port 8000
```

The interactive docs are available at http://localhost:8000/docs.

The raw OpenAPI schema (for GPT Action import) is at http://localhost:8000/openapi.json.

## Docker

### Build

```bash
docker build -t clickhouse-api:latest .
```

### Run

```bash
docker run --rm -p 8000:8000 \
  --env-file .env \
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

### Install

```bash
helm install clickhouse-api ./helm/clickhouse-api \
  --namespace clickhouse-api \
  --create-namespace \
  --set secrets.connection.CLICKHOUSE_HOST=clickhouse.prod.svc.cluster.local \
  --set config.PUBLIC_BASE_URL=https://clickhouse-api.example.com \
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

Create a Secret containing **all seven** canonical keys, then reference it:

```yaml
# The Secret must contain exactly these keys:
# CLICKHOUSE_HOST, CLICKHOUSE_PORT, CLICKHOUSE_USER, CLICKHOUSE_DATABASE,
# CLICKHOUSE_SECURE, CLICKHOUSE_PASSWORD, API_KEY
```

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

1. **Deploy the API** to a public HTTPS endpoint. Self-signed certificates will be rejected by ChatGPT — use a certificate from a trusted CA (Let's Encrypt is free).

2. **Set `PUBLIC_BASE_URL`** to your public HTTPS URL (e.g. `https://clickhouse-api.example.com`). This value is embedded in the OpenAPI `servers` block that ChatGPT reads.

3. **In the ChatGPT editor**:
   - Create a new Custom GPT or edit an existing one.
   - Go to **Configure → Actions → Add Action**.
   - Click **Import from URL** and enter:
     ```
     https://your-public-host.example.com/openapi.json
     ```
   - ChatGPT will parse all endpoints, their descriptions, and parameters.

4. **Set authentication**:
   - Authentication type: **API Key**
   - Auth type: **Bearer**
   - API Key value: the value you set in `API_KEY`

5. **Test** by asking the GPT: *"What databases are available?"* or *"Describe the schema of table X in database Y."*

### Recommended GPT System Prompt additions

```
You have access to a ClickHouse analytics database via the ClickHouse Query API.
Workflow:
1. Call listDatabases to see available databases.
2. Call listTables to find relevant tables.
3. ALWAYS call getTableSchema before writing a SELECT query — this prevents column name errors.
4. Optionally call sampleRows to understand data formats.
5. Call explainQuery to validate complex queries before running them.
6. Call runQuery to execute the final query.

If a query returns truncated=true, the result was capped. Rewrite with a narrower
WHERE clause or a more specific LIMIT.
```

## SQL Security Model

The API enforces defense-in-depth against LLM-generated SQL:

1. **Single statement only** — semicolons in the middle of a query are rejected.
2. **Allowlist** — only `SELECT`, `WITH`, `EXPLAIN`, `SHOW`, `DESCRIBE`, `DESC` are accepted.
3. **Denylist** — word-boundary regex blocks `INSERT`, `ALTER`, `DROP`, `CREATE`, `TRUNCATE`, `RENAME`, `ATTACH`, `DETACH`, `OPTIMIZE`, `GRANT`, `REVOKE`, `SET`, `KILL`, `SYSTEM`, `DELETE`, `UPDATE`, `INTO OUTFILE`, `FORMAT`.
4. **Auto-LIMIT** — queries without a LIMIT clause get one injected automatically.
5. **ClickHouse session settings** — every query runs with `readonly=1`, `max_execution_time`, `max_result_rows`, `result_overflow_mode='throw'`, `max_rows_to_read`.
