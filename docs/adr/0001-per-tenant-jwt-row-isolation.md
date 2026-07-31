# ADR-0001: Per-Tenant JWT Authentication and ClickHouse Row-Level Isolation

**Status:** Accepted  
**Date:** 2026-06-09  
**Deciders:** Kalpesh Mulye  

---

## Context

The ClickHouse REST+MCP API is used by multiple tenants. Currently all
authenticated callers share a single API key and see the same data. We need
row-level data isolation without changing the ClickHouse service account or
introducing per-tenant database users.

---

## Decision

1. **Identity source:** External JWT / OIDC. Clients present a signed JWT;
   the app validates the signature, iss, aud, exp, nbf, and alg pinning
   (reject `none` and RS/HS confusion attacks).

2. **Tenant identity:** A custom JWT claim named `user_name` (configurable
   via the `CLICKHOUSE_TENANT_SETTINGS` env-object, see below).

3. **Enforcement model:** App-enforced via a trusted ClickHouse custom setting.
   The app injects `SQL_tenant=<claim value>` into every query's `settings=`
   dict. ClickHouse row policies on each tenant table read
   `getSetting('SQL_tenant')` to filter rows. The JWT validation step is
   therefore the entire isolation boundary from the application side.

4. **Single service account retained:** The process-wide singleton
   clickhouse-connect client is kept as-is (thread-safe via
   `autogenerate_session_id=False`). No per-tenant ClickHouse credentials.

5. **Env-object mapping:** `CLICKHOUSE_TENANT_SETTINGS` is a JSON object
   mapping ClickHouse custom-setting-name → JWT-claim-name. Default:
   `{"SQL_tenant": "user_name"}`. All mapped keys must be prefixed `SQL_`
   and must not shadow any safety setting.

6. **Safety caps always win:** Merge order is
   `{**tenant_settings, **readonly_settings(settings)}` — safety settings
   are applied last and cannot be overridden by tenant settings.

7. **`readonly=1` + custom setting (resolved):** A live Docker spike showed the
   blocker was NOT a server-side `readonly` conflict but **clickhouse-connect's
   client-side setting validation**: the driver refuses to transmit any setting
   not in `system.settings` ("Setting SQL_tenant is unknown or readonly"), and
   custom settings never appear there. Fix: set
   `clickhouse_connect.common.set_setting('invalid_setting_action', 'send')` in
   `app/clickhouse_client.py` so the server (not the driver) validates settings.
   With that, **`readonly=1` works**. `CLICKHOUSE_READONLY` stays configurable
   (default 1) for unusual setups. Deploy prerequisite remains:
   `<custom_settings_prefixes>SQL_</custom_settings_prefixes>` on the server.

8. **Query/schema clients migrate to JWTs:** The per-tenant read-only routes
   (`/query`, `/query/explain`, `/databases`, `/tables`, schema) and the MCP
   HTTP transport authenticate with OIDC JWTs only. Existing REST and
   GPT-Action clients must migrate. MCP stdio keeps its no-auth local-trust
   model unchanged.

9. **`/admin` static API key — SUPERSEDED (2026-07-30):** The original decision
   kept the `/admin` CSV-ingest **write** routes on a single shared Bearer API
   key (`API_KEY`) separate from the per-tenant JWTs. That entire write surface
   (the `/admin` routes, the `/ui` static panel, `require_admin_api_key`, and the
   `api_key` setting) has since been **removed from the codebase**. All caller
   auth is now per-tenant OIDC/JWT only; there is no static-key path. The
   hardened ingest primitives (`validate_identifier`, `validate_ch_type`,
   `coerce`, DDL builders) live on in `app/ingest_primitives.py`, reused by the
   MCP scratch/upload write plane.

---

## Consequences

**Positive:**
- Row isolation is enforced at the ClickHouse engine level, not by SQL
  string manipulation. A compromised app cannot circumvent a correctly
  written row policy.
- No per-tenant credentials to rotate; isolation is controlled by row
  policies in ClickHouse config.
- The env-object pattern allows adding additional per-tenant settings (e.g.
  `SQL_region`) without code changes.

**Negative / risks:**
- JWT validation is the entire app-side isolation boundary. A misconfigured
  JWKS URL, wrong audience, or forgotten alg-pinning breaks isolation
  globally, not just for one tenant.
- Every tenant table must have a DEFAULT-DENY row policy. A table without a
  policy leaks all rows. This is a deploy-time operational requirement, not
  a code guarantee.
- The `readonly` / custom-setting interaction requires a spike before
  production deploy (see spec section 5).
- stdio MCP transport remains unauthenticated (local-trust posture); it must
  never be exposed over the network.

---

## Alternatives Considered

**Per-tenant ClickHouse users:** Rejected. Requires credential management
infrastructure, per-user connection pools, and breaks the singleton client
thread-safety invariant.

**SQL WHERE clause injection:** Rejected. Concatenating tenant filters into
SQL strings is brittle, can be subverted by query shapes the injector does
not anticipate, and conflicts with `validate_and_sanitize()`.

**OAuth opaque tokens / API key per tenant:** Rejected. A long-lived secret
per tenant is operationally heavier than a short-lived JWT and provides no
cryptographic identity binding.
