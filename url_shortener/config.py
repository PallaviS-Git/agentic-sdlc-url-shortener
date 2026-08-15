from __future__ import annotations

import enum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, enum.Enum):
    development = "development"
    testing = "testing"
    production = "production"


class Settings(BaseSettings):
    """
    All configuration is read from environment variables or a .env file.
    No secrets are hardcoded. See .env.example for the full list.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ─────────────────────────────────────────────────────────
    app_name: str = "agentic-sdlc-url-shortener"
    app_version: str = "0.1.0"
    environment: Environment = Environment.development
    debug: bool = False
    log_level: str = "INFO"

    # ── Database ─────────────────────────────────────────────────────────────
    # Default targets the docker-compose postgres service for local dev.
    # Must be overridden in staging/production via the DATABASE_URL env var.
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/urlshortener",
        description="Async PostgreSQL connection URL (asyncpg driver)",
    )

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL",
    )

    # ── Service ───────────────────────────────────────────────────────────────
    base_url: str = "http://localhost:8000"
    short_code_length: int = 8
    default_ttl_seconds: int = 60 * 60 * 24 * 365  # 1 year
    rate_limit_per_minute: int = 100

    # ── Orchestrator ──────────────────────────────────────────────────────────
    max_stage_retries: int = 3
    approval_timeout_seconds: int = 300
    state_file_path: str = "orchestrator_state.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
