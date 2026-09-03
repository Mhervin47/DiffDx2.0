from __future__ import annotations

import logging

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_log = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Every environment variable web/api.py actually reads, typed and
    validated in one place — replacing the scattered os.environ.get() calls
    (and the os.environ.setdefault("CRITIC_MODEL", ...) that ran at import
    time, which belongs here as a typed default instead).

    Fails fast at import time if GROQ_API_KEY is missing, naming it —
    every request would 503 without it anyway; better to know at boot.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Required
    groq_api_key: str = Field(..., alias="GROQ_API_KEY")

    # Optional — LLM fallback chain / critic. Critic is disabled if unset.
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    cerebras_api_key: str | None = Field(default=None, alias="CEREBRAS_API_KEY")
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    critic_model: str = Field(
        default="openrouter/google/gemma-4-31b-it:free", alias="CRITIC_MODEL"
    )

    # Optional — database. Falls back to local SQLite if unset.
    database_url: str | None = Field(default=None, alias="DATABASE_URL")

    # Optional — transactional email via Resend.
    resend_api_key: str | None = Field(default=None, alias="RESEND_API_KEY")
    resend_from: str = Field(default="reminders@diffdx.app", alias="RESEND_FROM")

    # Optional — transactional email via SMTP (alternative to Resend).
    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str | None = Field(default=None, alias="SMTP_USER")
    smtp_pass: str | None = Field(default=None, alias="SMTP_PASS")
    smtp_from: str | None = Field(default=None, alias="SMTP_FROM")

    # Optional — voice.
    elevenlabs_api_key: str | None = Field(default=None, alias="ELEVENLABS_API_KEY")

    # Optional — multilingual support.
    sarvam_api_key: str | None = Field(default=None, alias="SARVAM_API_KEY")

    @field_validator(
        "openrouter_api_key", "cerebras_api_key", "gemini_api_key", "database_url",
        "resend_api_key", "smtp_host", "smtp_user", "smtp_pass", "smtp_from",
        "elevenlabs_api_key", "sarvam_api_key",
        mode="before",
    )
    @classmethod
    def _blank_env_value_means_unset(cls, v):
        # A .env file with `KEY=` (present but empty) should behave like the
        # var being unset, not like an explicit empty value — this is how
        # every other os.environ.get(...) call in the codebase already
        # treats it (`if not key:`), so config.py should match.
        return None if v == "" else v

    @field_validator("smtp_port", mode="before")
    @classmethod
    def _blank_smtp_port_means_default(cls, v):
        return 587 if v == "" else v

    @field_validator("critic_model", mode="before")
    @classmethod
    def _blank_critic_model_means_default(cls, v):
        return "openrouter/google/gemma-4-31b-it:free" if v == "" else v

    @field_validator("resend_from", mode="before")
    @classmethod
    def _blank_resend_from_means_default(cls, v):
        return "reminders@diffdx.app" if v == "" else v

    def log_disabled_integrations(self) -> None:
        """Right now these silently no-op when unset — log it instead."""
        if not self.openrouter_api_key:
            _log.warning("OPENROUTER_API_KEY not set — critic scoring disabled.")
        if not self.database_url:
            _log.warning("DATABASE_URL not set — using local SQLite fallback.")
        if not self.resend_api_key and not self.smtp_host:
            _log.warning("Neither RESEND_API_KEY nor SMTP_HOST set — email notifications disabled.")
        if not self.elevenlabs_api_key:
            _log.warning("ELEVENLABS_API_KEY not set — voice features disabled.")
        if not self.sarvam_api_key:
            _log.warning("SARVAM_API_KEY not set — multilingual support disabled.")


settings = Settings()
