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

    # --- OpenAPI / GPT Action ---
    public_base_url: str = Field(
        "https://your-public-host.example.com",
        description="Public HTTPS base URL used in the OpenAPI servers block",
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

    def allowed_databases_list(self) -> Optional[List[str]]:
        """Return the parsed allowlist, or None if '*' (all databases allowed)."""
        if self.allowed_databases.strip() == "*":
            return None
        return [db.strip() for db in self.allowed_databases.split(",") if db.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings singleton.  Call this everywhere you need config."""
    return Settings()
