from __future__ import annotations

import logging
import secrets
from typing import ClassVar

from pydantic import Field, field_validator, model_validator
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
        default="openrouter/meta-llama/llama-3.3-70b-instruct", alias="CRITIC_MODEL"
    )

    # Optional — database. Falls back to local SQLite if unset.
    database_url: str | None = Field(default=None, alias="DATABASE_URL")

    # Optional — live diagnostic-session state. Falls back to an in-memory
    # dict if unset (single-process only; no restart survival, no sharing
    # across replicas — see src/diffdx/session_store.py).
    redis_url: str | None = Field(default=None, alias="REDIS_URL")

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

    # Deployment environment. "production" turns on the hard requirements
    # below (SECRET_KEY, CORS_ALLOWED_ORIGINS) instead of the dev-friendly
    # auto-generated/wildcard fallbacks.
    environment: str = Field(default="development", alias="ENVIRONMENT")

    # Required in production — JWT signing key (HS256). Task 5: the old
    # behavior (auto-generate a random key on every boot if unset) silently
    # invalidated every session on every deploy, which is why this now
    # refuses to start in production rather than papering over it. A dev
    # default is fine locally since nobody's session outlives a restart
    # they didn't cause anyway.
    secret_key: str | None = Field(default=None, alias="SECRET_KEY")
    access_token_ttl_minutes: int = Field(default=15, alias="ACCESS_TOKEN_TTL_MINUTES")
    refresh_token_ttl_days: int = Field(default=7, alias="REFRESH_TOKEN_TTL_DAYS")

    # CORS. "*" is fine for local dev; production must set a real list.
    cors_allowed_origins: str = Field(default="*", alias="CORS_ALLOWED_ORIGINS")

    @field_validator(
        "openrouter_api_key", "cerebras_api_key", "gemini_api_key", "database_url",
        "redis_url", "resend_api_key", "smtp_host", "smtp_user", "smtp_pass", "smtp_from",
        "elevenlabs_api_key", "sarvam_api_key", "secret_key",
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

    @field_validator("access_token_ttl_minutes", mode="before")
    @classmethod
    def _blank_access_ttl_means_default(cls, v):
        return 15 if v == "" else v

    @field_validator("refresh_token_ttl_days", mode="before")
    @classmethod
    def _blank_refresh_ttl_means_default(cls, v):
        return 7 if v == "" else v

    @field_validator("critic_model", mode="before")
    @classmethod
    def _blank_critic_model_means_default(cls, v):
        return "openrouter/google/gemma-4-31b-it:free" if v == "" else v

    @field_validator("resend_from", mode="before")
    @classmethod
    def _blank_resend_from_means_default(cls, v):
        return "reminders@diffdx.app" if v == "" else v

    @field_validator("environment", mode="before")
    @classmethod
    def _blank_environment_means_default(cls, v):
        return "development" if v == "" else v

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _blank_cors_origins_means_wildcard(cls, v):
        return "*" if v == "" else v

    @model_validator(mode="after")
    def _require_production_settings(self) -> "Settings":
        if self.environment.lower() == "production":
            if not self.secret_key:
                raise ValueError(
                    "SECRET_KEY must be set in production. The old "
                    "auto-generate-on-boot fallback silently invalidated "
                    "every session on every deploy — refusing to start "
                    "instead of repeating that."
                )
            if self.cors_allowed_origins == "*":
                raise ValueError(
                    "CORS_ALLOWED_ORIGINS must be set to a real, "
                    "comma-separated origin list in production — "
                    "allow_origins=['*'] on an app handling patient data "
                    "is indefensible."
                )
        return self

    # Cached at first access so the same random key is reused for the life
    # of this process (a fresh one on every access would invalidate every
    # token issued moments earlier) — but it's never persisted, so a
    # restart still invalidates every session in dev. That's fine; only
    # production requires an explicit, stable SECRET_KEY (enforced above).
    _dev_secret_key: ClassVar[str | None] = None

    @property
    def resolved_secret_key(self) -> str:
        if self.secret_key:
            return self.secret_key
        # _require_production_settings already guarantees we're not in
        # production if we get here.
        if Settings._dev_secret_key is None:
            Settings._dev_secret_key = secrets.token_urlsafe(48)
            _log.warning(
                "SECRET_KEY not set — using a random per-process key "
                "(dev only). Every session is invalidated on restart. "
                "Set SECRET_KEY in .env to persist sessions across restarts."
            )
        return Settings._dev_secret_key

    @property
    def cors_origins_list(self) -> list[str]:
        if self.cors_allowed_origins == "*":
            return ["*"]
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    def log_disabled_integrations(self) -> None:
        """Right now these silently no-op when unset — log it instead."""
        if not self.openrouter_api_key:
            _log.warning("OPENROUTER_API_KEY not set — critic scoring disabled.")
        if not self.database_url:
            _log.warning("DATABASE_URL not set — using local SQLite fallback.")
        if not self.redis_url:
            _log.warning("REDIS_URL not set — diagnostic sessions held in-memory (no restart survival, no multi-replica sharing).")
        if not self.resend_api_key and not self.smtp_host:
            _log.warning("Neither RESEND_API_KEY nor SMTP_HOST set — email notifications disabled.")
        if not self.elevenlabs_api_key:
            _log.warning("ELEVENLABS_API_KEY not set — voice features disabled.")
        if not self.sarvam_api_key:
            _log.warning("SARVAM_API_KEY not set — multilingual support disabled.")


settings = Settings()
