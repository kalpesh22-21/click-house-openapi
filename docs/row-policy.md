# Employee row-level security — DBA migration

The app (`app/employee_access.py`) populates `security.user_employee_access` and
relies on a **DBA-owned** table + row policy. The app issues **no DDL**; if the
table is missing it fails closed (`EMPLOYEE_ACCESS_NOT_PROVISIONED`). Apply the
following once before enabling `EMPLOYEE_ACCESS_ENABLED`.

## 1. Table (DBA-owned)

```sql
CREATE DATABASE IF NOT EXISTS security;

CREATE TABLE security.user_employee_access
(
    JTI          String,
    EmployeeCode String,
    pull_id      UInt64,
    UpdatedAt    DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(pull_id)
ORDER BY (JTI, EmployeeCode)
TTL UpdatedAt + INTERVAL 1 DAY;   -- housekeeping only; MUST exceed N (EMPLOYEE_ACCESS_REFRESH_SECONDS)
```

Correctness comes from the `pull_id = max(pull_id)` gate below, not the TTL —
the TTL only reclaims space (revoked codes / departed users). It must comfortably
exceed `N` so an active user's rows are never evicted between refreshes.

## 2. Row policy (DBA-owned)

```sql
CREATE ROW POLICY employee_rls ON dbpcm_warehouse.employee
USING
        ClientCode  = getSetting('SQL_CLIENTCODE')
    AND ProcCenter  = getSetting('SQL_PROCCENTER')
    AND EmployeeCode IN (
        SELECT EmployeeCode FROM security.user_employee_access
        WHERE JTI = getSetting('SQL_TENANT')
          AND pull_id = (SELECT max(pull_id) FROM security.user_employee_access
                         WHERE JTI = getSetting('SQL_TENANT'))
    )
TO mcp_user;
```

`SQL_TENANT` / `SQL_CLIENTCODE` / `SQL_PROCCENTER` are injected per request from
the verified JWT via `CLICKHOUSE_TENANT_SETTINGS` (see below); the LLM cannot
override them (`app/security.py` blocks any user `SETTINGS` clause).

## 3. Grants

```sql
-- Security-writer account (SECURITY_CH_USER): the ONLY account the app uses to
-- touch security.* — for both the freshness read and the pull insert. No DDL
-- (table is DBA-owned), no DELETE/ALTER (append-only + TTL eviction).
GRANT INSERT, SELECT ON security.user_employee_access TO security_writer;

-- Query account (mcp_user): needs SELECT on the lookup table because ClickHouse
-- evaluates the row-policy subquery in the QUERYING user's context. This grant
-- is for the DB engine's policy evaluation only — the app never issues an
-- mcp_user query against security.* (keep `security` OUT of ALLOWED_DATABASES so
-- the LLM can never read it directly).
GRANT SELECT ON security.user_employee_access TO mcp_user;
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
CLICKHOUSE_TENANT_SETTINGS={"SQL_tenant":"jti","SQL_CLIENTCODE":"clientcode","SQL_PROCCENTER":"proc_center"}

# security must NOT be selectable by the LLM directly
ALLOWED_DATABASES=dbpcm_warehouse,scratch     # (whatever the real allowlist is — just exclude `security`)
```

## Notes

- Single ClickHouse node assumed (insert is synchronously visible to the next
  read; the single-insert pull is an atomic pointer advance).
- `/cl/eeaccess` must accept the **same token audience** this MCP server does,
  since the caller's JWT is forwarded verbatim.
