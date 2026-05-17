"""Application configuration loaded from environment variables.

Settings are read once at startup and exposed via dependency injection.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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

    # NoDecode suppresses pydantic-settings' default JSON-decode pass on
    # complex-typed env vars, so the field_validator below can handle a plain
    # comma-separated string (the docker-compose-friendly format).
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default=[
            "https://hrfunc.org",
            "https://www.hrfunc.org",
            "http://localhost:5000",
        ],
        description=(
            "Origins permitted to make cross-origin GET/HEAD requests (intended for "
            "the frontend's /healthz status pill). Comma-separated in env; defaults "
            "cover hrfunc.org + www + the Flask dev port. CORS is scoped to GET/HEAD "
            "only at the middleware layer, so widening this never exposes the ingest "
            "POST surface to browsers."
        ),
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv_origins(cls, v: object) -> object:
        if isinstance(v, str):
            parts = [item.strip() for item in v.split(",") if item.strip()]
            if not parts:
                # An empty CORS_ORIGINS env (e.g. `CORS_ORIGINS=` left in a .env
                # template) would otherwise silently disable CORS for everyone
                # and break the frontend status pill with no obvious signal.
                # Fail loudly at startup instead.
                raise ValueError(
                    "CORS_ORIGINS is set but empty. Unset to use the default "
                    "allowlist, or provide at least one origin."
                )
            return parts
        return v


def load_settings() -> Settings:
    """Construct a Settings instance from the current environment.

    Kept as a function (not a module-level singleton) so tests can override env vars
    and call this fresh.
    """
    return Settings()  # type: ignore[call-arg]
