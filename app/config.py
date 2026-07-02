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
    # Callers present a signed JWT as a Bearer token.  We validate the signature
    # against the IdP's JWKS endpoint and verify issuer / audience / expiry, then
    # derive the tenant identity from a claim (see clickhouse_tenant_settings).
    # All three fields are required for the REST API and MCP HTTP transport; the
    # MCP stdio transport (local trust model) performs no auth.  They are optional
    # at the schema level so stdio mode can start without them — the REST app
    # lifespan and MCP _run_http fail closed if they are unset (see those modules).
    oidc_jwks_url: str = Field(
        "",
        description="JWKS endpoint (e.g. https://idp/.well-known/jwks.json) used to verify JWT signatures.",
    )
    oidc_issuer: str = Field("", description="Expected JWT 'iss' claim (token issuer).")
    oidc_audience: str = Field("", description="Expected JWT 'aud' claim (this API's audience).")
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
    # Defaults true (fail-closed / deploy posture); a short-lived false is the
    # mint-then-enforce transition escape hatch (remove once every minting path
    # stamps sid_hash — see the design's OQ-2).  Session-less requests (no
    # X-Session-Id header) are unaffected regardless of this flag.
    require_sid_binding: bool = Field(
        True,
        description=(
            "Require a matching 'sid_hash' JWT claim whenever an X-Session-Id "
            "header is present (session-hijack protection). Fail-closed default."
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
        network-exposed transport must not start without a way to verify tokens.
        """
        return bool(
            self.oidc_jwks_url.strip()
            and self.oidc_issuer.strip()
            and self.oidc_audience.strip()
        )

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

    def allowed_databases_list(self) -> Optional[List[str]]:
        """Return the parsed allowlist, or None if '*' (all databases allowed)."""
        if self.allowed_databases.strip() == "*":
            return None
        return [db.strip() for db in self.allowed_databases.split(",") if db.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings singleton.  Call this everywhere you need config."""
    return Settings()
