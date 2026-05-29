"""Application configuration loaded from environment variables via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- API authentication ---
    api_key: str = Field(..., description="Bearer token required on every route (secret)")

    # --- Query safety limits ---
    max_execution_time: int = Field(30, description="Max query wall-clock time in seconds")
    max_result_rows: int = Field(10_000, description="ClickHouse-side hard cap on result rows")
    max_rows_to_read: int = Field(100_000_000, description="ClickHouse-side max rows scanned")
    default_limit: int = Field(1_000, description="LIMIT injected when query has none")
    max_response_rows: int = Field(1_000, description="Max rows returned in API response")

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

    def allowed_databases_list(self) -> Optional[List[str]]:
        """Return the parsed allowlist, or None if '*' (all databases allowed)."""
        if self.allowed_databases.strip() == "*":
            return None
        return [db.strip() for db in self.allowed_databases.split(",") if db.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings singleton.  Call this everywhere you need config."""
    return Settings()
