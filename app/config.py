"""Application configuration loaded from environment variables via pydantic-settings."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated, Dict, List, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# paycompy is an internal package, only needed when VAULT_ENABLED=true.  Guard the
# import so the app boots without it (local/dev, or any environment using plain env
# vars instead of Vault).  load_settings_from_vault() raises clearly if Vault is
# enabled but paycompy is missing.
try:
    from paycompy import vault  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only where paycompy is absent
    vault = None

logger = logging.getLogger(__name__)


def _normalise_public_key(v: str) -> str:
    """Normalise and validate a static OIDC public key (shared by the field
    validator and the Vault loader).

    Accepts escaped ``\\n`` (env/secret stores often flatten PEMs to one line).
    Rejects anything that is not a loadable PEM *public* key — including a pasted
    private key — so a misconfiguration cannot silently disable auth.  Empty input
    normalises to ``""`` (no static key configured).
    """
    if not v or not v.strip():
        return ""
    pem = v.replace("\\n", "\n").strip()
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        load_pem_public_key(pem.encode())
    except Exception as exc:  # noqa: BLE001 — surface any parse failure as config error
        raise ValueError(
            f"oidc_public_key must be a valid PEM-encoded public key: {exc}"
        ) from exc
    return pem

# ClickHouse session-setting keys that the server controls for safety.  A
# per-tenant custom setting (see clickhouse_tenant_settings) must never shadow
# one of these, or a misconfigured env object could loosen the read-only caps.
_RESERVED_SETTING_KEYS = frozenset(
    {
        "readonly",
        "max_execution_time",
        "max_result_rows",
        "max_rows_to_read",
        "result_overflow_mode",
    }
)


class Settings(BaseSettings):
    """All configuration is read from environment variables (or a .env file).

    Every field maps 1-to-1 with an environment variable of the same name
    (pydantic-settings uppercases automatically when case_sensitive=False).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- ClickHouse connection ---
    # host/user/password default empty so a Vault-backed deploy can construct
    # Settings() without them in the environment and have load_settings_from_vault()
    # populate them afterwards.  When Vault is OFF, _validate_clickhouse_connection
    # re-imposes the old "host and user are mandatory" guarantee (fail fast, loud).
    # A password may legitimately be empty (passwordless server), so it is never
    # required.  All three are secret and belong in Vault (CLICKHOUSE_CREDS_PATH).
    clickhouse_host: str = Field(
        "", description="ClickHouse server hostname or IP (secret; from Vault when enabled)."
    )
    clickhouse_port: int = Field(8443, description="ClickHouse HTTP(S) port")
    clickhouse_user: str = Field(
        "", description="ClickHouse username (secret; from Vault when enabled)."
    )
    clickhouse_password: str = Field(
        "", description="ClickHouse password (secret; from Vault when enabled)."
    )
    clickhouse_database: str = Field("default", description="Default database")
    clickhouse_secure: bool = Field(True, description="Use TLS for ClickHouse connection")

    # --- API authentication (external JWT / OIDC) ---
    # Callers present a signed JWT as a Bearer token.  We always verify the
    # signature (against the IdP's JWKS or a static public key) and expiry, then
    # derive the tenant identity from a claim (see clickhouse_tenant_settings).
    # Only a KEY SOURCE (OIDC_JWKS_URL or OIDC_PUBLIC_KEY) is required for the
    # REST API and MCP HTTP transport — the REST app lifespan and MCP _run_http
    # fail closed (see auth_configured) if none is set.  OIDC_ISSUER / OIDC_AUDIENCE
    # are OPTIONAL: verified when set, skipped when empty (for a minter that stamps
    # neither).  The MCP stdio transport (local trust model) performs no auth.
    oidc_jwks_url: str = Field(
        "",
        description="JWKS endpoint (e.g. https://idp/.well-known/jwks.json) used to verify JWT signatures.",
    )
    oidc_issuer: str = Field(
        "",
        description=(
            "Expected JWT 'iss' claim (token issuer). Optional: verified when set, "
            "issuer check skipped when empty."
        ),
    )
    oidc_audience: str = Field(
        "",
        description=(
            "Expected JWT 'aud' claim (this API's audience). Optional: verified when "
            "set, audience check skipped when empty."
        ),
    )
    # --- Static public key (JWKS-less verification) ---
    # For an external token creator that mints RS*/ES*/PS* JWTs but does NOT
    # publish a JWKS endpoint — you only hold its public key.  When set, the
    # asymmetric path verifies against THIS key directly (no network) and takes
    # precedence over OIDC_JWKS_URL.  Only ever used on the asymmetric path
    # (pinned to JWT_ALGORITHMS), so it never touches HMAC verification.  A single
    # key only: rotation needs a JWKS endpoint (multiple published keys by kid).
    # Escaped '\n' sequences (common in env/secret stores) are normalised.
    oidc_public_key: str = Field(
        "",
        description=(
            "Optional PEM-encoded public key to verify asymmetric JWTs without a "
            "JWKS endpoint. Takes precedence over OIDC_JWKS_URL when set."
        ),
    )
    jwt_algorithms: str = Field(
        "RS256",
        description=(
            "Comma-separated allowlist of accepted JWT signing algorithms. "
            "Pinned to asymmetric algs (RS*/ES*/PS*); 'none' and HMAC (HS*) are rejected."
        ),
    )
    # --- Query safety limits ---
    max_execution_time: int = Field(30, description="Max query wall-clock time in seconds")
    max_result_rows: int = Field(10_000, description="ClickHouse-side hard cap on result rows")
    max_rows_to_read: int = Field(100_000_000, description="ClickHouse-side max rows scanned")
    default_limit: int = Field(1_000, description="LIMIT injected when query has none")
    max_response_rows: int = Field(1_000, description="Max rows returned in API response")
    # readonly mode applied on every query.  1 = no writes AND no settings changes
    # (the safe default).  GOTCHA: ClickHouse may refuse a custom per-tenant
    # setting alongside readonly=1 ("Cannot modify setting in readonly mode").  If
    # the deploy spike shows that, set CLICKHOUSE_READONLY=2 (no writes, but
    # settings changes allowed) — the safety caps below are still enforced because
    # we always send them ourselves.  Only 1 or 2 are accepted.
    clickhouse_readonly: int = Field(
        1, ge=1, le=2, description="ClickHouse readonly level applied to every query (1 or 2)."
    )
    # When true, sets the ClickHouse `final=1` session setting on every query so
    # ReplacingMergeTree/CollapsingMergeTree tables return their collapsed (latest)
    # rows instead of un-merged duplicates.  GOTCHA: this is query-GLOBAL — it
    # applies FINAL to every MergeTree-family table in a query, not just Replacing
    # ones, so plain MergeTree tables pay the merge-at-read overhead too and large
    # full scans may newly hit max_execution_time.  No-ops on system tables.
    # Default ON: correctness-first — an LLM caller should never see un-merged
    # duplicate rows from a Replacing/Collapsing table.  Set CLICKHOUSE_SELECT_FINAL
    # =false on a deployment whose warehouse has no Replacing/Collapsing tables and
    # wants to shed the merge-at-read cost.
    clickhouse_select_final: bool = Field(
        True,
        description="Apply ClickHouse final=1 to every query (dedup ReplacingMergeTree reads).",
    )

    # --- Per-tenant row isolation (the "env object") ---
    # JSON object mapping a ClickHouse custom-setting name -> the JWT claim that
    # fills it.  Each authenticated query injects settings[ch_setting] = jwt[claim]
    # via the trusted settings= channel (never concatenated into SQL).  ClickHouse
    # row policies read e.g. getSetting('paycom_client_code') to filter rows per
    # tenant.  The default maps the three settings the warehouse row policies read:
    #   paycom_client_code       <- 'clientcode' claim   (caller's client code)
    #   paycom_proc_center       <- 'proc_center' claim   (caller's processing center)
    #   paycom_authenticated_user <- 'jti' claim          (stable per-user id)
    clickhouse_tenant_settings: Dict[str, str] = Field(
        default_factory=lambda: {
            "paycom_client_code": "clientcode",
            "paycom_proc_center": "proc_center",
            "paycom_authenticated_user": "jti",
        },
        description=(
            "JSON object mapping ClickHouse custom-setting name to the JWT claim "
            "that fills it. Keys must start with CLICKHOUSE_CUSTOM_SETTINGS_PREFIX "
            "and must not shadow a reserved safety setting."
        ),
    )
    clickhouse_custom_settings_prefix: str = Field(
        "paycom_",
        description=(
            "Required prefix for tenant custom-setting keys. Must match the "
            "ClickHouse server's <custom_settings_prefixes> configuration."
        ),
    )
    # --- RLS identity transport (CHProxy) ---
    # How the per-tenant RLS identity settings (paycom_client_code / proc_center /
    # authenticated_user) reach ClickHouse.  clickhouse-connect's settings= channel
    # is sent as HTTP query parameters, which CHProxy (the multi-node cluster
    # gateway) STRIPS — so on a CHProxy deployment the getSetting(...) row policies
    # would see empty values and return no rows.  When true (the default), the
    # identity values ride the SQL body's SETTINGS clause as typed, CHProxy-preserved
    # param_* binds instead (see app/clickhouse_client._append_tenant_settings_clause).
    # Set false to revert to the legacy transport — sending them via settings=
    # alongside the safety caps — for a direct (non-CHProxy) ClickHouse endpoint.
    # Only the CUSTOM paycom_* settings are affected; the safety caps always ride
    # settings=.
    clickhouse_rls_via_sql_settings: bool = Field(
        True,
        description=(
            "Send the per-tenant RLS identity settings in the SQL body's SETTINGS "
            "clause (as CHProxy-preserved param_* binds) instead of the settings= "
            "HTTP channel that CHProxy strips. Default true (CHProxy-safe); set "
            "false for the legacy settings= transport on a direct ClickHouse endpoint."
        ),
    )

    # --- Schema filtering ---
    allowed_databases: str = Field(
        "*",
        description=(
            "Comma-separated allowlist of databases the API may access. "
            "'*' permits all databases."
        ),
    )
    restrict_to_catalogued_tables: bool = Field(
        False,
        description=(
            "When true, only tables present in the semantic catalog (the "
            "'schema dictionary' under app/semantic_catalog/data/*.yaml) may be "
            "listed, described, or sampled. Uncatalogued tables are hidden from "
            "listTables and rejected by getTableSchema / sampleRows with "
            "TABLE_NOT_ALLOWED. This is a tighter filter than ALLOWED_DATABASES "
            "(a whole-database allowlist); the two compose (a table must satisfy "
            "both). The scratch database is exempt — its tables are never "
            "catalogued and remain governed by per-session isolation. Defaults to "
            "false to preserve existing behaviour."
        ),
    )

    # --- Server ---
    app_port: int = Field(8000, description="Port uvicorn listens on")
    log_level: str = Field("INFO", description="Python logging level")

    # --- MCP ---
    mode: str = Field(
        "api",
        description=(
            "Server mode: 'api' starts the REST/FastAPI server (default), "
            "'mcp' starts the MCP server."
        ),
    )
    mcp_transport: str = Field(
        "stdio",
        description=(
            "MCP transport: 'stdio' for local desktop clients (no auth required), "
            "'http' for streamable-HTTP over the network (Bearer auth enforced)."
        ),
    )
    mcp_port: int = Field(8000, description="Port the MCP HTTP server listens on (default 8000)")
    mcp_path: str = Field("/mcp", description="Mount path for the MCP streamable-HTTP endpoint")

    # --- X-Session-Id ↔ JWT binding (auth-hardening Slice 1) ---
    # When true, any MCP HTTP request that carries an X-Session-Id header MUST
    # also carry a matching 'sid_hash' JWT claim (base64url(sha256(session_id)));
    # a mismatch or a missing claim is rejected 403 SESSION_BINDING_MISMATCH,
    # before the request reaches the scratch-isolation extractor.  This closes
    # the session-hijack gap (a caller sending another user's session id).
    # Defaults FALSE: the external token minter does not stamp a 'sid_hash'
    # claim, so enforcing the binding would 403 every session-carrying request.
    # In this posture the session id is taken purely from the X-Session-Id header
    # and the caller (a trusted agent backend) is relied on not to forward a
    # hostile session id.  Set true once every minting path stamps sid_hash
    # (base64url(sha256(session_id)); see app/session_binding.py) to re-enable the
    # session-hijack protection.  Session-less requests (no X-Session-Id header)
    # are unaffected regardless of this flag.
    require_sid_binding: bool = Field(
        False,
        description=(
            "Require a matching 'sid_hash' JWT claim whenever an X-Session-Id "
            "header is present (session-hijack protection). Default false because "
            "the external minter does not stamp sid_hash; set true once it does."
        ),
    )

    # --- Static service API key for the MCP export side-channels ---
    # A shared secret that lets a trusted system hydrator fetch the scope-
    # independent export routes (/catalog/export, /blueprints/export,
    # /knowledge/export) WITHOUT a user JWT, via the X-Service-Key header. It is
    # an EITHER-OR alternative to a Bearer JWT and applies ONLY to those export
    # paths (see JWTAuthMiddleware). Empty (the default) means the feature is OFF
    # and FAIL-CLOSED: an empty configured key must NEVER match, so a missing or
    # empty X-Service-Key can never authenticate. Secret; belongs in Vault
    # (MCP_SERVICE_KEY_VAULT_PATH) when enabled.
    mcp_service_key: str = Field(
        "",
        description=(
            "Static shared secret accepted on the MCP export routes via the "
            "X-Service-Key header (constant-time compared), as an EITHER-OR "
            "alternative to a user JWT. Export-paths only. Empty → feature OFF "
            "and fail-closed (an empty key never matches). Secret; from Vault "
            "when enabled."
        ),
    )

    # --- Scratch-reference fail-closed gate (ADR-0002, H3) ---
    # When true, ANY query that references the scratch database is fail-closed
    # unless it belongs to the current session — enforced on ALL transports incl.
    # REST/stdio, NOT just the scoped MCP path. This closes H3: on REST
    # current_scope is None, so the legacy `scope is not None` trigger skipped
    # scratch enforcement and a REST caller could read another session's scratch
    # table. With this flag on, a scratch-referencing query triggers provenance
    # extraction regardless of scope; a None/mismatched session then fails closed
    # via _validate_scratch_name (SCRATCH_SESSION_VIOLATION). A normal warehouse
    # query (no scratch reference) is unaffected — provenance is NOT run, so the
    # GPT Action's ordinary queries keep working with no PARSE_FAILED_CLOSED
    # regression. Default True; off-switch for a staged rollout — when False,
    # behavior reverts to the legacy `scope is not None` trigger exactly.
    require_session_scratch_gate: bool = Field(
        True,
        description=(
            "When true, any query that references the scratch database is "
            "fail-closed unless it belongs to the current session — enforced on "
            "ALL transports incl. REST/stdio. Off-switch for staged rollout."
        ),
    )

    # --- Scratch-write side-channel (table-intermediate Slice 1) ---
    # The scratch-write endpoints (/scratch/v1/materialize, /scratch/v1/drop) run
    # under a DISTINCT, server-side-only ClickHouse credential that is GRANTed
    # CREATE/INSERT/DROP/SELECT on the `scratch` database ONLY — never the
    # warehouse.  The runtime never holds this credential; the privilege lives in
    # MCP deploy config.  When these SCRATCH_CH_* fields are left unset they fall
    # back to the main CLICKHOUSE_* connection (dev/single-user convenience); a
    # production deploy MUST set SCRATCH_CH_USER / SCRATCH_CH_PASSWORD to the
    # scratch-only account so the grant confines the blast radius (invariant #1).
    scratch_ch_host: str = Field(
        "", description="Scratch-credential ClickHouse host (falls back to CLICKHOUSE_HOST)."
    )
    scratch_ch_port: int = Field(
        0, description="Scratch-credential ClickHouse port (0 → falls back to CLICKHOUSE_PORT)."
    )
    scratch_ch_user: str = Field(
        "", description="Scratch-only ClickHouse username (falls back to CLICKHOUSE_USER)."
    )
    scratch_ch_password: Optional[str] = Field(
        None,
        description=(
            "Scratch-only ClickHouse password (secret). None → falls back to "
            "CLICKHOUSE_PASSWORD. Set explicitly (even to empty) to override."
        ),
    )
    scratch_ch_secure: Optional[bool] = Field(
        None, description="TLS for the scratch credential (None → falls back to CLICKHOUSE_SECURE)."
    )
    scratch_database: str = Field(
        "scratch",
        description="ClickHouse database that holds session-scoped scratch tables (never the warehouse).",
    )
    scratch_ttl_seconds: int = Field(
        3600,
        ge=1,
        description=(
            "Wall-clock TTL (seconds) stamped on every scratch table so orphans from "
            "a crash/abandoned pause GC themselves (D20/D45). Must exceed a human "
            "approval-pause window (OQ-E)."
        ),
    )
    scratch_max_rows: int = Field(
        10_000,
        ge=1,
        description=(
            "Hard cap on rows a single /scratch/v1/materialize may load. Over-cap is "
            "rejected SCRATCH_TOO_LARGE so the runtime falls back to the raw loop (OQ-C)."
        ),
    )
    # --- Multi-node / cluster support for the scratch side-channel ---
    # The scratch table is CREATEd + INSERTed by one client, then JOINed by the
    # main read client. On a single endpoint (or a load balancer with session
    # affinity) both hit the same node, so a plain, node-local `MergeTree` scratch
    # table is fine — the default.  On a genuine multi-node cluster reached through
    # a non-sticky load balancer the create/insert and the JOIN can land on
    # DIFFERENT nodes → the JOIN sees UNKNOWN_TABLE.  Set SCRATCH_CLUSTER to a
    # ClickHouse cluster name to make the DDL cluster-aware: the table is created
    # `ON CLUSTER` as a `ReplicatedMergeTree` so it exists AND its rows replicate
    # to every node, and the JOIN resolves on whichever node serves it.  Empty
    # (default) preserves the node-local single-node behaviour unchanged.
    #
    # READ-YOUR-WRITES on a cluster: cluster mode inserts with insert_quorum='auto'
    # so the write is durable on a majority before materialize() returns, AND the
    # reader automatically sends select_sequential_consistency=1 (see
    # clickhouse_client.readonly_settings) so the immediately-following JOIN waits
    # for its replica to catch up rather than reading a lagging one.  That read
    # setting is query-global, so a cluster deployment pays a small replication-wait
    # cost on every read; single-node deployments (the default) never do.
    scratch_cluster: str = Field(
        "",
        description=(
            "ClickHouse cluster name for `ON CLUSTER` scratch DDL. Empty (default) "
            "keeps node-local plain-MergeTree scratch tables; set it to make scratch "
            "tables ReplicatedMergeTree ON CLUSTER so multi-node reads resolve them."
        ),
    )
    scratch_replica_path_prefix: str = Field(
        "/clickhouse/tables/{shard}",
        description=(
            "ClickHouse Keeper/ZooKeeper path prefix for ReplicatedMergeTree scratch "
            "tables (cluster mode only). The full path is "
            "'<prefix>/<database>/<table>'. May contain ClickHouse macros such as "
            "{shard}; leave them literal (server-expanded per node)."
        ),
    )
    scratch_replica_name: str = Field(
        "{replica}",
        description=(
            "ClickHouse replica name for ReplicatedMergeTree scratch tables (cluster "
            "mode only). Typically the {replica} macro so each node names itself."
        ),
    )
    upload_max_bytes: int = Field(
        8 * 1024 * 1024,
        ge=1,
        description=(
            "Hard cap (bytes) on an external-dataset upload (/scratch/v1/analyze, "
            "/scratch/v1/upload) enforced BEFORE the file is parsed (UI Slice 4 §3). "
            "Mirrors scratch_max_rows as a session-appropriate front-door guard — far "
            "below admin's 200 MB CSV cap — and bounds a compressed XLSX before it "
            "reaches openpyxl (decompression-bomb defence)."
        ),
    )

    # --- Employee row-level-security (RLS) access provisioning ---
    # Populates dbpcm_warehouse_security.user_employee_access, the table the
    # dbpcm_warehouse.employee row policy reads to scope each tenant (identified by
    # its stable jti) to only the employees it may see.  See app/employee_access.py
    # for the full design (ReplacingMergeTree pull_id gate, staleness-gated refresh,
    # sentinel row, fail-closed-loud).  Disabled by default so existing deploys are
    # unaffected.
    employee_access_enabled: bool = Field(
        False,
        description=(
            "Enable per-request employee-access provisioning for the employee row "
            "policy. When true, EEACCESS_BASE_URL must be set and the tenant-settings "
            "map should expose paycom_client_code/paycom_proc_center/"
            "paycom_authenticated_user."
        ),
    )
    employee_access_refresh_seconds: int = Field(
        600,
        ge=1,
        description=(
            "Staleness threshold N (seconds): a JTI's access set is re-pulled from "
            "/cl/eeaccess only when older than this. Also the revocation latency and "
            "the Redis freshness-marker TTL. Default 600s (10 min)."
        ),
    )
    # NOTE: the row-level TTL on dbpcm_warehouse_security.user_employee_access is
    # defined in the DBA-owned CREATE TABLE (see app/employee_access.py), NOT here —
    # the app never creates that table. Recommended `TTL updated_at + INTERVAL 1 DAY`
    # (must exceed N).
    security_database: str = Field(
        "dbpcm_warehouse_security",
        description="ClickHouse database holding user_employee_access (never the warehouse).",
    )
    # Claim names the jti / clientcode / proc_center are read from (the jti keys the
    # access table and identifies the user to /cl/eeaccess).
    employee_access_jti_claim: str = Field(
        "jti", description="JWT claim carrying the stable per-user id (row-policy key)."
    )
    # External access API (/cl/eeaccess).  The caller's own JWT is forwarded as a
    # Bearer credential (see _fetch_employee_codes), so no service key is needed.
    eeaccess_base_url: str = Field(
        "", description="Base URL of the employee-access API (path {system}/eeaccess is appended)."
    )
    eeaccess_timeout_seconds: float = Field(
        10.0, gt=0, description="Per-request timeout (seconds) for the /cl/eeaccess call."
    )
    # Redis freshness cache in front of the ClickHouse staleness read.  Empty →
    # cache disabled (always use the ClickHouse read).  A down/unreachable Redis
    # degrades to the ClickHouse read — correctness never depends on Redis.
    redis_url: str = Field(
        "",
        description=(
            "Redis URL for the employee-access freshness cache (e.g. "
            "redis://host:6379/0). Empty disables the cache (ClickHouse read only)."
        ),
    )
    redis_socket_timeout_seconds: float = Field(
        0.3,
        gt=0,
        description=(
            "Redis socket/connect timeout (seconds). Kept small so a down Redis "
            "fails fast to the ClickHouse fallback."
        ),
    )
    employee_access_cache_prefix: str = Field(
        "eeaccess:fresh:",
        description="Redis key prefix for the per-JTI freshness markers.",
    )
    employee_access_refresh_backoff_seconds: int = Field(
        30,
        ge=1,
        description=(
            "After a STALE (non-cold) refresh failure, suppress re-pull attempts "
            "for this many seconds (Redis-backed) so a down /cl/eeaccess is not "
            "hammered every request while the existing set is served."
        ),
    )
    # Security-writer ClickHouse credential (mirrors SCRATCH_CH_*): a distinct,
    # server-side-only account GRANTed only INSERT + SELECT on
    # `dbpcm_warehouse_security.user_employee_access` (the table + row policy are DBA-owned; the
    # app never issues DDL, and refresh is append-only with TTL eviction — so no
    # CREATE/DELETE/ALTER is needed).  Unset fields fall back to the main
    # CLICKHOUSE_* connection for dev/single-user convenience; a production deploy
    # MUST set SECURITY_CH_USER / SECURITY_CH_PASSWORD to the security-only account
    # to confine blast radius.
    security_ch_host: str = Field(
        "", description="Security-credential ClickHouse host (falls back to CLICKHOUSE_HOST)."
    )
    security_ch_port: int = Field(
        0, description="Security-credential ClickHouse port (0 → falls back to CLICKHOUSE_PORT)."
    )
    security_ch_user: str = Field(
        "", description="Security-only ClickHouse username (falls back to CLICKHOUSE_USER)."
    )
    security_ch_password: Optional[str] = Field(
        None,
        description=(
            "Security-only ClickHouse password (secret). None → falls back to "
            "CLICKHOUSE_PASSWORD. Set explicitly (even to empty) to override."
        ),
    )
    security_ch_secure: Optional[bool] = Field(
        None, description="TLS for the security credential (None → falls back to CLICKHOUSE_SECURE)."
    )

    # --- OpenAPI / GPT Action ---
    public_base_url: str = Field(
        "https://your-public-host.example.com",
        description="Public HTTPS base URL used in the OpenAPI servers block",
    )

    # --- OAuth 2.0 Protected Resource Metadata (RFC 9728 / MCP Authorization) ---
    # The MCP HTTP server is an OAuth 2.0 *Protected Resource*.  These fields let
    # interactive MCP clients (Claude Desktop, ChatGPT, MCP Inspector, VS Code)
    # discover the authorization server and obtain a token via authorization-code
    # + PKCE, instead of an operator hand-minting a JWT.  Token *validation* is
    # unchanged (see app/auth_jwt.py) — this only advertises *where* to get a
    # token.  The authorization server is an external IdP (see docs/oauth.md).
    oauth_authorization_servers: str = Field(
        "",
        description=(
            "Comma-separated OAuth Authorization Server issuer URL(s) advertised in "
            "Protected Resource Metadata. Each must serve RFC 8414 / OIDC discovery "
            "metadata. Defaults to OIDC_ISSUER when empty."
        ),
    )
    oauth_resource: str = Field(
        "",
        description=(
            "Canonical resource identifier advertised as the Protected Resource "
            "Metadata 'resource' value and used for RFC 8707 audience binding. "
            "MUST equal the audience the IdP stamps as the token 'aud' claim "
            "(see OIDC_AUDIENCE). Defaults to PUBLIC_BASE_URL + MCP_PATH when empty."
        ),
    )
    oauth_scopes_supported: str = Field(
        "",
        description="Optional comma-separated scopes advertised in PRM 'scopes_supported'.",
    )

    # --- HashiCorp Vault (sensitive-value sourcing) ---
    # When VAULT_ENABLED=true, sensitive values (the ClickHouse connection, the
    # scratch/security writer credentials, the Redis URL) are
    # read from Vault at startup and OVERRIDE anything from the environment.  Each
    # *_PATH points at a KV secret; a path left empty skips that group (its env or
    # default value is kept).  See load_settings_from_vault() for the read logic and
    # fail-closed behaviour.  Defaults keep Vault OFF so existing env-based deploys
    # are entirely unaffected.
    vault_enabled: bool = Field(
        False,
        description="Read sensitive values from HashiCorp Vault at startup (overrides env).",
    )
    clickhouse_creds_path: str = Field(
        "",
        description=(
            "Vault KV path holding the ClickHouse connection secret (keys: "
            "CLICKHOUSE_HOST, CLICKHOUSE_PORT, CLICKHOUSE_USER, CLICKHOUSE_PASSWORD). "
            "Empty → use env/defaults."
        ),
    )
    oidc_public_key_vault_path: str = Field(
        "",
        description=(
            "Vault KV path holding the static OIDC public key (key: OIDC_PUBLIC_KEY). "
            "Not confidential (it is a public key) — sourced from Vault only for "
            "central management / tamper-resistance. Empty → use env/default."
        ),
    )
    scratch_ch_creds_path: str = Field(
        "",
        description=(
            "Vault KV path holding the scratch-writer ClickHouse credential (keys: "
            "SCRATCH_CH_USER, SCRATCH_CH_PASSWORD). Empty → use env/fallback."
        ),
    )
    security_ch_creds_path: str = Field(
        "",
        description=(
            "Vault KV path holding the security-writer ClickHouse credential (keys: "
            "SECURITY_CH_USER, SECURITY_CH_PASSWORD). Empty → use env/fallback."
        ),
    )
    redis_vault_path: str = Field(
        "",
        description=(
            "Vault KV path holding the employee-access Redis URL (key: REDIS_URL). "
            "Empty → use env/default."
        ),
    )
    mcp_service_key_vault_path: str = Field(
        "",
        description=(
            "Vault KV path holding the MCP export service key (key: "
            "MCP_SERVICE_KEY). Empty → use env/default (and, if that is also "
            "empty, the service-key path stays OFF / fail-closed)."
        ),
    )

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return upper

    @field_validator("jwt_algorithms")
    @classmethod
    def _validate_algorithms(cls, v: str) -> str:
        """Pin to asymmetric signing algorithms.

        Rejecting HS*/none here is defence-in-depth against algorithm-confusion
        attacks: with JWKS we only ever hold public keys, so an attacker must not
        be able to coerce the verifier into treating a public key as an HMAC
        secret (HS256) or skipping verification ('none').
        """
        algs = [a.strip().upper() for a in v.split(",") if a.strip()]
        if not algs:
            raise ValueError("jwt_algorithms must list at least one algorithm")
        for alg in algs:
            if alg == "NONE" or alg.startswith("HS"):
                raise ValueError(
                    f"Refusing insecure JWT algorithm '{alg}'. "
                    "Use an asymmetric algorithm (RS256, ES256, PS256, ...)."
                )
            if not (alg.startswith("RS") or alg.startswith("ES") or alg.startswith("PS")):
                raise ValueError(f"Unsupported JWT algorithm '{alg}'.")
        return ",".join(algs)

    @field_validator("oidc_public_key")
    @classmethod
    def _validate_public_key(cls, v: str) -> str:
        """Normalise and validate a static public key at config time (fail fast)."""
        return _normalise_public_key(v)

    @model_validator(mode="after")
    def _validate_clickhouse_connection(self) -> "Settings":
        """Require the core ClickHouse identity unless Vault will supply it.

        CLICKHOUSE_HOST / CLICKHOUSE_USER used to be mandatory fields.  They are now
        optional so a Vault-backed deploy can build Settings() with empty
        placeholders and let load_settings_from_vault() fill them post-construction
        (Vault reads run after pydantic has already sourced env/defaults).  In that
        posture the fail-closed check lives in load_settings_from_vault().  When
        Vault is OFF, enforce the old guarantee here so a misconfigured environment
        still fails fast and loud.  CLICKHOUSE_PASSWORD may legitimately be empty
        (passwordless server), so it is never part of this gate.
        """
        if self.vault_enabled:
            return self
        missing = [
            name.upper()
            for name in ("clickhouse_host", "clickhouse_user")
            if not str(getattr(self, name)).strip()
        ]
        if missing:
            raise ValueError(
                "Missing required ClickHouse connection settings: "
                + ", ".join(missing)
                + ". Set them in the environment, or enable Vault "
                "(VAULT_ENABLED=true with CLICKHOUSE_CREDS_PATH)."
            )
        return self

    @model_validator(mode="after")
    def _validate_tenant_settings(self) -> "Settings":
        """Reject a tenant env object that could weaken safety or break ClickHouse.

        Every custom-setting key must (1) sit under the configured prefix so it
        matches the ClickHouse <custom_settings_prefixes> allowlist, and (2) not
        collide with a reserved safety setting — otherwise the per-tenant merge
        could overwrite readonly / the row caps.
        """
        prefix = self.clickhouse_custom_settings_prefix
        for ch_key, claim in self.clickhouse_tenant_settings.items():
            if not ch_key.startswith(prefix):
                raise ValueError(
                    f"Tenant setting key '{ch_key}' must start with the custom "
                    f"prefix '{prefix}' (matching ClickHouse <custom_settings_prefixes>)."
                )
            if ch_key in _RESERVED_SETTING_KEYS:
                raise ValueError(
                    f"Tenant setting key '{ch_key}' shadows a reserved safety setting."
                )
            if not claim or not str(claim).strip():
                raise ValueError(
                    f"Tenant setting '{ch_key}' must map to a non-empty JWT claim name."
                )
        return self

    def jwt_algorithms_list(self) -> List[str]:
        """Return the validated signing-algorithm allowlist."""
        return [a.strip() for a in self.jwt_algorithms.split(",") if a.strip()]

    def oauth_authorization_servers_list(self) -> List[str]:
        """Authorization-server issuer URL(s) advertised in Protected Resource Metadata.

        Falls back to OIDC_ISSUER so a single-IdP deployment needs no extra config.
        """
        raw = self.oauth_authorization_servers.strip() or self.oidc_issuer.strip()
        return [s.strip() for s in raw.split(",") if s.strip()]

    def oauth_resource_identifier(self) -> str:
        """Canonical resource URL advertised as the PRM 'resource' (RFC 9728/8707)."""
        if self.oauth_resource.strip():
            return self.oauth_resource.strip().rstrip("/")
        return self.public_base_url.rstrip("/") + self.mcp_path

    def protected_resource_metadata(self) -> Dict[str, object]:
        """Build the RFC 9728 Protected Resource Metadata document for this server."""
        doc: Dict[str, object] = {
            "resource": self.oauth_resource_identifier(),
            "authorization_servers": self.oauth_authorization_servers_list(),
            "bearer_methods_supported": ["header"],
        }
        scopes = [s.strip() for s in self.oauth_scopes_supported.split(",") if s.strip()]
        if scopes:
            doc["scopes_supported"] = scopes
        return doc

    def protected_resource_metadata_url(self) -> str:
        """The well-known URL where PRM is published (RFC 9728 §3.1 path-insertion).

        For resource ``https://host/mcp`` the metadata lives at
        ``https://host/.well-known/oauth-protected-resource/mcp``.
        """
        base = self.public_base_url.rstrip("/")
        path = self.mcp_path if self.mcp_path not in ("", "/") else ""
        return f"{base}/.well-known/oauth-protected-resource{path}"

    def auth_configured(self) -> bool:
        """True when enough OIDC config is present to validate JWTs.

        Used by the REST lifespan and MCP HTTP entrypoint to fail closed: a
        network-exposed transport must not start without a way to verify token
        *signatures*.  Only a key source is mandatory — EITHER a JWKS endpoint OR
        a static public key.  Issuer and audience are OPTIONAL: when set they are
        verified (see app/auth_jwt.py), when empty that claim check is skipped.
        """
        return bool(self.oidc_jwks_url.strip() or self.oidc_public_key.strip())

    def scratch_client_params(self) -> Dict[str, object]:
        """Resolve the scratch-credential connection params (with CLICKHOUSE_* fallback).

        Each SCRATCH_CH_* field independently overrides the corresponding main
        CLICKHOUSE_* field; unset fields inherit the main connection.  This lets a
        dev/single-user deploy run the scratch endpoints against the same account
        while a production deploy points them at a scratch-only grant (invariant #1)
        by setting SCRATCH_CH_USER / SCRATCH_CH_PASSWORD.
        """
        password = (
            self.scratch_ch_password
            if self.scratch_ch_password is not None
            else self.clickhouse_password
        )
        secure = (
            self.scratch_ch_secure
            if self.scratch_ch_secure is not None
            else self.clickhouse_secure
        )
        return {
            "host": self.scratch_ch_host or self.clickhouse_host,
            "port": self.scratch_ch_port or self.clickhouse_port,
            "user": self.scratch_ch_user or self.clickhouse_user,
            "password": password,
            "secure": secure,
        }

    def security_client_params(self) -> Dict[str, object]:
        """Resolve the security-writer connection params (with CLICKHOUSE_* fallback).

        Mirrors ``scratch_client_params``: each SECURITY_CH_* field independently
        overrides the corresponding main CLICKHOUSE_* field; unset fields inherit
        the main connection.  A production deploy points these at a security-only
        grant by setting SECURITY_CH_USER / SECURITY_CH_PASSWORD.
        """
        password = (
            self.security_ch_password
            if self.security_ch_password is not None
            else self.clickhouse_password
        )
        secure = (
            self.security_ch_secure
            if self.security_ch_secure is not None
            else self.clickhouse_secure
        )
        return {
            "host": self.security_ch_host or self.clickhouse_host,
            "port": self.security_ch_port or self.clickhouse_port,
            "user": self.security_ch_user or self.clickhouse_user,
            "password": password,
            "secure": secure,
        }

    @model_validator(mode="after")
    def _validate_scratch_db_single_source(self) -> "Settings":
        """Fail LOUD if SCRATCH_DATABASE diverges from the provenance extractor's
        hardcoded scratch DB name (single source of truth, ADR-0002 review).

        Two components must agree on which database is "scratch":
          - ``_references_scratch_db`` (the ADR-0002 pre-check) scans for THIS
            configurable ``scratch_database`` to decide whether to run provenance;
          - the D64 ownership parser (``_validate_scratch_name`` /
            ``_build_alias_map`` in app/sqlparse/provenance.py) treats the hardcoded
            module constant ``_SCRATCH_DB`` as THE scratch database.

        If they diverge (e.g. an operator sets ``SCRATCH_DATABASE=sandbox`` while
        the parser still knows only ``scratch``), the pre-check would trip on
        ``sandbox.*`` but provenance would treat ``sandbox.*`` as an uncatalogued
        WAREHOUSE table → ``PARSE_FAILED_CLOSED`` even for the OWNER's own
        legitimate scratch access (north-star broken), AND the gate would silently
        stop recognising the real scratch DB. Rather than degrade silently, refuse
        to boot until the two identifiers agree. (The parser constant is not
        threaded through as a parameter in this slice — that would touch the whole
        D64 ownership chain and its tests; this assertion is the low-risk guarantee
        that the two can never diverge unnoticed.)
        """
        # Lazy import: keeps sqlglot (imported by provenance) off the config
        # module-import path; this runs once per Settings construction.
        from app.sqlparse.provenance import _SCRATCH_DB

        if self.scratch_database != _SCRATCH_DB:
            raise ValueError(
                f"SCRATCH_DATABASE='{self.scratch_database}' diverges from the "
                f"provenance extractor's scratch database '{_SCRATCH_DB}' "
                "(app/sqlparse/provenance.py::_SCRATCH_DB). The scratch-reference "
                "gate and the D64 ownership parser must name the SAME database, or "
                "the fail-closed scratch gate silently degrades. Set SCRATCH_DATABASE "
                f"to '{_SCRATCH_DB}', or update _SCRATCH_DB to match, so there is one "
                "source of truth."
            )
        return self

    @model_validator(mode="after")
    def _validate_employee_access(self) -> "Settings":
        """Fail fast if employee-access is enabled without its external API URL."""
        if self.employee_access_enabled and not self.eeaccess_base_url.strip():
            raise ValueError(
                "EMPLOYEE_ACCESS_ENABLED is true but EEACCESS_BASE_URL is empty — "
                "set the employee-access API base URL."
            )
        return self

    def allowed_databases_list(self) -> Optional[List[str]]:
        """Return the parsed allowlist, or None if '*' (all databases allowed)."""
        if self.allowed_databases.strip() == "*":
            return None
        return [db.strip() for db in self.allowed_databases.split(",") if db.strip()]


def _read_vault_secret(vc, key: str, path: str, current):
    """Read one KV field from Vault, returning ``current`` (env/default) on failure.

    A per-key failure is logged and swallowed so one missing/renamed key does not
    take down the whole config load; the caller's fail-closed check still catches a
    genuinely-absent critical secret (see load_settings_from_vault()).
    """
    try:
        return vc.read_kv_secret(key, path).get_secret_value()
    except Exception as exc:  # noqa: BLE001 - surface as a warning, keep booting
        logger.warning("Vault: could not read '%s' from '%s': %s", key, path, exc)
        return current


def load_settings_from_vault() -> Settings:
    """Construct Settings, then override sensitive values from Vault when enabled.

    Priority for each sensitive value:

      1. Vault  — when VAULT_ENABLED and the relevant ``*_PATH`` is set
      2. Environment variable / ``.env``
      3. Field default

    Vault runs AFTER construction (it mutates the already-built Settings) because
    pydantic-settings sources env/defaults at construction time; this mirrors the
    llm-router pattern.  When Vault is disabled the env-built Settings is returned
    unchanged, so nothing about the existing env-based deployment path changes.
    """
    settings = Settings()

    if not settings.vault_enabled:
        return settings

    if vault is None:
        raise RuntimeError(
            "VAULT_ENABLED=true but the internal 'paycompy' package is not "
            "installed. Install paycompy or set VAULT_ENABLED=false."
        )

    vc = vault.get_client_using_os_environ()

    # --- ClickHouse main connection ---
    if settings.clickhouse_creds_path:
        p = settings.clickhouse_creds_path
        settings.clickhouse_host = _read_vault_secret(
            vc, "CLICKHOUSE_HOST", p, settings.clickhouse_host
        )
        settings.clickhouse_user = _read_vault_secret(
            vc, "CLICKHOUSE_USER", p, settings.clickhouse_user
        )
        settings.clickhouse_password = _read_vault_secret(
            vc, "CLICKHOUSE_PASSWORD", p, settings.clickhouse_password
        )
        port = _read_vault_secret(vc, "CLICKHOUSE_PORT", p, None)
        if port not in (None, ""):
            try:
                settings.clickhouse_port = int(port)
            except (TypeError, ValueError):
                logger.warning(
                    "Vault: CLICKHOUSE_PORT '%s' is not an int; keeping %s",
                    port,
                    settings.clickhouse_port,
                )

    # --- Static OIDC public key (not secret, but validated like the field would
    # be at construction — the field_validator does NOT run on this post-hoc
    # assignment, so re-apply _normalise_public_key or a bad key silently breaks
    # asymmetric verification).
    if settings.oidc_public_key_vault_path:
        raw = _read_vault_secret(
            vc, "OIDC_PUBLIC_KEY", settings.oidc_public_key_vault_path, settings.oidc_public_key
        )
        settings.oidc_public_key = _normalise_public_key(raw)

    # --- Scratch-writer ClickHouse credential ---
    if settings.scratch_ch_creds_path:
        p = settings.scratch_ch_creds_path
        settings.scratch_ch_user = _read_vault_secret(
            vc, "SCRATCH_CH_USER", p, settings.scratch_ch_user
        )
        settings.scratch_ch_password = _read_vault_secret(
            vc, "SCRATCH_CH_PASSWORD", p, settings.scratch_ch_password
        )

    # --- Security-writer ClickHouse credential ---
    if settings.security_ch_creds_path:
        p = settings.security_ch_creds_path
        settings.security_ch_user = _read_vault_secret(
            vc, "SECURITY_CH_USER", p, settings.security_ch_user
        )
        settings.security_ch_password = _read_vault_secret(
            vc, "SECURITY_CH_PASSWORD", p, settings.security_ch_password
        )

    # --- Employee-access Redis URL ---
    if settings.redis_vault_path:
        settings.redis_url = _read_vault_secret(
            vc, "REDIS_URL", settings.redis_vault_path, settings.redis_url
        )

    # --- MCP export service key ---
    if settings.mcp_service_key_vault_path:
        settings.mcp_service_key = _read_vault_secret(
            vc, "MCP_SERVICE_KEY", settings.mcp_service_key_vault_path, settings.mcp_service_key
        )

    # Fail closed: Vault is enabled, so the core ClickHouse identity MUST be present.
    # If a transient Vault failure left host/user empty, refuse to boot rather than
    # silently start with no connection identity — crashing lets the orchestrator
    # restart the pod once Vault is healthy again.  (Password may be empty for a
    # passwordless server, so it is not part of this gate.)
    missing = [
        name.upper()
        for name in ("clickhouse_host", "clickhouse_user")
        if not str(getattr(settings, name)).strip()
    ]
    if missing:
        raise RuntimeError(
            "Vault is enabled but these ClickHouse connection settings are still "
            "empty after loading: " + ", ".join(missing) + ". Check Vault "
            "connectivity and CLICKHOUSE_CREDS_PATH."
        )

    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings singleton (Vault-resolved when VAULT_ENABLED).

    Call this everywhere you need config.  The first call resolves Vault (if
    enabled) and caches the result for the process lifetime.
    """
    return load_settings_from_vault()
