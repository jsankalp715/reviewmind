"""Application settings, loaded from environment variables (and an optional ``.env`` file)."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

APP_VERSION = "2.0.0"


class Settings(BaseSettings):
    """All runtime configuration. Every field maps to an upper-case env var of the same name."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM -------------------------------------------------------------------------------
    llm_provider: Literal["gemini", "mock"] = "gemini"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.0-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    gemini_request_timeout_seconds: float = Field(default=90.0, gt=0)

    # --- Agents / orchestration ------------------------------------------------------------
    agent_timeout_seconds: float = Field(default=60.0, gt=0)
    agent_max_retries: int = Field(default=1, ge=0, le=5)
    agent_retry_backoff_seconds: float = Field(default=1.0, ge=0)
    llm_max_concurrency: int = Field(default=4, ge=1, le=64)
    # Accepts JSON (`["security","tests"]`) or a comma-separated list (`security,tests`).
    enabled_agents: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["security", "performance", "documentation", "tests"]
    )

    # --- Chunking --------------------------------------------------------------------------
    chunk_max_tokens: int = Field(default=6000, ge=200)
    chars_per_token: int = Field(default=4, ge=1)
    max_chunks_per_review: int = Field(default=40, ge=1)

    # --- GitHub ----------------------------------------------------------------------------
    github_token: SecretStr | None = None
    github_webhook_secret: SecretStr | None = None
    github_api_url: str = "https://api.github.com"
    github_timeout_seconds: float = Field(default=30.0, gt=0)
    github_max_retries: int = Field(default=4, ge=0, le=10)
    github_backoff_base_seconds: float = Field(default=1.0, ge=0)
    github_backoff_max_seconds: float = Field(default=60.0, ge=0)
    post_reviews: bool = True

    # Bearer token for the read API (GET /reviews*). Reviews describe code in your repos, so
    # set this whenever the server is reachable by anyone else. /health stays public.
    api_token: SecretStr | None = None
    max_webhook_body_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)  # GitHub's cap

    # --- Storage / server ------------------------------------------------------------------
    database_path: str = "reviewmind.db"
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    port: int = 8000

    @field_validator("enabled_agents", mode="before")
    @classmethod
    def _parse_agents(cls, value: Any) -> Any:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return json.loads(text)
            return [part.strip() for part in text.split(",") if part.strip()]
        return value

    @model_validator(mode="after")
    def _check_provider_credentials(self) -> Settings:
        if self.llm_provider == "gemini" and not (
            self.gemini_api_key and self.gemini_api_key.get_secret_value()
        ):
            raise ValueError("LLM_PROVIDER=gemini requires GEMINI_API_KEY to be set")
        return self

    @property
    def chunk_max_chars(self) -> int:
        """Chunk budget in characters (the token budget times the chars-per-token estimate)."""
        return self.chunk_max_tokens * self.chars_per_token

    @property
    def webhook_secret(self) -> str | None:
        """The webhook secret as a plain string, or ``None`` if it is not configured."""
        if self.github_webhook_secret is None:
            return None
        value = self.github_webhook_secret.get_secret_value()
        return value or None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings (cached; call ``get_settings.cache_clear()`` in tests)."""
    return Settings()
