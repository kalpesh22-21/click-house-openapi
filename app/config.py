"""Application configuration loaded from environment variables via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Dict, List, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    clickhouse_host: str = Field(..., description="ClickHouse server hostname or IP")
    clickhouse_port: int = Field(8443, description="ClickHouse HTTP(S) port")
    clickhouse_user: str = Field(..., description="ClickHouse username")
    clickhouse_password: str = Field(..., description="ClickHouse password (secret)")
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
    # --- Admin authentication (static API key) ---
    # The /admin CSV-ingest write routes are an internal operational path, not a
    # per-tenant LLM surface, so they authenticate with a single shared API key
    # (Bearer) rather than a per-user JWT.  Required only when the /admin routes
    # are used; require_admin_api_key fails closed when it is empty.
    api_key: str = Field(
        "", description="Static Bearer API key for the /admin write routes (secret)."
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

    # --- Per-tenant row isolation (the "env object") ---
    # JSON object mapping a ClickHouse custom-setting name -> the JWT claim that
    # fills it.  Each authenticated query injects settings[ch_setting] = jwt[claim]
    # via the trusted settings= channel (never concatenated into SQL).  ClickHouse
    # row policies read getSetting('SQL_tenant') to filter rows per tenant.
    # Example: CLICKHOUSE_TENANT_SETTINGS='{"SQL_tenant": "user_name"}'
    clickhouse_tenant_settings: Dict[str, str] = Field(
        default_factory=lambda: {"SQL_tenant": "user_name"},
        description=(
            "JSON object mapping ClickHouse custom-setting name to the JWT claim "
            "that fills it. Keys must start with CLICKHOUSE_CUSTOM_SETTINGS_PREFIX "
            "and must not shadow a reserved safety setting."
        ),
    )
    clickhouse_custom_settings_prefix: str = Field(
        "SQL_",
        description=(
            "Required prefix for tenant custom-setting keys. Must match the "
            "ClickHouse server's <custom_settings_prefixes> configuration."
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
    # Populates security.user_employee_access, the table the dbpcm_warehouse.employee
    # row policy reads to scope each tenant (identified by its stable composite JTI)
    # to only the employees it may see.  See app/employee_access.py for the full
    # design (ReplacingMergeTree pull_id gate, staleness-gated refresh, sentinel
    # row, fail-closed-loud).  Disabled by default so existing deploys are unaffected.
    employee_access_enabled: bool = Field(
        False,
        description=(
            "Enable per-request employee-access provisioning for the employee row "
            "policy. When true, EEACCESS_BASE_URL must be set and the tenant-settings "
            "map should expose SQL_TENANT/SQL_CLIENTCODE/SQL_PROCCENTER."
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
    # NOTE: the row-level TTL on security.user_employee_access is defined in the
    # DBA-owned CREATE TABLE (see app/employee_access.py), NOT here — the app never
    # creates that table. Recommended `TTL UpdatedAt + INTERVAL 1 DAY` (must exceed N).
    security_database: str = Field(
        "security",
        description="ClickHouse database holding user_employee_access (never the warehouse).",
    )
    # Claim names the JTI / clientcode / proc_center are read from (the JTI keys the
    # access table and identifies the user to /cl/eeaccess).
    employee_access_jti_claim: str = Field(
        "jti", description="JWT claim carrying the stable composite JTI (row-policy key)."
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
    # `security.user_employee_access` (the table + row policy are DBA-owned; the
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
        """Normalise and validate a static public key at config time (fail fast).

        Accepts escaped ``\\n`` (env/secret stores often flatten PEMs to one line).
        Rejects anything that is not a loadable PEM *public* key — including a
        pasted private key — so a misconfiguration cannot silently disable auth.
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings singleton.  Call this everywhere you need config."""
    return Settings()
