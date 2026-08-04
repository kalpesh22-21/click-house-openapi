# Employee row-level security — DBA migration

The app (`app/employee_access.py`) populates `security.user_employee_access` and
relies on a **DBA-owned** table + row policy. The app issues **no DDL**; if the
table is missing it fails closed (`EMPLOYEE_ACCESS_NOT_PROVISIONED`). Apply the
following once before enabling `EMPLOYEE_ACCESS_ENABLED`.

## 1. Table (DBA-owned)

```sql
CREATE DATABASE IF NOT EXISTS dbpcm_warehouse_security;

CREATE TABLE dbpcm_warehouse_security.user_employee_access
(
    jti           String,
    employee_code String,
    pull_id       UInt64,
    updated_at    DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(pull_id)
ORDER BY (jti, employee_code)
TTL updated_at + INTERVAL 1 DAY;   -- housekeeping only; MUST exceed N (EMPLOYEE_ACCESS_REFRESH_SECONDS)
```

Correctness comes from the `pull_id = max(pull_id)` gate below, not the TTL —
the TTL only reclaims space (revoked codes / departed users). It must comfortably
exceed `N` so an active user's rows are never evicted between refreshes.

## 2. Row policy (DBA-owned)

```sql
CREATE ROW POLICY employee_rls ON dbpcm_warehouse.employee
USING
        client_code = getSetting('paycom_client_code')
    AND proc_center = getSetting('paycom_proc_center')
    AND employee_code IN (
        SELECT employee_code FROM dbpcm_warehouse_security.user_employee_access
        WHERE jti = getSetting('paycom_authenticated_user')
          AND pull_id = (SELECT max(pull_id) FROM dbpcm_warehouse_security.user_employee_access
                         WHERE jti = getSetting('paycom_authenticated_user'))
    )
TO mcp_user;
```

`paycom_client_code` / `paycom_proc_center` / `paycom_authenticated_user` are
injected per request from the verified JWT via `CLICKHOUSE_TENANT_SETTINGS` (see
below); the LLM cannot override them (`app/security.py` blocks any user
`SETTINGS` clause).

## 3. Grants

```sql
-- Security-writer account (SECURITY_CH_USER): the ONLY account the app uses to
-- touch dbpcm_warehouse_security.* — for both the freshness read and the pull
-- insert. No DDL (table is DBA-owned), no DELETE/ALTER (append-only + TTL eviction).
GRANT INSERT, SELECT ON dbpcm_warehouse_security.user_employee_access TO security_writer;

-- Query account (mcp_user): needs SELECT on the lookup table because ClickHouse
-- evaluates the row-policy subquery in the QUERYING user's context. This grant
-- is for the DB engine's policy evaluation only — the app never issues an
-- mcp_user query against dbpcm_warehouse_security.* (keep it OUT of
-- ALLOWED_DATABASES so the LLM can never read it directly).
GRANT SELECT ON dbpcm_warehouse_security.user_employee_access TO mcp_user;
```

## 4. App configuration

```bash
EMPLOYEE_ACCESS_ENABLED=true
EMPLOYEE_ACCESS_REFRESH_SECONDS=600          # N: refresh window / revocation latency / Redis marker TTL
EEACCESS_BASE_URL=https://<access-api-host>  # {system}/eeaccess is appended; caller's JWT forwarded as Bearer

# Security-writer ClickHouse credential (distinct from the main query account)
SECURITY_CH_USER=security_writer
SECURITY_CH_PASSWORD=<secret>
# SECURITY_CH_HOST/PORT/SECURE fall back to the main CLICKHOUSE_* connection if unset.

# Redis freshness cache (optional; empty => ClickHouse read only, still correct)
REDIS_URL=redis://<host>:6379/0

# Tenant settings the row policy reads (must expose all three)
CLICKHOUSE_TENANT_SETTINGS={"paycom_client_code":"clientcode","paycom_proc_center":"proc_center","paycom_authenticated_user":"jti"}

# dbpcm_warehouse_security must NOT be selectable by the LLM directly
ALLOWED_DATABASES=dbpcm_warehouse,scratch     # (whatever the real allowlist is — just exclude `dbpcm_warehouse_security`)
```

## Notes

- Single ClickHouse node assumed (insert is synchronously visible to the next
  read; the single-insert pull is an atomic pointer advance).
- `/cl/eeaccess` must accept the **same token audience** this MCP server does,
  since the caller's JWT is forwarded verbatim.
