"""Application configuration loaded from environment variables.

Settings are read once at startup and exposed via dependency injection.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class NodeRole(StrEnum):
    PRIMARY = "primary"
    REPLICA = "replica"


class Settings(BaseSettings):
    """Process-wide configuration.

    Values come from environment variables. A `.env` file is loaded in development
    if present; production deployments inject the env directly via docker-compose.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(
        ...,
        description="asyncpg DSN, e.g. postgresql://user:pass@host:5432/hrserv",
    )

    max_upload_bytes: int = Field(
        default=5 * 1024 * 1024,
        ge=1024,
        description="Reject multipart uploads larger than this many bytes (default 5 MiB).",
    )

    node_role: NodeRole = Field(
        ...,
        description=(
            "primary accepts writes; replica returns 503 on POST /upload_json. "
            "Required (no default) so a misconfigured node fails to start rather than "
            "silently picking the wrong role."
        ),
    )

    require_cf_access_headers: bool = Field(
        default=True,
        description=(
            "If true, ingest rejects 401 when the Cloudflare Access JWT/email headers are "
            "absent. Defense in depth in case the Access policy is misconfigured. Disable "
            "only in local dev/tests."
        ),
    )

    log_level: str = Field(default="INFO")

    db_pool_min_size: int = Field(default=1, ge=1)
    db_pool_max_size: int = Field(default=8, ge=1)


def load_settings() -> Settings:
    """Construct a Settings instance from the current environment.

    Kept as a function (not a module-level singleton) so tests can override env vars
    and call this fresh.
    """
    return Settings()  # type: ignore[call-arg]
